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


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_upsert_and_query(self) -> None:
        guild_a = "111"
        guild_b = "222"
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            await db.connect()
            await db.upsert_metric(guild_a, "2026-09-28", "PrincessPea", "HQLevel", 24)
            await db.upsert_metric(
                guild_a, "2026-09-28", "PrincessPea", "Power", 65_400_000
            )
            await db.upsert_metric(
                guild_a, "2026-09-28", "EnemyHelicopter", "VersusPoints", 112_257_938
            )
            await db.upsert_metric(
                guild_b, "2026-09-28", "OtherServerPlayer", "VersusPoints", 99.0
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


if __name__ == "__main__":
    unittest.main()
