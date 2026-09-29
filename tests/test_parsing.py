"""Lightweight unit tests for parsing helpers (no Discord/OCR required)."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

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
    def test_explicit_monday(self) -> None:
        # 2026-09-28 is a Monday
        self.assertEqual(parse_week_start("2026-09-28"), "2026-09-28")

    def test_normalize_to_monday(self) -> None:
        # 2026-09-30 is Wednesday -> Monday 2026-09-28
        self.assertEqual(parse_week_start("2026-09-30"), "2026-09-28")


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
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            await db.connect()
            await db.upsert_metric("2026-09-28", "PrincessPea", "HQLevel", 24)
            await db.upsert_metric("2026-09-28", "PrincessPea", "Power", 65_400_000)
            await db.upsert_metric("2026-09-28", "EnemyHelicopter", "VersusPoints", 112_257_938)

            week = await db.get_week_metrics("2026-09-28")
            self.assertEqual(len(week), 3)

            player = await db.get_player_metrics("princesspea")
            self.assertEqual(len(player), 2)

            board = await db.get_leaderboard("VersusPoints", "2026-09-28")
            self.assertEqual(board[0]["PlayerName"], "EnemyHelicopter")

            stats = await db.stats()
            self.assertEqual(stats["rows"], 3)
            await db.close()


if __name__ == "__main__":
    unittest.main()
