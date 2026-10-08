"""Tests for the free Command trial: starting it from /premium, once per
server (even after a revoke or a data purge), overlap with other plans, the
3-day heads-up, and /ops trial-reset."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.cogs.ops import Ops, format_show  # noqa: E402
from bot.cogs.premium import (  # noqa: E402
    Premium,
    TrialView,
    format_premium,
    trial_note,
    trial_refused_message,
)
from bot.db import Database  # noqa: E402
from bot.utils.gift_reminders import STAGE_SERVER, GiftReminders  # noqa: E402
from bot.utils.tiers import FREE, FULL, MID, TRIAL_DAYS, Tiers, TierStatus  # noqa: E402

TS = "%Y-%m-%d %H:%M:%S"
NOW = datetime.now(timezone.utc).replace(microsecond=0)
GUILD = "111"
CHANNEL = "222"
OPERATOR = 1
OWNER = 77
CONTROL = 100
SUPPORT = "https://discord.gg/support"


def ts(days: float, base: datetime = NOW) -> str:
    return (base + timedelta(days=days)).strftime(TS)


class _Case(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "t.db")
        await self.db.connect()
        self.tiers = Tiers(self.db, enforced=False)
        self.bot = SimpleNamespace(
            db=self.db, tiers=self.tiers, guilds=[],
            settings=SimpleNamespace(
                control_guild_id=CONTROL, bot_owner_ids={OPERATOR}, support_url=SUPPORT,
                auto_trial=True,
            ),
            get_guild=lambda gid: None,
        )
        self.premium = Premium(self.bot)  # type: ignore[arg-type]

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def start(self, guild=GUILD, user="9", now=NOW):
        return await self.db.start_trial(guild, user, tier="full", days=TRIAL_DAYS, now=now)

    async def grant(self, source="gift", tier="full", ends_days=None, guild=GUILD) -> int:
        return await self.db.add_entitlement(
            guild, tier, source, starts_at=ts(-1),
            ends_at=None if ends_days is None else ts(ends_days),
            granted_by="op", reason="test",
        )

    async def audit(self, guild=GUILD):
        async with self.db.conn.execute(
            "SELECT * FROM EntitlementAudit WHERE GuildId = ? ORDER BY Id", (guild,)
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


class StartTrialTests(_Case):
    async def test_start_creates_trial_entitlement_claim_and_audit(self) -> None:
        outcome, details = await self.start()
        self.assertEqual(outcome, "ok")
        [row] = await self.db.entitlements_for(GUILD)
        self.assertEqual((row["Source"], row["Tier"], row["GrantedBy"]), ("trial", "full", "9"))
        self.assertEqual(row["StartsAt"], ts(0))
        self.assertEqual(row["EndsAt"], ts(14))
        self.assertEqual(details, {"entitlement_id": row["Id"], "ends_at": ts(14)})
        self.assertEqual(await self.db.trial_claimed_at(GUILD), ts(0))
        [entry] = await self.audit()
        self.assertEqual((entry["Action"], entry["ActorId"]), ("trial", "9"))
        self.assertIn(f"#{row['Id']} full trial until {ts(14)}", entry["Detail"])
        status = await self.tiers.status(GUILD)
        self.assertIs(status.policy, FULL)
        self.assertEqual(status.describe(), f"Command — trial until {ts(14)[:10]}")

    async def test_second_attempt_is_refused(self) -> None:
        await self.start()
        outcome, details = await self.start(user="10")
        self.assertEqual(outcome, "claimed")
        self.assertEqual(details["claimed_at"], ts(0))
        self.assertEqual(len(await self.db.entitlements_for(GUILD)), 1)
        # Even after the trial has ended.
        self.assertEqual((await self.start(now=NOW + timedelta(days=30)))[0], "claimed")

    async def test_other_servers_are_independent(self) -> None:
        await self.start()
        self.assertEqual((await self.start(guild="999"))[0], "ok")

    async def test_refused_after_revoke(self) -> None:
        _, details = await self.start()
        self.assertTrue(await self.db.revoke_entitlement(
            details["entitlement_id"], at=ts(1), actor_id="op", reason="abuse"
        ))
        self.assertEqual((await self.start(now=NOW + timedelta(days=2)))[0], "claimed")

    async def test_refused_after_data_purge_and_re_add(self) -> None:
        await self.start()
        await self.db.delete_guild_data(GUILD)  # /data delete scope:server
        await self.db.schedule_purge(GUILD, ts(1))  # bot removed
        await self.db.purge_guild(GUILD)  # retention period over
        await self.db.cancel_purge(GUILD)  # bot added back
        self.assertEqual(await self.db.trial_claimed_at(GUILD), ts(0))
        self.assertEqual((await self.start(now=NOW + timedelta(days=60)))[0], "claimed")

    async def test_claim_alone_keeps_the_rule(self) -> None:
        # Even if the entitlement rows were gone, TrialClaim still counts.
        await self.start()
        await self.db.conn.execute("DELETE FROM GuildEntitlement")
        await self.db.conn.commit()
        self.assertEqual((await self.start())[0], "claimed")

    async def test_claim_is_not_server_data(self) -> None:
        # TrialClaim mustn't make a server look like it has data to purge.
        await self.start()
        self.assertNotIn(GUILD, await self.db.guilds_with_data())

    async def test_concurrent_starts_give_one_trial(self) -> None:
        results = await asyncio.gather(*(self.start(user=str(u)) for u in range(5)))
        self.assertEqual(sorted(r[0] for r in results), ["claimed"] * 4 + ["ok"])
        self.assertEqual(len(await self.db.entitlements_for(GUILD)), 1)

    async def test_claim_key_backstops_a_racing_writer(self) -> None:
        # Another connection claims between our check and our insert.
        await self.db.conn.execute(
            "INSERT INTO TrialClaim (GuildId, ClaimedAt) VALUES (?, ?)", (GUILD, ts(-1))
        )
        await self.db.conn.commit()
        original = self.db.conn.execute
        calls = {"n": 0}

        def skip_first_check(sql, *args, **kwargs):
            if "SELECT ClaimedAt FROM TrialClaim" in sql and calls["n"] == 0:
                calls["n"] += 1
                return original("SELECT NULL AS ClaimedAt WHERE 0")
            return original(sql, *args, **kwargs)

        self.db.conn.execute = skip_first_check  # type: ignore[method-assign]
        try:
            outcome, details = await self.start()
        finally:
            self.db.conn.execute = original  # type: ignore[method-assign]
        self.assertEqual((outcome, details["claimed_at"]), ("claimed", ts(-1)))
        self.assertEqual(await self.db.entitlements_for(GUILD), [])
        self.assertEqual(await self.audit(), [])


class OverlapTests(_Case):
    async def test_refused_with_active_command_plan(self) -> None:
        for source in ("discord", "stripe", "gift", "code"):
            with self.subTest(source=source):
                guild = f"5{len(source)}{source}"
                ent = await self.grant(source=source, guild=guild)
                outcome, details = await self.start(guild=guild)
                self.assertEqual(outcome, "covered")
                self.assertEqual(details["entitlement"]["Id"], ent)
                # The trial stays available for later.
                self.assertIsNone(await self.db.trial_claimed_at(guild))
                self.assertNotIn("trial", [a["Action"] for a in await self.audit(guild)])

    async def test_refusal_advises_and_keeps_trial(self) -> None:
        await self.grant(source="gift", ends_days=40)
        text = await self.premium.start_trial(GUILD, 9)
        self.assertIn(f"already has **Command — gifted until {ts(40)[:10]}**", text)
        self.assertIn("stays available", text)
        await self.grant(source="discord", guild="777")
        self.assertIn("Command — subscribed", await self.premium.start_trial("777", 9))

    async def test_allowed_over_alliance_plan(self) -> None:
        await self.grant(source="discord", tier="mid")
        text = await self.premium.start_trial(GUILD, 9)
        self.assertIn("trial has started", text)
        self.assertIn("goes back to **Alliance — subscribed**", text)
        self.assertIs((await self.tiers.status(GUILD)).policy, FULL)

    async def test_allowed_after_command_gift_ended(self) -> None:
        await self.db.add_entitlement(GUILD, "full", "gift", starts_at=ts(-60), ends_at=ts(-1),
                                      granted_by="op", reason="old")
        self.assertEqual((await self.start())[0], "ok")

    async def test_gift_ending_during_trial_is_not_the_plan_after(self) -> None:
        await self.grant(source="gift", tier="mid", ends_days=5)
        text = await self.premium.start_trial(GUILD, 9)
        self.assertIn("goes back to **Free**", text)


class PremiumTrialTests(_Case):
    async def test_start_message_and_cache(self) -> None:
        self.assertIs((await self.tiers.status(GUILD)).policy, FREE)  # cached
        text = await self.premium.start_trial(GUILD, 9)
        self.assertIn(f"runs until **{ts(14)[:10]}**", text)
        self.assertIn("goes back to **Free**", text)
        self.assertIn("Set a report channel with `/setup`", text)
        self.assertIn("aren't switched on yet", text)
        self.assertIs((await self.tiers.status(GUILD)).policy, FULL)
        await self.db.set_report_channel("999", CHANNEL)
        self.tiers.enforced = True
        text = await self.premium.start_trial("999", 9)
        self.assertIn("reminder in the report channel", text)
        self.assertNotIn("switched on", text)

    async def test_second_start_message(self) -> None:
        await self.premium.start_trial(GUILD, 9)
        await self.db.revoke_entitlement(1, at=ts(0), actor_id="op", reason="x")
        text = await self.premium.start_trial(GUILD, 9)
        self.assertIn(f"already used its free trial (started {ts(0)[:10]})", text)

    async def test_running_trial_after_reset(self) -> None:
        await self.premium.start_trial(GUILD, 9)
        await self.db.reset_trial(GUILD, actor_id="op", reason="x")
        text = await self.premium.start_trial(GUILD, 9)
        self.assertIn("free trial is already running", text)
        self.assertIn("14 days left", text)

    def test_plan_line_shows_trial_and_days_left(self) -> None:
        status = TierStatus(FULL, "trial", ts(14))
        text = format_premium(status, 3, enforced=False, now=NOW + timedelta(hours=1))
        self.assertIn(f"**Plan: Command — trial until {ts(14)[:10]}** (14 days left)", text)
        text = format_premium(status, 3, enforced=False, now=NOW + timedelta(days=12, hours=1))
        self.assertIn("(2 days left)", text)
        text = format_premium(status, 3, enforced=False, now=NOW + timedelta(days=13, hours=23))
        self.assertIn("(less than a day left)", text)
        gifted = format_premium(TierStatus(FULL, "gift", ts(14)), 3, enforced=False)
        self.assertNotIn("days left", gifted)

    def test_trial_note(self) -> None:
        free = TierStatus(FREE)
        admin = trial_note(free, None, can_manage=True, enforced=True)
        self.assertIn("try **Command** free for 14 days, once", admin)
        self.assertNotIn("switched on", admin)
        self.assertIn("keep it for later", trial_note(free, None, can_manage=True, enforced=False))
        self.assertIn("A server admin (Manage Server) can start",
                      trial_note(free, None, can_manage=False, enforced=True))
        self.assertEqual(trial_note(free, ts(-20), can_manage=True, enforced=True),
                         f"Free trial: used (started {ts(-20)[:10]}).")
        self.assertIsNone(trial_note(TierStatus(FULL, "trial", ts(3)), ts(-11), can_manage=True, enforced=True))
        self.assertIsNone(trial_note(TierStatus(FULL, "gift", None), None, can_manage=True, enforced=True))
        # Alliance can still try Command.
        self.assertIsNotNone(trial_note(TierStatus(MID, "gift", None), None, can_manage=True, enforced=True))

    def _interaction(self, *, manage=True):
        followup = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())))
        response = SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), edit_message=AsyncMock())
        return SimpleNamespace(
            guild_id=int(GUILD), user=SimpleNamespace(id=9), guild=SimpleNamespace(owner_id=OWNER),
            permissions=SimpleNamespace(manage_guild=manage),
            response=response, followup=followup,
        )

    async def test_premium_shows_button_only_to_admins_who_can_start(self) -> None:
        inter = self._interaction()
        await self.premium.premium.callback(self.premium, inter)
        kwargs = inter.followup.send.await_args.kwargs
        self.assertIsInstance(kwargs["view"], TrialView)
        self.assertIn("Free trial:", inter.followup.send.await_args.args[0])
        self.assertIn(f"[support server](<{SUPPORT}>)", inter.followup.send.await_args.args[0])

        inter = self._interaction(manage=False)
        await self.premium.premium.callback(self.premium, inter)
        self.assertNotIn("view", inter.followup.send.await_args.kwargs)
        self.assertIn("A server admin", inter.followup.send.await_args.args[0])

        await self.premium.start_trial(GUILD, 9)
        inter = self._interaction()
        await self.premium.premium.callback(self.premium, inter)
        self.assertNotIn("view", inter.followup.send.await_args.kwargs)
        self.assertIn("Command — trial until", inter.followup.send.await_args.args[0])

    async def test_button_starts_trial(self) -> None:
        view = TrialView(self.premium)
        inter = self._interaction()
        await view.start.callback(inter)
        inter.response.edit_message.assert_awaited_once_with(view=None)
        self.assertIn("trial has started", inter.followup.send.await_args.args[0])
        self.assertIs((await self.tiers.status(GUILD)).policy, FULL)

    async def test_button_needs_manage_server(self) -> None:
        view = TrialView(self.premium)
        inter = self._interaction(manage=False)
        await view.start.callback(inter)
        self.assertIn("Manage Server", inter.response.send_message.await_args.args[0])
        self.assertIsNone(await self.db.trial_claimed_at(GUILD))


class OwnerLimitTests(_Case):
    async def test_second_server_of_same_owner_is_refused(self) -> None:
        outcome, _ = await self.db.start_trial(
            GUILD, "9", tier="full", days=TRIAL_DAYS, now=NOW, owner_id="77"
        )
        self.assertEqual(outcome, "ok")
        outcome, details = await self.db.start_trial(
            "112", "9", tier="full", days=TRIAL_DAYS, now=NOW, owner_id="77"
        )
        self.assertEqual(outcome, "owner_claimed")
        self.assertEqual(details["guild_id"], GUILD)
        self.assertEqual(await self.db.entitlements_for("112"), [])
        self.assertIn("owner has already used", trial_refused_message(outcome, details))

    async def test_other_owner_and_unknown_owner_are_unaffected(self) -> None:
        await self.db.start_trial(GUILD, "9", tier="full", days=TRIAL_DAYS, now=NOW, owner_id="77")
        outcome, _ = await self.db.start_trial(
            "112", "9", tier="full", days=TRIAL_DAYS, now=NOW, owner_id="78"
        )
        self.assertEqual(outcome, "ok")
        outcome, _ = await self.start(guild="113")  # no owner: per-server rule only
        self.assertEqual(outcome, "ok")

    async def test_button_counts_the_owner(self) -> None:
        await self.premium.start_trial(GUILD, 9, owner_id=OWNER)
        text = await self.premium.start_trial("112", 9, owner_id=OWNER)
        self.assertIn("owner has already used", text)

    async def test_operators_are_exempt(self) -> None:
        await self.premium.start_trial(GUILD, 9, owner_id=OPERATOR)
        text = await self.premium.start_trial("112", 9, owner_id=OPERATOR)
        self.assertIn("trial has started", text)

    async def test_migration_adds_owner_column(self) -> None:
        path = Path(self._tmp.name) / "old.db"
        import aiosqlite
        async with aiosqlite.connect(path) as conn:
            await conn.execute("CREATE TABLE TrialClaim (GuildId TEXT PRIMARY KEY, ClaimedAt TEXT NOT NULL)")
            await conn.execute("INSERT INTO TrialClaim VALUES ('5', '2026-01-01 00:00:00')")
            await conn.commit()
        db = Database(path)
        await db.connect()
        try:
            self.assertIn("OwnerId", await db._table_columns("TrialClaim"))
            self.assertEqual(await db.trial_claimed_at("5"), "2026-01-01 00:00:00")
        finally:
            await db.close()


class AutoTrialTests(_Case):
    def _guild(self, gid=int(GUILD), owner=OWNER, *, system_ok=True):
        def perms(ok):
            return SimpleNamespace(view_channel=ok, send_messages=ok)
        system = SimpleNamespace(position=5, send=AsyncMock(),
                                 permissions_for=lambda me, ok=system_ok: perms(ok))
        general = SimpleNamespace(position=0, send=AsyncMock(), permissions_for=lambda me: perms(True))
        return SimpleNamespace(id=gid, owner_id=owner, me=object(), system_channel=system,
                               text_channels=[system, general])

    async def test_join_starts_trial_and_welcomes(self) -> None:
        self.tiers.enforced = True
        guild = self._guild()
        await self.premium.on_guild_join(guild)
        self.assertIs((await self.tiers.status(GUILD)).policy, FULL)
        [row] = await self.db.entitlements_for(GUILD)
        self.assertIsNone(row["GrantedBy"])
        text = guild.system_channel.send.await_args.args[0]
        self.assertIn("Thanks for adding", text)
        self.assertIn("goes back to **Free**", text)

    async def test_falls_back_to_first_channel_it_can_post_in(self) -> None:
        self.tiers.enforced = True
        guild = self._guild(system_ok=False)
        await self.premium.on_guild_join(guild)
        guild.system_channel.send.assert_not_awaited()
        guild.text_channels[1].send.assert_awaited_once()

    async def test_rejoin_gets_nothing(self) -> None:
        self.tiers.enforced = True
        await self.premium.on_guild_join(self._guild())
        guild = self._guild()
        await self.premium.on_guild_join(guild)
        guild.system_channel.send.assert_not_awaited()
        self.assertEqual(len(await self.db.entitlements_for(GUILD)), 1)

    async def test_owner_with_earlier_trial_gets_nothing(self) -> None:
        self.tiers.enforced = True
        await self.premium.on_guild_join(self._guild())
        guild = self._guild(gid=112)
        await self.premium.on_guild_join(guild)
        guild.system_channel.send.assert_not_awaited()
        self.assertIsNone(await self.db.trial_claimed_at("112"))

    async def test_nothing_while_not_enforced_or_switched_off(self) -> None:
        guild = self._guild()
        await self.premium.on_guild_join(guild)  # enforced is False
        self.tiers.enforced = True
        self.bot.settings.auto_trial = False
        await self.premium.on_guild_join(guild)
        self.bot.settings.auto_trial = True
        await self.premium.on_guild_join(self._guild(gid=CONTROL))
        guild.system_channel.send.assert_not_awaited()
        self.assertIsNone(await self.db.trial_claimed_at(GUILD))
        self.assertIsNone(await self.db.trial_claimed_at(str(CONTROL)))


class TrialReminderTests(_Case):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.now = NOW
        self.channel = SimpleNamespace(send=AsyncMock())
        self.guild = SimpleNamespace(
            id=int(GUILD), name="Wolves",
            get_channel=lambda cid: self.channel if str(cid) == CHANNEL else None,
        )
        self.operator = SimpleNamespace(send=AsyncMock())
        self.bot.guilds = [self.guild]
        self.bot.get_guild = lambda gid: self.guild if str(gid) == GUILD else None
        self.bot.get_user = lambda uid: self.operator
        self.reminders = GiftReminders(self.bot, clock=lambda: self.now)
        await self.db.set_report_channel(GUILD, CHANNEL)

    async def test_heads_up_three_days_before_end(self) -> None:
        await self.start()
        self.now = NOW + timedelta(days=10, hours=23)  # 3 days 1 hour left
        await self.reminders.run()
        self.channel.send.assert_not_awaited()
        self.now = NOW + timedelta(days=11, hours=1)
        await self.reminders.run()
        self.channel.send.assert_awaited_once()
        text = self.channel.send.await_args.args[0]
        self.assertIn(f"free **Command** trial ends on **{ts(14)[:10]}**", text)
        self.assertIn("will be on **Free**", text)
        self.assertIn("25 a week instead of 1,000", text)
        self.assertIn("last 4 weeks", text)
        self.assertIn("up to 1 channel(s)", text)
        self.assertIn("`/report player`", text)
        self.assertIn("Nothing is deleted", text)
        self.assertIn("nothing changes for now", text)  # not enforced
        self.assertLess(len(text), 2000)
        # Once only, and no operator DM for trials.
        await self.reminders.run()
        self.channel.send.assert_awaited_once()
        self.operator.send.assert_not_awaited()
        self.assertTrue(await self.db.reminder_sent(1, STAGE_SERVER, ts(14)))

    async def test_names_alliance_plan_after_trial(self) -> None:
        await self.grant(source="gift", tier="mid")
        await self.start()
        self.tiers.enforced = True
        self.now = NOW + timedelta(days=12)
        await self.reminders.run()
        text = self.channel.send.await_args.args[0]
        self.assertIn("will be on **Alliance — gifted**", text)
        self.assertIn("250 a week", text)
        self.assertIn("reports covering more than one channel", text)
        self.assertNotIn("`/report player`", text)
        self.assertNotIn("nothing changes for now", text)

    async def test_skipped_when_command_plan_outlasts_trial(self) -> None:
        await self.start()
        await self.grant(source="gift", ends_days=60)  # gifted during the trial
        self.now = NOW + timedelta(days=12)
        await self.reminders.run()
        self.channel.send.assert_not_awaited()

    async def test_skipped_when_paid_outlasts_trial(self) -> None:
        await self.start()
        await self.grant(source="discord", tier="mid")
        self.now = NOW + timedelta(days=12)
        await self.reminders.run()
        self.channel.send.assert_not_awaited()


class OpsTrialTests(_Case):
    def _interaction(self, *, user=OPERATOR, guild=CONTROL):
        return SimpleNamespace(
            user=SimpleNamespace(id=user), guild_id=guild,
            response=SimpleNamespace(send_message=AsyncMock()),
        )

    def _reply(self, inter) -> str:
        return inter.response.send_message.await_args.args[0]

    async def test_reset_lets_server_start_again_and_is_audited(self) -> None:
        ops = Ops(self.bot)  # type: ignore[arg-type]
        _, details = await self.start()
        await self.db.revoke_entitlement(details["entitlement_id"], at=ts(0), actor_id="op", reason="x")
        inter = self._interaction()
        await ops.trial_reset.callback(ops, inter, GUILD, "support ticket")
        self.assertIn(f"can start a free trial again from `/premium` (its last one started {ts(0)[:10]})",
                      self._reply(inter))
        self.assertIsNone(await self.db.trial_claimed_at(GUILD))
        entry = (await self.audit())[-1]
        self.assertEqual((entry["Action"], entry["ActorId"]), ("trial_reset", str(OPERATOR)))
        self.assertIn("support ticket", entry["Detail"])
        self.assertEqual((await self.start(now=NOW + timedelta(days=1)))[0], "ok")

    async def test_reset_accepts_autocomplete_label_and_notes_running_trial(self) -> None:
        guild = "123456789012345678"
        ops = Ops(self.bot)  # type: ignore[arg-type]
        await self.start(guild=guild)
        inter = self._interaction()
        await ops.trial_reset.callback(ops, inter, f"Wolves ({guild})", "retry")
        self.assertIn("is still running until", self._reply(inter))
        self.assertIsNone(await self.db.trial_claimed_at(guild))

    async def test_reset_without_trial_changes_nothing(self) -> None:
        ops = Ops(self.bot)  # type: ignore[arg-type]
        inter = self._interaction()
        await ops.trial_reset.callback(ops, inter, GUILD, "x")
        self.assertIn("hasn't used its free trial", self._reply(inter))
        self.assertEqual(await self.audit(), [])
        inter = self._interaction()
        await ops.trial_reset.callback(ops, inter, "not-an-id", "x")
        self.assertIn("isn't a server ID", self._reply(inter))

    async def test_reset_is_operator_only(self) -> None:
        ops = Ops(self.bot)  # type: ignore[arg-type]
        await self.start()
        names = {c.qualified_name for c in ops.walk_app_commands()}
        self.assertIn("ops trial-reset", names)
        self.assertIs(ops.trial_reset.binding, ops)  # so the cog's check runs
        self.assertIn("guild_id", ops.trial_reset._params)
        self.assertIsNotNone(ops.trial_reset._params["guild_id"].autocomplete)
        for user, guild in ((2, CONTROL), (OPERATOR, 555)):
            inter = self._interaction(user=user, guild=guild)
            self.assertFalse(await ops.trial_reset._check_can_run(inter))
        # The handler checks the operator again by itself.
        inter = self._interaction(user=2)
        with self.assertLogs("bot.cogs.ops", level="WARNING"):
            await ops.trial_reset.callback(ops, inter, GUILD, "x")
        self.assertIn("Only the bot operator", self._reply(inter))
        self.assertEqual(await self.db.trial_claimed_at(GUILD), ts(0))

    async def test_show_lists_trial(self) -> None:
        ops = Ops(self.bot)  # type: ignore[arg-type]
        await self.start()
        inter = self._interaction()
        await ops.show.callback(ops, inter, GUILD)
        text = self._reply(inter)
        self.assertIn(f"Free trial: started {ts(0)[:10]}", text)
        self.assertIn(f"Command (trial): active, until {ts(14)[:10]}", text)
        self.assertIn("trial by `9`", text)
        self.assertIn("Free trial: not used", format_show("2", None, [], [], 0, ts(0)))


if __name__ == "__main__":
    unittest.main()
