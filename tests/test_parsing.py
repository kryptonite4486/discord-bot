"""Lightweight unit tests for parsing helpers (no Discord/OCR required)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

# Allow `python tests/test_parsing.py` from repo root
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.utils.parsing import (  # noqa: E402
    chunk_fenced_md,
    format_value,
    parse_numeric_value,
    parse_pasted_rows,
    parse_week_start,
)
from bot.db import Database  # noqa: E402
from bot.db.database import LEGACY_GUILD_FALLBACK  # noqa: E402


class ParseNumericTests(unittest.TestCase):
    def test_plain_and_commas(self) -> None:
        self.assertEqual(parse_numeric_value("167040"), 167040.0)
        self.assertEqual(parse_numeric_value("167,040"), 167040.0)
        self.assertEqual(parse_numeric_value("112,257,938"), 112257938.0)

    def test_suffixes(self) -> None:
        self.assertAlmostEqual(parse_numeric_value("65.4M"), 65_400_000.0, places=3)
        self.assertAlmostEqual(parse_numeric_value("50.0M"), 50_000_000.0, places=3)
        self.assertAlmostEqual(parse_numeric_value("1.2K"), 1200.0, places=3)


class WeekTests(unittest.TestCase):
    def test_explicit_sunday(self) -> None:
        # 2026-09-20 is a Sunday — stays as week start
        self.assertEqual(parse_week_start("2026-09-20"), "2026-09-20")

    def test_normalize_to_sunday(self) -> None:
        # 2026-09-23 is Wednesday -> Sunday 2026-09-20
        self.assertEqual(parse_week_start("2026-09-23"), "2026-09-20")

    def test_current_and_last_sunday_boundaries(self) -> None:
        # Freeze "today" to Wednesday 2026-09-24 within the week of Sunday 2026-09-20
        class FixedDate(date):
            @classmethod
            def today(cls) -> date:
                return date(2026, 9, 24)

        with patch("bot.utils.parsing.date", FixedDate):
            self.assertEqual(parse_week_start("current"), "2026-09-20")
            self.assertEqual(parse_week_start("last"), "2026-09-13")
            self.assertEqual(parse_week_start(None), "2026-09-20")


class PasteTests(unittest.TestCase):
    def test_csv_versus(self) -> None:
        text = "player,points\n1Parzival1,167040\n93NAT93,161180\n"
        rows = parse_pasted_rows(text)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "1Parzival1")


class FormatTests(unittest.TestCase):
    def test_power_format(self) -> None:
        self.assertEqual(format_value("Power", 65_400_000), "65.4M")
        self.assertEqual(format_value("HQLevel", 24), "24")

    def test_signed_abbreviations(self) -> None:
        self.assertEqual(format_value("Power", -2_300_000), "-2.3M")
        self.assertEqual(format_value("VersusPoints", -12_500), "-12.5K")
        self.assertEqual(format_value("TechContribution", 1_200_000), "1.2M")
        self.assertEqual(format_value("Power", -500), "-500")


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_upsert_and_query(self) -> None:
        guild_a = "111"
        guild_b = "222"
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            await db.connect()
            await db.upsert_metric(
                guild_a, "2026-09-28", "PrincessPea", "HQLevel", 24, channel_id="c1"
            )
            await db.upsert_metric(
                guild_a,
                "2026-09-28",
                "PrincessPea",
                "Power",
                65_400_000,
                channel_id="c1",
            )
            await db.upsert_metric(
                guild_a,
                "2026-09-28",
                "EnemyHelicopter",
                "VersusPoints",
                112_257_938,
                channel_id="c1",
            )
            await db.upsert_metric(
                guild_b,
                "2026-09-28",
                "OtherServerPlayer",
                "VersusPoints",
                99.0,
                channel_id="c2",
            )

            week = await db.get_week_metrics(guild_a, "2026-09-28")
            self.assertEqual(len(week), 3)

            player = await db.get_player_metrics(guild_a, "princesspea")
            self.assertEqual(len(player), 2)

            board = await db.get_leaderboard(guild_a, "VersusPoints", "2026-09-28")
            self.assertEqual(board[0]["PlayerName"], "EnemyHelicopter")

            other = await db.get_week_metrics(guild_b, "2026-09-28")
            self.assertEqual(len(other), 1)
            self.assertEqual(other[0]["PlayerName"], "OtherServerPlayer")

            stats = await db.stats(guild_a)
            self.assertEqual(stats["rows"], 3)
            self.assertEqual(stats["guild_id"], guild_a)
            self.assertEqual(stats["unassigned_rows"], 0)
            await db.close()

    async def test_channel_phase1_includes_unassigned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "ch.db")
            await db.connect()
            # Historical unassigned row
            await db.upsert_metric(
                "g1", "2026-09-28", "Alice", "Power", 1000, channel_id=""
            )
            # New ingest into channel A
            await db.upsert_metric(
                "g1", "2026-09-28", "Bob", "Power", 2000, channel_id="ch-a"
            )
            # Other channel should not appear when scoped to ch-a (but unassigned does)
            await db.upsert_metric(
                "g1", "2026-09-28", "Carol", "Power", 3000, channel_id="ch-b"
            )

            phase1 = await db.get_week_metrics(
                "g1", "2026-09-28", channel_id="ch-a", include_unassigned=True
            )
            names = {r["PlayerName"] for r in phase1}
            self.assertEqual(names, {"Alice", "Bob"})

            strict = await db.get_week_metrics(
                "g1", "2026-09-28", channel_id="ch-a", include_unassigned=False
            )
            self.assertEqual([r["PlayerName"] for r in strict], ["Bob"])

            updated = await db.assign_channel("g1", "ch-a")
            self.assertEqual(updated, 1)
            self.assertEqual(await db.count_unassigned("g1"), 0)

            after = await db.get_week_metrics(
                "g1", "2026-09-28", channel_id="ch-a", include_unassigned=True
            )
            self.assertEqual({r["PlayerName"] for r in after}, {"Alice", "Bob"})
            # Idempotent
            self.assertEqual(await db.assign_channel("g1", "ch-a"), 0)
            await db.close()

    async def test_phase2_channel_excludes_unassigned_and_other_channels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "p2.db")
            await db.connect()
            await db.upsert_metric(
                "g1", "2026-09-28", "Alice", "Power", 1000, channel_id=""
            )
            await db.upsert_metric(
                "g1", "2026-09-28", "Bob", "Power", 2000, channel_id="ch-a"
            )
            await db.upsert_metric(
                "g1", "2026-09-28", "Carol", "Power", 3000, channel_id="ch-b"
            )

            # Phase 2 default: include_unassigned=False
            channel_a = await db.get_week_metrics(
                "g1", "2026-09-28", channel_id="ch-a"
            )
            self.assertEqual([r["PlayerName"] for r in channel_a], ["Bob"])

            # Server scope: all channels, no cross-player merge
            server = await db.get_week_metrics("g1", "2026-09-28", channel_id=None)
            self.assertEqual(
                {(r["PlayerName"], r["ChannelId"]) for r in server},
                {("Alice", ""), ("Bob", "ch-a"), ("Carol", "ch-b")},
            )
            self.assertEqual(await db.count_all_unassigned(), 1)
            await db.close()

    async def test_growth_does_not_merge_across_channels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "growth.db")
            await db.connect()
            await db.upsert_metric(
                "g1", "2026-09-14", "Same", "Power", 1000, channel_id="ch-a"
            )
            await db.upsert_metric(
                "g1", "2026-09-21", "Same", "Power", 2000, channel_id="ch-a"
            )
            await db.upsert_metric(
                "g1", "2026-09-14", "Same", "Power", 500, channel_id="ch-b"
            )
            await db.upsert_metric(
                "g1", "2026-09-21", "Same", "Power", 600, channel_id="ch-b"
            )
            rows = await db.get_growth_rates("g1", "Power", weeks=4, channel_id=None)
            self.assertEqual(len(rows), 2)
            by_ch = {r["ChannelId"]: r for r in rows}
            self.assertAlmostEqual(by_ch["ch-a"]["GrowthPct"], 100.0)
            self.assertAlmostEqual(by_ch["ch-b"]["GrowthPct"], 20.0)
            await db.close()

    async def test_migrate_add_channel_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "guild_only.db"
            import aiosqlite

            async with aiosqlite.connect(path) as raw:
                await raw.executescript(
                    """
                    CREATE TABLE WeeklyMetrics (
                        GuildId     TEXT    NOT NULL,
                        WeekStart   TEXT    NOT NULL,
                        PlayerName  TEXT    NOT NULL,
                        MetricType  TEXT    NOT NULL,
                        Value       REAL    NOT NULL,
                        UpdatedAt   TEXT    NOT NULL DEFAULT (datetime('now')),
                        PRIMARY KEY (GuildId, WeekStart, PlayerName, MetricType)
                    );
                    """
                )
                await raw.execute(
                    """
                    INSERT INTO WeeklyMetrics
                        (GuildId, WeekStart, PlayerName, MetricType, Value)
                    VALUES ('g1', '2026-09-28', 'Dana', 'HQLevel', 22)
                    """
                )
                await raw.commit()

            db = Database(path)
            await db.connect()
            rows = await db.get_week_metrics("g1", "2026-09-28")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["ChannelId"], "")
            self.assertEqual(await db.count_unassigned("g1"), 1)
            await db.close()

    async def test_migrate_with_legacy_guild_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.db"
            # Seed a pre-GuildId schema
            import aiosqlite

            async with aiosqlite.connect(path) as raw:
                await raw.executescript(
                    """
                    CREATE TABLE WeeklyMetrics (
                        WeekStart   TEXT    NOT NULL,
                        PlayerName  TEXT    NOT NULL,
                        MetricType  TEXT    NOT NULL,
                        Value       REAL    NOT NULL,
                        UpdatedAt   TEXT    NOT NULL DEFAULT (datetime('now')),
                        PRIMARY KEY (WeekStart, PlayerName, MetricType)
                    );
                    """
                )
                await raw.execute(
                    """
                    INSERT INTO WeeklyMetrics (WeekStart, PlayerName, MetricType, Value)
                    VALUES ('2026-09-28', 'Alice', 'Power', 1000)
                    """
                )
                await raw.commit()

            db = Database(path, legacy_guild_id="999888777")
            await db.connect()
            rows = await db.get_week_metrics("999888777", "2026-09-28")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["PlayerName"], "Alice")
            empty = await db.get_week_metrics(LEGACY_GUILD_FALLBACK, "2026-09-28")
            self.assertEqual(len(empty), 0)
            await db.close()

    async def test_migrate_without_legacy_guild_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy2.db"
            import aiosqlite

            async with aiosqlite.connect(path) as raw:
                await raw.executescript(
                    """
                    CREATE TABLE WeeklyMetrics (
                        WeekStart   TEXT    NOT NULL,
                        PlayerName  TEXT    NOT NULL,
                        MetricType  TEXT    NOT NULL,
                        Value       REAL    NOT NULL,
                        UpdatedAt   TEXT    NOT NULL DEFAULT (datetime('now')),
                        PRIMARY KEY (WeekStart, PlayerName, MetricType)
                    );
                    """
                )
                await raw.execute(
                    """
                    INSERT INTO WeeklyMetrics (WeekStart, PlayerName, MetricType, Value)
                    VALUES ('2026-09-28', 'Bob', 'HQLevel', 20)
                    """
                )
                await raw.commit()

            db = Database(path)
            await db.connect()
            rows = await db.get_week_metrics(LEGACY_GUILD_FALLBACK, "2026-09-28")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["PlayerName"], "Bob")
            await db.close()


class ChunkFenceTests(unittest.TestCase):
    def test_each_chunk_is_complete_fence(self) -> None:
        lines = [f"line {i} with week 2026-09-27" for i in range(80)]
        text = "## Leaderboard — TechContribution (2026-09-27)\n" + "\n".join(lines)
        chunks = chunk_fenced_md(text, limit=400)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertTrue(chunk.startswith("```md\n"), chunk[:20])
            self.assertTrue(chunk.endswith("\n```"), chunk[-10:])
            self.assertLessEqual(len(chunk), 400)

    def test_single_short_message(self) -> None:
        chunks = chunk_fenced_md("## Leaderboard — Tech (2026-09-27)\nok", limit=1900)
        self.assertEqual(len(chunks), 1)
        self.assertIn("2026-09-27", chunks[0])
        self.assertTrue(chunks[0].startswith("```md\n"))
        self.assertTrue(chunks[0].endswith("\n```"))


if __name__ == "__main__":
    unittest.main()
