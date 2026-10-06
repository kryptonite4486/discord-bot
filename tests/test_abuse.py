"""Tests for the abuse controls: the per-user OCR rate limit, the Free-server
cap per person, where /ingest applies them, and /ops abuse."""

from __future__ import annotations

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

from bot.cogs.ingest import Ingest  # noqa: E402
from bot.cogs.ops import Ops, format_abuse_report  # noqa: E402
from bot.db import Database  # noqa: E402
from bot.utils.abuse import (  # noqa: E402
    FREE_LINK_DAYS,
    AbuseControls,
    IngestRateLimiter,
    ensure_ocr_allowed,
)
from bot.utils.rate_limit import SlidingWindowLimiter  # noqa: E402
from bot.utils.tiers import Tiers  # noqa: E402

TS = "%Y-%m-%d %H:%M:%S"
NOW = datetime.now(timezone.utc).replace(microsecond=0)
OPERATOR = 1
CONTROL = 100
OWNER = 500


def settings(**overrides):
    base = dict(
        bot_owner_ids=frozenset({OPERATOR}),
        control_guild_id=CONTROL,
        ingest_rate_window_minutes=10,
        ingest_rate_requests=6,
        ingest_rate_images=60,
        ingest_rate_admin_multiplier=3,
        free_servers_per_owner=3,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class SlidingWindowTests(unittest.TestCase):
    def test_weighted_amounts_wait_for_enough_to_age_out(self) -> None:
        w = SlidingWindowLimiter(10, window=60)
        w.record("k", 4, now=0)
        w.record("k", 4, now=10)
        self.assertEqual(w.retry_after("k", 5, amount=2), 0)
        # 8 used: 5 more need the first 4 to age out (at t=60).
        self.assertAlmostEqual(w.retry_after("k", 20, amount=5), 40)
        # 9 more need both batches gone (the second ages out at t=70).
        self.assertAlmostEqual(w.retry_after("k", 20, amount=9), 50)
        self.assertEqual(w.retry_after("k", 70, amount=10), 0)

    def test_amount_over_limit_fits_an_empty_window(self) -> None:
        w = SlidingWindowLimiter(10, window=60)
        self.assertEqual(w.retry_after("k", 0, amount=50), 0)
        w.record("k", 50, now=0)
        self.assertAlmostEqual(w.retry_after("k", 1, amount=1), 59)


class RateLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rl = IngestRateLimiter(requests=6, images=60, window=600, admin_multiplier=3)

    def test_requests_cap_then_reset_after_window(self) -> None:
        for t in range(6):
            self.assertEqual(self.rl.try_start("g", 7, 1, now=t * 10), 0)
        wait = self.rl.try_start("g", 7, 1, now=100)
        self.assertAlmostEqual(wait, 500)  # the first request ages out at t=600
        self.assertEqual(self.rl.try_start("g", 8, 1, now=100), 0)  # per user
        self.assertEqual(self.rl.try_start("h", 7, 1, now=100), 0)  # per server
        self.assertEqual(self.rl.try_start("g", 7, 1, now=600), 0)  # oldest aged out
        self.assertGreater(self.rl.try_start("g", 7, 1, now=601), 0)

    def test_images_cap_counts_screenshots(self) -> None:
        self.assertEqual(self.rl.try_start("g", 7, 50, now=0), 0)
        self.assertAlmostEqual(self.rl.try_start("g", 7, 20, now=60), 540)
        self.assertEqual(self.rl.try_start("g", 7, 10, now=60), 0)  # exactly 60
        self.assertGreater(self.rl.try_start("g", 7, 1, now=61), 0)
        self.assertEqual(self.rl.try_start("g", 7, 50, now=600), 0)

    def test_refused_and_early_checks_count_nothing(self) -> None:
        for t in range(6):
            self.rl.try_start("g", 7, 10, now=t)
        self.assertGreater(self.rl.try_start("g", 7, 1, now=10), 0)
        self.assertGreater(self.rl.try_start("g", 7, 1, now=11, record=False), 0)
        self.assertEqual(self.rl.requests.total(("g", 7), now=12), 6)
        self.assertEqual(self.rl.try_start("g", 9, 1, now=12, record=False), 0)
        self.assertEqual(self.rl.requests.total(("g", 9), now=12), 0)
        (top,) = self.rl.top(now=12)  # user 9 neither started nor was refused anything
        self.assertEqual((top.user_id, top.requests, top.images, top.refused), (7, 6, 60, 2))
        self.assertEqual(self.rl.top(now=10_000), [])

    def test_admins_get_the_multiplier(self) -> None:
        for t in range(18):
            self.assertEqual(self.rl.try_start("g", 7, 10, admin=True, now=t), 0)
        self.assertGreater(self.rl.try_start("g", 7, 1, admin=True, now=20), 0)
        self.assertEqual(self.rl.limits(admin=True), (18, 180))

    def test_zero_turns_a_cap_off(self) -> None:
        rl = IngestRateLimiter(requests=0, images=5, window=600)
        for t in range(20):
            self.assertEqual(rl.try_start("g", 7, 0, now=t), 0)
        self.assertEqual(rl.try_start("g", 7, 4, now=30), 0)
        self.assertGreater(rl.try_start("g", 7, 2, now=31), 0)


class _DbCase(unittest.IsolatedAsyncioTestCase):
    enforced = True

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "t.db")
        await self.db.connect()
        self.tiers = Tiers(self.db, enforced=self.enforced)
        self.controls = AbuseControls(self.db, self.tiers, settings())
        self.cap = self.controls.free_cap

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def use(self, guild, *, owner=OWNER, runner=9, now=NOW):
        """Run the cap check and, if allowed, record the use; return the check."""
        check = await self.cap.check(guild, owner_id=owner, runner_id=runner, now=now)
        if check.allowed:
            await self.cap.record(guild, owner_id=owner, runner_id=runner, now=now)
        return check

    async def grant(self, guild, source="gift", tier="mid"):
        await self.db.add_entitlement(
            guild, tier, source, starts_at=(NOW - timedelta(days=1)).strftime(TS),
            ends_at=None, granted_by="op", reason="test",
        )
        self.tiers.invalidate(guild)

    async def links(self):
        async with self.db.conn.execute("SELECT * FROM FreeOcrLink") as cursor:
            return [dict(r) for r in await cursor.fetchall()]


