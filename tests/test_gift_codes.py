"""Tests for gift codes: /ops code create/list/revoke and /redeem."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.cogs.ops import Ops, format_code_list  # noqa: E402
from bot.cogs.premium import Premium  # noqa: E402
from bot.db import Database  # noqa: E402
from bot.utils.gift_codes import (  # noqa: E402
    ALPHABET,
    AttemptLimiter,
    code_hint,
    generate_code,
    hash_code,
    normalize_code,
)
from bot.utils.tiers import FREE, FULL, MID, Tiers  # noqa: E402

TS = "%Y-%m-%d %H:%M:%S"
NOW = datetime.now(timezone.utc)


def ts(delta_days: float) -> str:
    return (NOW + timedelta(days=delta_days)).strftime(TS)


class CodeFormatTests(unittest.TestCase):
    def test_generated_codes_are_random_and_typeable(self) -> None:
        codes = {generate_code() for _ in range(200)}
        self.assertEqual(len(codes), 200)
        for code in list(codes)[:20]:
            self.assertRegex(code, r"^[0-9A-Z]{4}(-[0-9A-Z]{4}){3}$")
            self.assertFalse(set(code) & set("ILOU"))
            self.assertEqual(normalize_code(code), code.replace("-", ""))

    def test_normalize_forgives_case_spacing_and_lookalikes(self) -> None:
        self.assertEqual(normalize_code(" abcd efgh-jkmn pqr0 "), "ABCDEFGHJKMNPQR0")
        self.assertEqual(normalize_code("o1l1-0000-0000-0000"), "0111000000000000")
        self.assertIsNone(normalize_code("ABCD-EFGH"))  # too short
        self.assertIsNone(normalize_code("ABCD-EFGH-JKMN-PQRU"))  # U isn't used
        self.assertIsNone(normalize_code("ABCD-EFGH-JKMN-PQRS-T"))  # too long

    def test_hash_and_hint(self) -> None:
        raw = "ABCDEFGHJKMNPQRS"
        self.assertEqual(len(hash_code(raw)), 64)
        self.assertNotIn(raw, hash_code(raw))
        self.assertEqual(code_hint(raw), "PQRS")
        self.assertEqual(len(ALPHABET), 32)

    def test_limiter_blocks_after_failures_then_recovers(self) -> None:
        limiter = AttemptLimiter(max_failures=3, window=60)
        for t in (0, 1, 2):
            self.assertEqual(limiter.retry_after(7, now=t), 0)
            limiter.record_failure(7, now=t)
        self.assertAlmostEqual(limiter.retry_after(7, now=10), 50)
        self.assertEqual(limiter.retry_after(8, now=10), 0)  # per user
        self.assertEqual(limiter.retry_after(7, now=60), 0)  # oldest aged out


class _DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "t.db")
        await self.db.connect()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def make_code(self, *, tier="full", days=90, uses=1, expires=None, raw=None):
        raw = raw or normalize_code(generate_code())
        code_id = await self.db.create_gift_code(
            hash_code(raw), code_hint(raw), tier,
            duration_days=days, max_uses=uses, expires_at=expires,
            created_by="op", note="giveaway",
        )
        return code_id, raw

    async def redeem(self, raw, guild="g1", user="u1", now=NOW):
        return await self.db.redeem_gift_code(hash_code(raw), guild, user, now=now)

    async def audit(self, guild):
        async with self.db.conn.execute(
            "SELECT * FROM EntitlementAudit WHERE GuildId = ? ORDER BY Id", (guild,)
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


class GiftCodeDbTests(_DbCase):
    async def test_create_stores_only_hash_and_audits(self) -> None:
        code_id, raw = await self.make_code(expires=ts(30))
        row = await self.db.get_gift_code(code_id)
        self.assertEqual(row["CodeHash"], hash_code(raw))
        self.assertEqual((row["Hint"], row["Uses"], row["MaxUses"]), (raw[-4:], 0, 1))
        async with self.db.conn.execute("SELECT * FROM GiftCode") as cursor:
            dump = repr([tuple(r) for r in await cursor.fetchall()])
        self.assertNotIn(raw, dump)
        [entry] = await self.audit("")
        self.assertEqual((entry["Action"], entry["ActorId"]), ("code_create", "op"))
        self.assertIn(f"code #{code_id}", entry["Detail"])
        self.assertIn("giveaway", entry["Detail"])
        self.assertNotIn(raw, entry["Detail"])

    async def test_redeem_creates_code_entitlement(self) -> None:
        code_id, raw = await self.make_code(tier="mid", days=30)
        outcome, details = await self.redeem(raw)
        self.assertEqual(outcome, "ok")
        self.assertEqual(details["tier"], "mid")
        self.assertEqual(details["ends_at"], (NOW + timedelta(days=30)).strftime(TS))
        [ent] = await self.db.active_entitlements("g1", ts(0.001))
        self.assertEqual((ent["Id"], ent["Source"], ent["Tier"]), (details["entitlement_id"], "code", "mid"))
        self.assertEqual(ent["ExternalId"], hash_code(raw))
        self.assertEqual(ent["GrantedBy"], "u1")
        self.assertEqual((await self.db.get_gift_code(code_id))["Uses"], 1)
        [red] = await self.db.gift_code_redemptions(code_id)
        self.assertEqual((red["GuildId"], red["UserId"], red["EntitlementId"]),
                         ("g1", "u1", ent["Id"]))
        [entry] = await self.audit("g1")
        self.assertEqual((entry["Action"], entry["ActorId"]), ("redeem", "u1"))
        self.assertIn(f"#{ent['Id']} mid", entry["Detail"])
        self.assertIn(f"code #{code_id}", entry["Detail"])

    async def test_permanent_code(self) -> None:
        _, raw = await self.make_code(days=None)
        outcome, details = await self.redeem(raw)
        self.assertEqual((outcome, details["ends_at"]), ("ok", None))

    async def test_unknown_code(self) -> None:
        await self.make_code()
        self.assertEqual((await self.redeem("0000000000000000"))[0], "invalid")

    async def test_exhausted_code(self) -> None:
        code_id, raw = await self.make_code(uses=2)
        self.assertEqual((await self.redeem(raw, guild="g1"))[0], "ok")
        self.assertEqual((await self.redeem(raw, guild="g2"))[0], "ok")
        self.assertEqual((await self.redeem(raw, guild="g3"))[0], "used_up")
        self.assertEqual((await self.db.get_gift_code(code_id))["Uses"], 2)
        self.assertEqual(await self.db.active_entitlements("g3", ts(0.001)), [])
        self.assertEqual(await self.audit("g3"), [])

    async def test_expired_code(self) -> None:
        _, raw = await self.make_code(expires=ts(-0.01))
        self.assertEqual((await self.redeem(raw))[0], "expired")
        _, raw = await self.make_code(expires=ts(1))
        self.assertEqual((await self.redeem(raw))[0], "ok")
        self.assertEqual((await self.redeem(raw, guild="g2", now=NOW + timedelta(days=2)))[0], "expired")

    async def test_revoked_code(self) -> None:
        code_id, raw = await self.make_code(uses=5)
        self.assertEqual((await self.redeem(raw, guild="g1"))[0], "ok")
        newly, revoked = await self.db.revoke_gift_code(
            code_id, at=ts(0), actor_id="op", reason="leaked"
        )
        self.assertTrue(newly)
        self.assertEqual(revoked, [])
        self.assertEqual((await self.redeem(raw, guild="g2"))[0], "revoked")
        # The plan g1 already got is kept unless asked otherwise.
        self.assertEqual(len(await self.db.active_entitlements("g1", ts(0.001))), 1)
        actions = [a["Action"] for a in await self.audit("")]
        self.assertEqual(actions, ["code_create", "code_revoke"])
        self.assertIsNone(await self.db.revoke_gift_code(999, at=ts(0), actor_id="op", reason="x"))

    async def test_revoke_with_redeemed_plans(self) -> None:
        code_id, raw = await self.make_code(uses=5)
        _, d1 = await self.redeem(raw, guild="g1")
        _, d2 = await self.redeem(raw, guild="g2")
        other_id, other_raw = await self.make_code()
        await self.redeem(other_raw, guild="g1")
        newly, revoked = await self.db.revoke_gift_code(
            code_id, at=ts(0), actor_id="op", reason="abuse", revoke_redeemed=True
        )
        self.assertTrue(newly)
        self.assertEqual(sorted(revoked), [(d1["entitlement_id"], "g1"), (d2["entitlement_id"], "g2")])
        self.assertEqual(await self.db.active_entitlements("g2", ts(0.001)), [])
        # g1 keeps the plan from the other code.
        [left] = await self.db.active_entitlements("g1", ts(0.001))
        self.assertEqual(left["ExternalId"], hash_code(other_raw))
        self.assertEqual([a["Action"] for a in await self.audit("g2")], ["redeem", "revoke"])
        # Revoking again: code already revoked, nothing left to revoke.
        self.assertEqual(
            await self.db.revoke_gift_code(code_id, at=ts(0), actor_id="op", reason="x",
                                           revoke_redeemed=True),
            (False, []),
        )

    async def test_double_redemption_same_server(self) -> None:
        code_id, raw = await self.make_code(uses=3)
        self.assertEqual((await self.redeem(raw, user="u1"))[0], "ok")
        self.assertEqual((await self.redeem(raw, user="u2"))[0], "already")
        self.assertEqual((await self.db.get_gift_code(code_id))["Uses"], 1)
        self.assertEqual(len(await self.db.entitlements_for("g1")), 1)
        self.assertEqual([a["Action"] for a in await self.audit("g1")], ["redeem"])

    async def test_concurrent_redemptions_respect_limits(self) -> None:
        code_id, raw = await self.make_code(uses=3)
        guilds = [f"g{i % 5}" for i in range(20)]  # 5 servers, 4 tries each
        outcomes = await asyncio.gather(*(self.redeem(raw, guild=g) for g in guilds))
        ok = [g for g, (o, _) in zip(guilds, outcomes) if o == "ok"]
        self.assertEqual(len(ok), 3)
        self.assertEqual(len(set(ok)), 3)
        self.assertEqual((await self.db.get_gift_code(code_id))["Uses"], 3)
        self.assertEqual(len(await self.db.gift_code_redemptions(code_id)), 3)
        async with self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM GuildEntitlement WHERE Source = 'code'"
        ) as cursor:
            self.assertEqual((await cursor.fetchone())["n"], 3)

    async def test_redemption_key_backstops_a_racing_writer(self) -> None:
        # Simulate another connection having redeemed between our checks
        # and our insert: the primary key rejects it and nothing is kept.
        code_id, raw = await self.make_code(uses=3)
        await self.db.conn.execute(
            "INSERT INTO GiftCodeRedemption (CodeId, GuildId, UserId, EntitlementId, RedeemedAt) "
            "VALUES (?, 'g1', 'x', 0, ?)", (code_id, ts(0)),
        )
        await self.db.conn.commit()
        original = self.db.conn.execute
        statements: list[str] = []

        def skip_check(sql, *args, **kwargs):
            statements.append(sql.split()[0])
            if sql.startswith("SELECT 1 FROM GiftCodeRedemption"):
                sql = sql + " AND 0"
            return original(sql, *args, **kwargs)

        self.db.conn.execute = skip_check  # type: ignore[method-assign]
        try:
            self.assertEqual((await self.redeem(raw))[0], "already")
        finally:
            del self.db.conn.execute
        self.assertEqual(statements.count("INSERT"), 2)  # entitlement, then the rejected key
        self.assertEqual((await self.db.get_gift_code(code_id))["Uses"], 0)
        self.assertEqual(await self.db.entitlements_for("g1"), [])
        self.assertEqual(await self.audit("g1"), [])

    async def test_list_filters_inactive(self) -> None:
        live, _ = await self.make_code(uses=2)
        _, used = await self.make_code(uses=1)
        await self.redeem(used)
        await self.make_code(expires=ts(-1))
        gone, _ = await self.make_code()
        await self.db.revoke_gift_code(gone, at=ts(0), actor_id="op", reason="x")
        now = ts(0)
        self.assertEqual([r["Id"] for r in await self.db.gift_codes(now)], [live])
        rows = await self.db.gift_codes(now, include_inactive=True)
        self.assertEqual(len(rows), 4)
        text = format_code_list(rows, now, include_inactive=True)
        self.assertIn("used up", text)
        self.assertIn("expired", text)
        self.assertIn("revoked", text)
        self.assertIn(f"#{live} ", text)
        self.assertIn("0/2 used, redeemable — giveaway", text)


def _bot(db, tiers):
    return SimpleNamespace(
        db=db, tiers=tiers,
        settings=SimpleNamespace(control_guild_id=100, bot_owner_ids={1}),
    )


class RedeemCommandTests(_DbCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.tiers = Tiers(self.db, enforced=False)
        self.cog = Premium(_bot(self.db, self.tiers))  # type: ignore[arg-type]

    async def test_redeem_updates_plan_and_cache(self) -> None:
        _, raw = await self.make_code(tier="full", days=None)
        self.assertIs((await self.tiers.status("1")).policy, FREE)  # now cached
        text = await self.cog.redeem_code("1", 42, raw.lower())
        self.assertIn("**Command** plan with no end date", text)
        status = await self.tiers.status("1")
        self.assertIs(status.policy, FULL)
        self.assertEqual(status.describe(), "Command — from a gift code")

    async def test_redeem_lower_tier_mentions_current_plan(self) -> None:
        await self.db.add_entitlement("1", "full", "gift", starts_at=ts(-1), ends_at=None,
                                      granted_by="op", reason="x")
        _, raw = await self.make_code(tier="mid", days=30)
        text = await self.cog.redeem_code("1", 42, raw)
        self.assertIn("**Alliance** plan until", text)
        self.assertIn("Its plan is now **Command — gifted**", text)
        self.assertIs((await self.tiers.status("1")).policy, FULL)

    async def test_failures_explain_and_rate_limit(self) -> None:
        _, raw = await self.make_code()
        self.assertIn("doesn't look like a gift code", await self.cog.redeem_code("1", 42, "hello"))
        for _ in range(4):
            self.assertIn("isn't valid", await self.cog.redeem_code("1", 42, "0000-0000-0000-0000"))
        # Five failures: even the right code is refused for now.
        self.assertIn("Too many attempts", await self.cog.redeem_code("1", 42, raw))
        self.assertEqual(await self.db.entitlements_for("1"), [])
        # Another admin isn't affected.
        self.assertIn("Code redeemed", await self.cog.redeem_code("1", 43, raw))
        self.assertIs((await self.tiers.status("1")).policy, FULL)
        self.assertIn("already redeemed", await self.cog.redeem_code("1", 43, raw))

    def test_redeem_needs_manage_server(self) -> None:
        cmd = self.cog.redeem
        self.assertTrue(cmd.default_permissions.manage_guild)
        self.assertTrue(cmd.checks)


class _Response:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def is_done(self) -> bool:
        return bool(self.sent)

    async def send_message(self, text, ephemeral=False):
        self.sent.append(text)


class OpsCodeCommandTests(_DbCase):
    def _interaction(self, *, user=1, guild=100):
        return SimpleNamespace(
            user=SimpleNamespace(id=user), guild_id=guild, response=_Response()
        )

    async def test_create_list_revoke_flow(self) -> None:
        tiers = Tiers(self.db, enforced=False)
        cog = Ops(_bot(self.db, tiers))  # type: ignore[arg-type]
        inter = self._interaction()
        choice = SimpleNamespace
        await cog.code_create.callback(
            cog, inter, choice(name="Command", value="full"), choice(name="90 days", value="90"),
            2, 14, "partner",
        )
        reply = inter.response.sent[0]
        shown = reply.split("`")[1]
        raw = normalize_code(shown)
        self.assertIsNotNone(raw)
        self.assertIn("Copy it now", reply)
        [row] = await self.db.gift_codes(ts(0))
        self.assertEqual((row["Tier"], row["DurationDays"], row["MaxUses"]), ("full", 90, 2))
        self.assertEqual(row["ExpiresAt"][:10], ts(14)[:10])
        self.assertEqual(row["CodeHash"], hash_code(raw))

        premium = Premium(_bot(self.db, tiers))  # type: ignore[arg-type]
        self.assertIn("Code redeemed", await premium.redeem_code("5", 9, shown))

        inter = self._interaction()
        await cog.code_list.callback(cog, inter, False)
        self.assertIn("1/2 used", inter.response.sent[0])

        inter = self._interaction()
        await cog.code_revoke.callback(cog, inter, row["Id"], "ended", True)
        self.assertIn(f"Code #{row['Id']} revoked", inter.response.sent[0])
        self.assertIn("Also revoked #", inter.response.sent[0])
        self.assertIs((await tiers.status("5")).policy, FREE)  # cache invalidated
        self.assertIn("withdrawn", await premium.redeem_code("6", 9, shown))

    async def test_code_commands_are_operator_only(self) -> None:
        cog = Ops(_bot(self.db, Tiers(self.db, enforced=False)))  # type: ignore[arg-type]
        names = {c.qualified_name for c in cog.walk_app_commands()}
        self.assertTrue({"ops code create", "ops code list", "ops code revoke"} <= names)
        for cmd in (cog.code_create, cog.code_list, cog.code_revoke):
            self.assertIs(cmd.binding, cog)  # so the cog's interaction_check runs
        for user, guild in ((2, 100), (1, 555)):
            inter = self._interaction(user=user, guild=guild)
            self.assertFalse(await cog.code_create._check_can_run(inter))
            self.assertIn("Only the bot operator", inter.response.sent[0])
        self.assertTrue(await cog.code_create._check_can_run(self._interaction()))


if __name__ == "__main__":
    unittest.main()
