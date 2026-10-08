"""Tests for Discord subscriptions: entitlement events become GuildEntitlement
rows, the reconcile on connect, the grace period, and the subscribe buttons
in /premium and in "needs a higher plan" replies."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import discord  # noqa: E402

from bot.cogs.billing import BillingEvents, thanks_message  # noqa: E402
from bot.cogs.premium import Premium, TrialView, format_premium, plans_to_offer  # noqa: E402
from bot.db import Database  # noqa: E402
from bot.utils.billing import PAID_GRACE_DAYS, Billing, DiscordEntitlement, row_values  # noqa: E402
from bot.utils.skus import sku_id_for  # noqa: E402
from bot.utils.tiers import FREE, FULL, MID, Tiers, TierStatus, ensure_feature  # noqa: E402

TS = "%Y-%m-%d %H:%M:%S"
NOW = datetime.now(timezone.utc).replace(microsecond=0)
GUILD = 111
MID_SKU = int(sku_id_for("mid"))
FULL_SKU = int(sku_id_for("full"))


def ent(id=1, sku=FULL_SKU, guild=GUILD, starts=-30, ends=None, deleted=False, test=False):
    return DiscordEntitlement(
        id=id, sku_id=sku, guild_id=guild,
        starts_at=None if starts is None else NOW + timedelta(days=starts),
        ends_at=None if ends is None else NOW + timedelta(days=ends),
        deleted=deleted, test=test,
    )


def ts(days: float) -> str:
    return (NOW + timedelta(days=days)).strftime(TS)


class _Case(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "t.db")
        await self.db.connect()
        self.tiers = Tiers(self.db, enforced=True)
        self.billing = Billing(self.db, self.tiers)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def status(self, guild=GUILD):
        self.tiers.invalidate()
        return await self.tiers.status(str(guild))

    async def rows(self):
        async with self.db.conn.execute(
            "SELECT * FROM GuildEntitlement WHERE Source = 'discord' ORDER BY Id"
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def audit_actions(self):
        async with self.db.conn.execute("SELECT Action FROM EntitlementAudit ORDER BY Id") as c:
            return [r["Action"] for r in await c.fetchall()]


class RowValuesTests(unittest.TestCase):
    def test_active_subscription_has_no_end(self) -> None:
        tier, starts, ends, reason = row_values(ent(), NOW)
        self.assertEqual((tier, starts, ends), ("full", ts(-30), None))
        self.assertEqual(reason, "Discord subscription")

    def test_ended_subscription_gets_grace(self) -> None:
        tier, _, ends, reason = row_values(ent(sku=MID_SKU, ends=2), NOW)
        self.assertEqual(tier, "mid")
        self.assertEqual(ends, ts(2 + PAID_GRACE_DAYS))
        self.assertIn(f"{PAID_GRACE_DAYS}-day grace", reason)

    def test_test_entitlement_without_dates(self) -> None:
        _, starts, ends, reason = row_values(ent(starts=None, test=True), NOW)
        self.assertEqual((starts, ends), (NOW.strftime(TS), None))
        self.assertEqual(reason, "Discord test entitlement")

    def test_unknown_sku_or_user_entitlement_ignored(self) -> None:
        self.assertIsNone(row_values(ent(sku=42), NOW))
        self.assertIsNone(row_values(ent(guild=None), NOW))


class ApplyTests(_Case):
    async def test_subscribe_upgrades_server(self) -> None:
        self.assertEqual((await self.status()).policy, FREE)
        self.assertEqual(await self.billing.apply(ent(), NOW), "added")
        status = await self.status()
        self.assertEqual((status.policy, status.source, status.ends_at), (FULL, "discord", None))
        self.assertEqual(status.describe(), "Command — subscribed")
        self.assertEqual(await self.audit_actions(), ["discord_add"])

    async def test_same_event_twice_is_unchanged(self) -> None:
        await self.billing.apply(ent(), NOW)
        self.assertEqual(await self.billing.apply(ent(), NOW), "unchanged")
        self.assertEqual(len(await self.rows()), 1)

    async def test_end_keeps_plan_through_grace_then_falls_back(self) -> None:
        await self.billing.apply(ent(), NOW)
        self.assertEqual(await self.billing.apply(ent(ends=-1), NOW), "updated")
        status = await self.status()
        self.assertEqual(status.policy, FULL)  # 1 day past the end, inside the grace
        self.assertEqual(status.ends_at, ts(-1 + PAID_GRACE_DAYS))

        await self.billing.apply(ent(ends=-PAID_GRACE_DAYS - 1), NOW)
        self.assertEqual((await self.status()).policy, FREE)
        self.assertEqual(await self.audit_actions(), ["discord_add", "discord_update", "discord_update"])

    async def test_lapsed_subscription_falls_back_to_gift(self) -> None:
        await self.db.add_entitlement(str(GUILD), "mid", "gift", starts_at=ts(-60), ends_at=None,
                                      granted_by="op", reason="friend")
        await self.billing.apply(ent(), NOW)
        self.assertEqual((await self.status()).policy, FULL)
        await self.billing.apply(ent(ends=-30), NOW)
        status = await self.status()
        self.assertEqual((status.policy, status.source), (MID, "gift"))

    async def test_refund_revokes_at_once(self) -> None:
        await self.billing.apply(ent(), NOW)
        self.assertEqual(await self.billing.apply(ent(deleted=True), NOW), "revoked")
        self.assertEqual((await self.status()).policy, FREE)
        self.assertEqual(await self.billing.remove(1, NOW), "ignored")  # already revoked

    async def test_revoked_row_stays_revoked(self) -> None:
        await self.billing.apply(ent(), NOW)
        await self.billing.remove(1, NOW)
        self.assertEqual(await self.billing.apply(ent(), NOW), "unchanged")
        self.assertEqual((await self.status()).policy, FREE)

    async def test_ignored_skus(self) -> None:
        self.assertEqual(await self.billing.apply(ent(sku=42), NOW), "ignored")
        self.assertEqual(await self.billing.apply(ent(guild=None), NOW), "ignored")
        self.assertEqual(await self.rows(), [])

    async def test_apply_clears_tier_cache(self) -> None:
        self.assertEqual((await self.tiers.status(str(GUILD))).policy, FREE)  # cached
        await self.billing.apply(ent(), NOW)
        self.assertEqual((await self.tiers.status(str(GUILD))).policy, FULL)


class ReconcileTests(_Case):
    async def test_adds_updates_and_revokes_missing(self) -> None:
        await self.billing.apply(ent(id=1), NOW)
        await self.billing.apply(ent(id=2, guild=222), NOW)
        counts = await self.billing.reconcile(
            [ent(id=1, ends=-1), ent(id=3, guild=333, sku=MID_SKU)], NOW
        )
        self.assertEqual(counts, {"updated": 1, "added": 1, "revoked": 1})
        self.assertEqual((await self.status(222)).policy, FREE)
        self.assertEqual((await self.status(333)).policy, MID)
        self.assertEqual((await self.status()).policy, FULL)  # in its grace

    async def test_second_run_changes_nothing(self) -> None:
        listing = [ent(id=1), ent(id=2, guild=222, ends=-60)]
        await self.billing.reconcile(listing, NOW)
        self.assertEqual(await self.billing.reconcile(listing, NOW), {"unchanged": 2})

    async def test_cog_skips_reconcile_when_fetch_fails(self) -> None:
        await self.billing.apply(ent(), NOW)

        def failing(**kwargs):
            raise discord.HTTPException(SimpleNamespace(status=500, reason="boom"), "down")

        bot = SimpleNamespace(db=self.db, tiers=self.tiers, entitlements=failing)
        cog = BillingEvents(bot)  # type: ignore[arg-type]
        self.assertIsNone(await cog.reconcile())
        self.assertEqual((await self.status()).policy, FULL)

    async def test_cog_reconciles_listing(self) -> None:
        live = SimpleNamespace(
            id=5, sku_id=MID_SKU, guild_id=GUILD, starts_at=NOW, ends_at=None, deleted=False,
            type=discord.EntitlementType.application_subscription,
        )

        async def listing(**kwargs):
            self.assertEqual(kwargs, {"limit": None, "exclude_ended": False})
            yield live

        bot = SimpleNamespace(db=self.db, tiers=self.tiers, entitlements=listing)
        cog = BillingEvents(bot)  # type: ignore[arg-type]
        self.assertEqual(await cog.reconcile(), {"added": 1})
        self.assertEqual((await self.status()).policy, MID)


class EventTests(_Case):
    def _cog(self):
        bot = SimpleNamespace(db=self.db, tiers=self.tiers)
        return BillingEvents(bot)  # type: ignore[arg-type]

    def _discord_ent(self, **kw):
        base = dict(id=9, sku_id=FULL_SKU, guild_id=GUILD, starts_at=NOW, ends_at=None,
                    deleted=False, type=discord.EntitlementType.application_subscription)
        base.update(kw)
        return SimpleNamespace(**base)

    async def test_create_posts_thanks_once(self) -> None:
        cog = self._cog()
        with patch("bot.cogs.billing.send_to_report_channel", new=AsyncMock()) as send:
            await cog.on_entitlement_create(self._discord_ent())
            await cog.on_entitlement_create(self._discord_ent())  # duplicate event
        send.assert_awaited_once()
        self.assertEqual(send.await_args.kwargs["content"], thanks_message("full"))
        self.assertIn("**Command** plan", thanks_message("full"))

    async def test_update_and_delete(self) -> None:
        cog = self._cog()
        with patch("bot.cogs.billing.send_to_report_channel", new=AsyncMock()):
            await cog.on_entitlement_create(self._discord_ent())
        await cog.on_entitlement_update(self._discord_ent(ends_at=NOW - timedelta(days=30)))
        self.assertEqual((await self.status()).policy, FREE)
        await cog.on_entitlement_delete(self._discord_ent())
        self.assertIsNotNone((await self.rows())[0]["RevokedAt"])


class OfferTests(unittest.TestCase):
    def test_free_server_sees_both(self) -> None:
        self.assertEqual(plans_to_offer(TierStatus(FREE), []), [MID, FULL])

    def test_alliance_subscriber_sees_command(self) -> None:
        active = [{"Tier": "mid", "Source": "discord"}]
        self.assertEqual(plans_to_offer(TierStatus(MID, "discord"), active), [FULL])

    def test_command_subscriber_sees_nothing(self) -> None:
        active = [{"Tier": "full", "Source": "discord"}]
        self.assertEqual(plans_to_offer(TierStatus(FULL, "discord"), active), [])

    def test_trial_sees_both(self) -> None:
        active = [{"Tier": "full", "Source": "trial"}]
        self.assertEqual(plans_to_offer(TierStatus(FULL, "trial", ts(5)), active), [MID, FULL])

    def test_permanent_command_gift_sees_nothing(self) -> None:
        active = [{"Tier": "full", "Source": "gift"}]
        self.assertEqual(plans_to_offer(TierStatus(FULL, "gift"), active), [])

    def test_premium_text(self) -> None:
        self.assertIn("aren't on sale yet", format_premium(TierStatus(FREE), 0, enforced=True))
        text = format_premium(TierStatus(FREE), 0, enforced=True, on_sale=True)
        self.assertIn("billed monthly by Discord", text)
        self.assertNotIn("on sale yet", text)


def _sku_ids(view) -> list[int]:
    return [item.sku_id for item in view.children if getattr(item, "sku_id", None)]


class ButtonTests(_Case):
    def _premium(self, billing: bool):
        bot = SimpleNamespace(
            db=self.db, tiers=self.tiers, get_cog=lambda name: None,
            settings=SimpleNamespace(support_url=None, bot_owner_ids=set(),
                                     billing_enabled=billing),
        )
        return Premium(bot)  # type: ignore[arg-type]

    def _interaction(self, manage=True):
        followup = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())))
        return SimpleNamespace(
            guild_id=GUILD, user=SimpleNamespace(id=9), guild=SimpleNamespace(owner_id=7),
            permissions=SimpleNamespace(manage_guild=manage),
            response=SimpleNamespace(defer=AsyncMock()), followup=followup,
        )

    async def test_premium_has_no_buttons_until_billing_enabled(self) -> None:
        premium = self._premium(False)
        inter = self._interaction(manage=False)
        await premium.premium.callback(premium, inter)
        self.assertNotIn("view", inter.followup.send.await_args.kwargs)

    async def test_premium_buttons_with_trial(self) -> None:
        premium = self._premium(True)
        inter = self._interaction()
        await premium.premium.callback(premium, inter)
        view = inter.followup.send.await_args.kwargs["view"]
        self.assertIsInstance(view, TrialView)
        self.assertEqual(_sku_ids(view), [MID_SKU, FULL_SKU])

    async def test_premium_buttons_for_member(self) -> None:
        await self.billing.apply(ent(sku=MID_SKU), NOW)
        premium = self._premium(True)
        inter = self._interaction(manage=False)
        await premium.premium.callback(premium, inter)
        self.assertEqual(_sku_ids(inter.followup.send.await_args.kwargs["view"]), [FULL_SKU])

    async def test_locked_reply_offers_needed_plan_and_up(self) -> None:
        sent = []

        async def send_message(text, **kwargs):
            sent.append(kwargs)

        for billing, expected in ((False, None), (True, [FULL_SKU])):
            inter = SimpleNamespace(
                client=SimpleNamespace(tiers=self.tiers,
                                       settings=SimpleNamespace(billing_enabled=billing)),
                guild_id=GUILD,
                response=SimpleNamespace(is_done=lambda: False, send_message=send_message),
            )
            self.assertFalse(await ensure_feature(inter, "multi_channel_reports"))
            view = sent[-1].get("view")
            self.assertEqual(None if view is None else _sku_ids(view), expected)


if __name__ == "__main__":
    unittest.main()