class FreeServerCapTests(_DbCase):
    async def test_fourth_free_server_of_an_owner_is_refused(self) -> None:
        for i, g in enumerate(("g1", "g2", "g3")):
            self.assertTrue((await self.use(g, runner=10 + i, now=NOW + timedelta(minutes=i))).allowed)
        check = await self.use("g4", runner=20)
        self.assertFalse(check.allowed)
        self.assertEqual((check.user_id, check.role, check.servers), (str(OWNER), "owner", 4))
        # The first three keep working: a server keeps its place by first use.
        for g in ("g1", "g2", "g3"):
            self.assertTrue((await self.use(g, runner=30)).allowed)
        self.assertFalse(any(r["GuildId"] == "g4" for r in await self.links()))

    async def test_runner_counts_across_servers_with_different_owners(self) -> None:
        for i, g in enumerate(("g1", "g2", "g3")):
            await self.use(g, owner=600 + i, runner=9, now=NOW + timedelta(minutes=i))
        check = await self.use("g4", owner=700, runner=9)
        self.assertFalse(check.allowed)
        self.assertEqual((check.user_id, check.role), ("9", "runner"))
        self.assertTrue((await self.use("g4", owner=700, runner=10)).allowed)

    async def test_paid_gifted_and_trial_servers_dont_count(self) -> None:
        for i, g in enumerate(("g1", "g2", "g3")):
            await self.use(g, now=NOW + timedelta(minutes=i))
        self.assertFalse((await self.use("g4")).allowed)
        for g, source in (("g1", "discord"), ("g2", "gift"), ("g3", "trial")):
            await self.grant(g, source=source, tier="full" if source == "trial" else "mid")
        for g in ("g4", "g5", "g6"):
            self.assertTrue((await self.use(g)).allowed, g)
        self.assertFalse((await self.use("g7")).allowed)
        # A paid server itself is never checked or recorded.
        await self.grant("g8", source="code")
        self.assertTrue((await self.use("g8")).allowed)
        self.assertFalse(any(r["GuildId"] == "g8" for r in await self.links()))

    async def test_lapsed_server_drops_out_after_the_link_period(self) -> None:
        for i, g in enumerate(("g1", "g2", "g3")):
            await self.use(g, now=NOW + timedelta(minutes=i))
        later = NOW + timedelta(days=FREE_LINK_DAYS, hours=1)
        await self.use("g2", now=later - timedelta(days=1))
        await self.use("g3", now=later - timedelta(days=1))
        self.assertTrue((await self.use("g4", now=later)).allowed)  # g1 lapsed
        self.assertFalse(any(r["GuildId"] == "g1" for r in await self.links()))

    async def test_operators_are_exempt_and_limit_zero_turns_it_off(self) -> None:
        for g in ("g1", "g2", "g3", "g4", "g5"):
            self.assertTrue((await self.use(g, owner=OPERATOR, runner=OPERATOR)).allowed)
        self.assertEqual(await self.links(), [])
        self.cap.limit = 0
        for g in ("g1", "g2", "g3", "g4", "g5"):
            self.assertTrue((await self.use(g)).allowed)
        self.assertEqual(await self.links(), [])

    async def test_whole_server_delete_and_purge_drop_its_links(self) -> None:
        await self.use("g1")
        await self.use("g2")
        await self.db.delete_guild_data("g1", channel_id="c1")
        self.assertEqual({r["GuildId"] for r in await self.links()}, {"g1", "g2"})
        await self.db.delete_guild_data("g1")
        await self.db.purge_guild("g2")
        self.assertEqual(await self.links(), [])

    async def test_over_cap_lists_people_with_too_many_free_servers(self) -> None:
        for i, g in enumerate(("g1", "g2", "g3")):
            await self.use(g, owner=600 + i, runner=9, now=NOW + timedelta(minutes=i))
        self.assertEqual(await self.cap.over_cap(), [])
        self.tiers.enforced = False
        await self.use("g4", owner=700, runner=9, now=NOW + timedelta(minutes=5))
        (person,) = await self.cap.over_cap()
        self.assertEqual(person.user_id, "9")
        self.assertEqual(list(person.servers), ["g1", "g2", "g3", "g4"])
        self.assertEqual(person.servers["g1"][1], {"runner"})


