"""Tests for tier history windows: reports hide weeks older than the plan allows."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from discord import app_commands  # noqa: E402

from bot.cogs.reports import ReportScope, Reports  # noqa: E402
from bot.db import Database  # noqa: E402
from bot.utils.tiers import (  # noqa: E402
    FREE,
    FULL,
    MID,
    Tiers,
    TierStatus,
    hidden_weeks_note,
    lowest_tier_showing,
)

TODAY = date(2026, 10, 7)  # a Wednesday; its week starts Sunday 2026-10-04
TS = "%Y-%m-%d %H:%M:%S"

# Weeks with data: this week and the 39 before it, newest first.
THIS_WEEK = datetime.now(timezone.utc).date()
THIS_WEEK -= timedelta(days=(THIS_WEEK.weekday() + 1) % 7)
WEEKS = [(THIS_WEEK - timedelta(weeks=i)).isoformat() for i in range(40)]


class PolicyTests(unittest.TestCase):
    def test_window_per_tier(self) -> None:
        cases = [
            (FREE, 4, "2026-09-13"),
            (MID, 26, "2026-04-12"),
            (FULL, None, None),
        ]
        for policy, weeks, min_week in cases:
            with self.subTest(policy.name):
                self.assertEqual(policy.history_weeks, weeks)
                self.assertEqual(policy.min_week(TODAY), min_week)
                # Any day of the same week gives the same window.
                self.assertEqual(policy.min_week(date(2026, 10, 4)), min_week)
                self.assertEqual(policy.min_week(date(2026, 10, 10)), min_week)

    def test_lowest_tier_showing(self) -> None:
        cases = [
            ("2026-10-04", FREE),
            ("2026-09-13", FREE),
            ("2026-09-06", MID),
            ("2026-04-12", MID),
            ("2026-04-05", FULL),
            ("2020-01-05", FULL),
        ]
        for week, tier in cases:
            with self.subTest(week):
                self.assertIs(lowest_tier_showing(week, TODAY), tier)

    def test_note(self) -> None:
        self.assertEqual(hidden_weeks_note([], TierStatus(FREE), TODAY), "")
        cases = [
            (["2026-09-06"], FREE, "1 older week hidden", "**Alliance** plan shows it"),
            (["2026-09-06", "2026-04-05"], FREE, "2 older weeks hidden", "**Command** plan shows them"),
            (["2026-04-05"], MID, "the **Alliance** plan shows the last 26 weeks", "**Command**"),
        ]
        for hidden, policy, *expected in cases:
            with self.subTest(hidden=hidden, policy=policy.name):
                note = hidden_weeks_note(hidden, TierStatus(policy), TODAY)
                for part in expected:
                    self.assertIn(part, note)
                self.assertIn("/premium", note)


class _DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "t.db")
        await self.db.connect()
        rows = [
            (week, player, metric, 1000.0 * (40 - i) + n)
            for i, week in enumerate(WEEKS)
            for n, player in enumerate(("alice", "bob"))
            for metric in ("Power", "VersusPoints", "TechContribution")
        ]
        await self.db.upsert_metrics("g1", rows, channel_id="c1")
        await self.db.upsert_metrics("g1", [(WEEKS[30], "carol", "Power", 5.0)], channel_id="c2")

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def grant(self, tier: str) -> None:
        now = datetime.now(timezone.utc)
        await self.db.add_entitlement(
            "g1", tier, "gift", starts_at=(now - timedelta(days=1)).strftime(TS),
            ends_at=None, granted_by="op", reason="test",
        )


class DatabaseWindowTests(_DbCase):
    async def test_every_report_query_respects_min_week(self) -> None:
        db = self.db
        queries = {
            "player": lambda **kw: db.get_player_metrics("g1", "alice", channel_id="c1", **kw),
            "trend": lambda **kw: db.get_trends("g1", "Power", weeks=26, channel_id="c1", **kw),
            "growth": lambda **kw: db.get_growth_rates("g1", "Power", weeks=26, channel_id="c1", **kw),
            "server-wide trend": lambda **kw: db.get_trends("g1", "Power", weeks=40, **kw),
        }
        # Oldest week each report shows: (player, trend and growth over 26 weeks).
        cases = [
            (FREE, WEEKS[3], WEEKS[3]),
            (MID, WEEKS[25], WEEKS[25]),
            (FULL, WEEKS[39], WEEKS[25]),
        ]
        for tier, oldest_player, oldest_trend in cases:
            min_week = tier.min_week()
            for name, query in queries.items():
                with self.subTest(tier=tier.name, query=name):
                    rows = await query(min_week=min_week)
                    weeks = [r.get("WeekStart") or r.get("FirstWeek") for r in rows]
                    if min_week is not None:
                        self.assertTrue(all(w >= min_week for w in weeks))
                    if name == "player":
                        self.assertEqual(min(weeks), oldest_player)
                    elif name != "server-wide trend":
                        self.assertEqual(min(weeks), oldest_trend)
        # Server-wide scope on Command reaches carol's week 30 in channel c2.
        rows = await db.get_trends("g1", "Power", weeks=40, min_week=FULL.min_week())
        self.assertIn(WEEKS[30], {r["WeekStart"] for r in rows})

    async def test_single_week_reports(self) -> None:
        cases = [
            (FREE, WEEKS[3], True),
            (FREE, WEEKS[4], False),
            (MID, WEEKS[25], True),
            (MID, WEEKS[26], False),
            (FULL, WEEKS[39], True),
        ]
        for tier, week, visible in cases:
            with self.subTest(tier=tier.name, week=week):
                kw = {"min_week": tier.min_week()}
                week_rows = await self.db.get_week_metrics("g1", week, **kw)
                board = await self.db.get_leaderboard("g1", "VersusPoints", week, **kw)
                self.assertEqual(bool(week_rows), visible)
                self.assertEqual(bool(board), visible)

    async def test_no_window_matches_unfiltered(self) -> None:
        """min_week=None is today's behaviour exactly."""
        db = self.db
        pairs = [
            (db.get_player_metrics, ("g1", "alice")),
            (db.get_week_metrics, ("g1", WEEKS[30])),
            (db.get_trends, ("g1", "Power", 26)),
            (db.get_growth_rates, ("g1", "Power", 26)),
            (db.get_leaderboard, ("g1", "Power")),
            (db.get_leaderboard, ("g1", "Power", WEEKS[39])),
        ]
        for fn, args in pairs:
            with self.subTest(fn.__name__, args=args):
                self.assertEqual(await fn(*args), await fn(*args, min_week=None))
                self.assertTrue(await fn(*args))

    async def test_hidden_weeks(self) -> None:
        min_week = FREE.min_week()
        db = self.db
        self.assertEqual(len(await db.hidden_weeks("g1", min_week, player_name="ALICE")), 36)
        self.assertEqual(
            await db.hidden_weeks("g1", min_week, metric_type="Power", recent=6),
            WEEKS[4:6],
        )
        self.assertEqual(await db.hidden_weeks("g1", min_week, week_start=WEEKS[10]), [WEEKS[10]])
        self.assertEqual(await db.hidden_weeks("g1", min_week, week_start=WEEKS[1]), [])
        self.assertEqual(
            await db.hidden_weeks("g1", min_week, week_start=WEEKS[30], channel_ids=["c2"]),
            [WEEKS[30]],
        )
        self.assertEqual(await db.hidden_weeks("g1", min_week, channel_ids=["nope"]), [])
        # Rows are hidden, not deleted.
        self.assertTrue(await db.get_week_metrics("g1", WEEKS[39]))