class CapNotEnforcedTests(_DbCase):
    enforced = False

    async def test_logs_and_allows_and_records(self) -> None:
        for i, g in enumerate(("g1", "g2", "g3")):
            await self.use(g, now=NOW + timedelta(minutes=i))
        with self.assertLogs("bot.utils.abuse", level="INFO") as logs:
            check = await self.use("g4")
        self.assertTrue(check.allowed)
        self.assertTrue(check.over)
        self.assertIn("Free-server cap (not enforced)", "\n".join(logs.output))
        self.assertIn("g4", {r["GuildId"] for r in await self.links()})


class CheckOcrTests(_DbCase):
    async def check(self, user=9, *, guild="g1", images=10, admin=False, start=True, owner=OWNER):
        return await self.controls.check_ocr(
            guild_id=guild, user_id=user, owner_id=owner, images=images, admin=admin, start=start
        )

    async def test_rate_limit_applies_even_with_tiers_not_enforced(self) -> None:
        self.tiers.enforced = False
        for _ in range(6):
            self.assertIsNone(await self.check())
        text = await self.check()
        self.assertIn("You can try again <t:", text)
        self.assertIn("6 uploads or 60 screenshots every 10 minutes", text)
        self.assertIn("`/add` and `/ingest text` still work", text)

    async def test_early_check_counts_nothing(self) -> None:
        for _ in range(10):
            self.assertIsNone(await self.check(start=False))
        self.assertEqual(self.controls.limiter.requests.total(("g1", 9)), 0)
        self.assertEqual(await self.links(), [])

    async def test_admin_higher_limit_and_operator_exempt(self) -> None:
        for _ in range(6):
            await self.check(user=9)
        self.assertIsNotNone(await self.check(user=9))
        for _ in range(18):
            self.assertIsNone(await self.check(user=8, admin=True, images=10))
        text = await self.check(user=8, admin=True)
        self.assertIn("18 uploads or 180 screenshots", text)
        for _ in range(50):
            self.assertIsNone(await self.check(user=OPERATOR, images=50))

    async def test_cap_refusal_message_and_counts_no_request(self) -> None:
        for g in ("g1", "g2", "g3"):
            self.assertIsNone(await self.check(guild=g))
        text = await self.check(guild="g4")
        self.assertIn("This server's owner already uses screenshot reading in 3 other", text)
        self.assertIn("/premium", text)
        self.assertEqual(self.controls.limiter.requests.total(("g4", 9)), 0)
        text = await self.check(guild="g5", owner=None)
        self.assertIn("You already use screenshot reading in 3 other", text)


def _interaction(bot, *, user=9, manage=False, done=True):
    return SimpleNamespace(
        client=bot, guild_id=1, user=SimpleNamespace(id=user, mention="<@9>"),
        guild=SimpleNamespace(owner_id=OWNER),
        permissions=SimpleNamespace(manage_guild=manage, administrator=False),
        response=SimpleNamespace(
            is_done=lambda: done, defer=AsyncMock(), send_message=AsyncMock()
        ),
        followup=SimpleNamespace(send=AsyncMock()),
        channel_id=2, channel=None,
    )


class IngestWiringTests(_DbCase):
    def setUp(self) -> None:
        self.bot = None

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot = SimpleNamespace(abuse=self.controls, tiers=None, db=self.db, settings=settings())

    async def test_ensure_replies_ephemerally_either_way(self) -> None:
        for _ in range(6):
            self.assertTrue(await ensure_ocr_allowed(_interaction(self.bot), 1))
        inter = _interaction(self.bot)
        self.assertFalse(await ensure_ocr_allowed(inter, 1))
        self.assertTrue(inter.followup.send.await_args.kwargs["ephemeral"])
        inter = _interaction(self.bot, done=False)
        self.assertFalse(await ensure_ocr_allowed(inter, 1, start=False))
        self.assertTrue(inter.response.send_message.await_args.kwargs["ephemeral"])
        # Without abuse controls (or outside a server) everything is allowed.
        self.assertTrue(await ensure_ocr_allowed(_interaction(SimpleNamespace()), 99))

    async def test_ingest_image_is_refused_before_download(self) -> None:
        cog = Ingest.__new__(Ingest)  # skip __init__: no vision client needed
        cog.bot = self.bot
        cog._default_week = AsyncMock()

        def attachment(i):
            return SimpleNamespace(id=i, filename=f"s{i}.png", content_type="image/png",
                                   read=AsyncMock(return_value=b""))

        choice = SimpleNamespace(value="versus")
        for _ in range(6):
            self.controls.limiter.try_start("1", 9, 1)
        images = [attachment(i) for i in range(3)]
        inter = _interaction(self.bot)
        await cog.ingest_image.callback(cog, inter, images[0], choice, None, images[1], images[2])
        self.assertIn("You can try again", inter.followup.send.await_args.args[0])
        cog._default_week.assert_not_awaited()
        for a in images:
            a.read.assert_not_awaited()

    async def test_ingest_batch_is_refused_before_arming(self) -> None:
        cog = Ingest.__new__(Ingest)
        cog.bot = self.bot
        cog._default_week = AsyncMock()
        for _ in range(6):
            self.controls.limiter.try_start("1", 9, 1)
        inter = _interaction(self.bot, done=False)
        await cog.ingest_batch.callback(cog, inter, SimpleNamespace(value="versus"), None, 10)
        self.assertIn("You can try again", inter.response.send_message.await_args.args[0])
        cog._default_week.assert_not_awaited()


class OpsAbuseTests(_DbCase):
    enforced = False

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot = SimpleNamespace(
            db=self.db, tiers=self.tiers, abuse=self.controls, settings=settings(),
            guilds=[SimpleNamespace(id=1, name="Wolves")], get_guild=lambda gid: None,
        )

    def _interaction(self, *, user=OPERATOR, guild=CONTROL):
        return SimpleNamespace(
            user=SimpleNamespace(id=user), guild_id=guild,
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_report_shows_top_users_and_owners_over_cap(self) -> None:
        for _ in range(7):
            await self.controls.check_ocr(guild_id="1", user_id=9, owner_id=OWNER,
                                          images=5, admin=False, start=True)
        for i, g in enumerate(("2", "3", "4")):
            await self.use(g, runner=20, now=NOW + timedelta(hours=1 + i))
        ops = Ops(self.bot)  # type: ignore[arg-type]
        inter = self._interaction()
        await ops.abuse.callback(ops, inter)
        text = inter.followup.send.await_args.args[0]
        self.assertIn("6 requests, 60 images; ×3 for Manage Server", text)
        self.assertIn("<@9> in Wolves: 6 request(s), 30 image(s), **1 refused**", text)
        self.assertIn("not enforced, logging only", text)
        self.assertIn(f"<@{OWNER}> (`{OWNER}`): **4** — Wolves (owner, since", text)
        self.assertIn("4 (owner, since", text)
        self.assertEqual(inter.followup.send.await_args.kwargs["allowed_mentions"].users, False)

    async def test_abuse_is_operator_only(self) -> None:
        ops = Ops(self.bot)  # type: ignore[arg-type]
        self.assertIn("ops abuse", {c.qualified_name for c in ops.walk_app_commands()})
        self.assertIs(ops.abuse.binding, ops)
        for user, guild in ((2, CONTROL), (OPERATOR, 555)):
            inter = self._interaction(user=user, guild=guild)
            self.assertFalse(await ops.abuse._check_can_run(inter))
        inter = self._interaction(user=2)
        with self.assertLogs("bot.cogs.ops", level="WARNING"):
            await ops.abuse.callback(ops, inter)
        self.assertIn("Only the bot operator", inter.response.send_message.await_args.args[0])
        inter.followup.send.assert_not_awaited()

    def test_empty_report_and_cap_off(self) -> None:
        text = format_abuse_report([], [], {}, window_minutes=10, limits=(0, 0),
                                   admin_multiplier=3, cap=0, enforced=False)
        self.assertIn("(off;", text)
        self.assertIn("No OCR requests in the window.", text)
        self.assertIn("Free-server cap** — off", text)
        text = format_abuse_report([], [], {}, window_minutes=10, limits=(6, 60),
                                   admin_multiplier=3, cap=3, enforced=True)
        self.assertIn("Nobody uses OCR in more than 3 Free servers.", text)
        self.assertIn("(enforced)", text)


if __name__ == "__main__":
    unittest.main()