def _choice(value: str) -> app_commands.Choice[str]:
    return app_commands.Choice(name=value, value=value)


class ReportCommandTests(_DbCase):
    """Run each /report command with a plan and check what it sends."""

    async def run_report(self, command: str, tiers, *, server_wide: bool = False, **kwargs) -> str:
        bot = SimpleNamespace(db=self.db, tiers=tiers)
        cog = Reports(bot)  # type: ignore[arg-type]
        scope = ReportScope("g1", None if server_wide else ["c1"], server_wide, {})
        interaction = SimpleNamespace(
            guild=None,
            response=SimpleNamespace(defer=AsyncMock(), is_done=lambda: True),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        sent: list[str] = []

        async def send_text(_inter, text, **_files):
            sent.append(text)

        with patch.object(cog, "_resolve_scope_or_prompt", AsyncMock(return_value=scope)), \
                patch.object(cog, "_send_text", send_text):
            report = getattr(Reports, f"report_{command}")
            await report.callback(cog, interaction, scope=_choice("select"), **kwargs)
        self.assertEqual(len(sent), 1, interaction.followup.send.call_args_list)
        return sent[0]

    REPORTS = {
        "week (old)": ("week", {"week": WEEKS[10]}),
        "week (current)": ("week", {"week": "current"}),
        "versus (old)": ("versus", {"week": WEEKS[30]}),
        "tech (old)": ("tech", {"week": WEEKS[5]}),
        "leaderboard (latest)": ("leaderboard", {"metric": _choice("power")}),
        "leaderboard (old)": ("leaderboard", {"metric": _choice("power"), "week": WEEKS[20]}),
        "player": ("player", {"name": "alice", "chart": False}),
        "trend": ("trend", {"metric": _choice("power"), "weeks": 26}),
        "growth": ("growth", {"metric": _choice("power"), "weeks": 8}),
    }

    # Expected hidden-week count per report for Free and Alliance.
    HIDDEN = {
        "week (old)": (1, 0),
        "week (current)": (0, 0),
        "versus (old)": (1, 1),
        "tech (old)": (1, 0),
        "leaderboard (latest)": (0, 0),
        "leaderboard (old)": (1, 0),
        "player": (36, 14),
        "trend": (22, 0),
        "growth": (4, 0),
    }

    async def test_each_tier_enforced(self) -> None:
        for tier_key, column in (("free", 0), ("mid", 1), ("full", None)):
            if tier_key != "free":
                await self.grant(tier_key)
            tiers = Tiers(self.db, enforced=True)
            for label, (name, kwargs) in self.REPORTS.items():
                with self.subTest(tier=tier_key, report=label):
                    text = await self.run_report(name, tiers, **kwargs)
                    hidden = 0 if column is None else self.HIDDEN[label][column]
                    if hidden:
                        plural = "s" if hidden != 1 else ""
                        self.assertIn(f"🔒 {hidden} older week{plural} hidden", text)
                    else:
                        self.assertNotIn("🔒", text)

    async def test_server_wide_scope(self) -> None:
        tiers = Tiers(self.db, enforced=True)
        text = await self.run_report("versus", tiers, server_wide=True, week=WEEKS[30])
        self.assertIn("🔒 1 older week hidden", text)
        text = await self.run_report(
            "trend", tiers, server_wide=True, metric=_choice("power"), weeks=26
        )
        self.assertIn("🔒 22 older weeks hidden", text)
        self.assertNotIn(WEEKS[4], text)

    async def test_not_enforced_is_unchanged(self) -> None:
        """TIERS_ENFORCED off: Free shows everything, same as with no tier service."""
        shadow = Tiers(self.db, enforced=False)
        self.assertEqual(await shadow.history_window("g1"), (None, TierStatus(FREE)))
        with patch.object(self.db, "hidden_weeks", AsyncMock()) as hidden:
            for label, (name, kwargs) in self.REPORTS.items():
                with self.subTest(report=label):
                    text = await self.run_report(name, shadow, **kwargs)
                    self.assertEqual(text, await self.run_report(name, None, **kwargs))
                    self.assertNotIn("🔒", text)
            hidden.assert_not_called()
