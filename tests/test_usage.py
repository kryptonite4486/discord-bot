"""Tests for OCR usage metering (no Discord or vision server required)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.cogs.ingest import Ingest  # noqa: E402
from bot.cogs.ops import MAX_USAGE_ROWS, format_usage_report  # noqa: E402
from bot.db import Database  # noqa: E402


class UsageLedgerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        await self.db.connect()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def test_amounts_accumulate_per_day(self) -> None:
        await self.db.add_usage("g1", "2026-10-04", {"ocr_images": 10, "ocr_batches": 1})
        await self.db.add_usage("g1", "2026-10-04", {"ocr_images": 5, "ocr_batches": 1})
        await self.db.add_usage("g1", "2026-10-05", {"ocr_images": 3, "ocr_failed": 1})
        await self.db.add_usage("g2", "2026-10-05", {"ocr_images": 7})

        totals = await self.db.usage_by_guild("2026-10-04")
        self.assertEqual(totals["g1"], {"ocr_images": 18, "ocr_batches": 2, "ocr_failed": 1})
        self.assertEqual(totals["g2"], {"ocr_images": 7})

        self.assertEqual(
            await self.db.usage_by_day("2026-10-04", "ocr_images"),
            [("2026-10-04", 15), ("2026-10-05", 10)],
        )

    async def test_since_day_excludes_older_rows(self) -> None:
        await self.db.add_usage("g1", "2026-09-01", {"ocr_images": 99})
        await self.db.add_usage("g1", "2026-10-05", {"ocr_images": 1})
        totals = await self.db.usage_by_guild("2026-10-01")
        self.assertEqual(totals["g1"]["ocr_images"], 1)

    async def test_zero_amounts_write_nothing(self) -> None:
        await self.db.add_usage("g1", "2026-10-05", {"ocr_failed": 0})
        self.assertEqual(await self.db.usage_by_guild("2026-01-01"), {})

    async def test_unknown_kind_rejected(self) -> None:
        with self.assertRaises(ValueError):
            await self.db.add_usage("g1", "2026-10-05", {"tokens": 5})


class RecordUsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_records_images_time_and_wait(self) -> None:
        calls = []

        async def add_usage(guild_id, day, amounts):
            calls.append((guild_id, day, amounts))

        cog = Ingest.__new__(Ingest)  # skip __init__: no vision client needed
        cog.bot = SimpleNamespace(db=SimpleNamespace(add_usage=add_usage))
        await cog._record_usage(
            "g1", queued_at=0.0, started_at=0.0, images=4, failed=1
        )
        (guild_id, day, amounts), = calls
        self.assertEqual(guild_id, "g1")
        self.assertRegex(day, r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(amounts["ocr_batches"], 1)
        self.assertEqual(amounts["ocr_images"], 4)
        self.assertEqual(amounts["ocr_failed"], 1)
        self.assertEqual(amounts["ocr_wait_seconds"], 0.0)
        self.assertGreater(amounts["ocr_seconds"], 0)

    async def test_recording_failure_does_not_raise(self) -> None:
        async def add_usage(*_):
            raise RuntimeError("disk full")

        cog = Ingest.__new__(Ingest)
        cog.bot = SimpleNamespace(db=SimpleNamespace(add_usage=add_usage))
        with self.assertLogs("bot.cogs.ingest", level="ERROR"):
            await cog._record_usage(
                "g1", queued_at=0.0, started_at=None, images=0, failed=0
            )


class UsageReportTests(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertIn("No OCR usage", format_usage_report(7, {}, [], {}))

    def test_rows_totals_and_peak(self) -> None:
        by_guild = {
            "1": {"ocr_batches": 2, "ocr_images": 20, "ocr_seconds": 200, "ocr_wait_seconds": 30},
            "2": {"ocr_batches": 1, "ocr_images": 5, "ocr_failed": 1, "ocr_seconds": 60},
        }
        text = format_usage_report(
            7, by_guild, [("2026-10-04", 5), ("2026-10-05", 20)], {"1": "SWag"}
        )
        lines = text.splitlines()
        swag = next(line for line in lines if line.startswith("SWag"))
        self.assertIn("10.0s", swag)  # 200 s / 20 images
        self.assertIn("15s", swag)  # 30 s wait / 2 batches
        self.assertTrue(lines.index(swag) < next(i for i, l in enumerate(lines) if l.startswith("2 ")))
        total = next(line for line in lines if line.startswith("Total"))
        self.assertIn(" 25 ", total)
        self.assertIn("Busiest day: **2026-10-05** with **20** images", text)

    def test_many_servers_stay_under_message_limit(self) -> None:
        by_guild = {
            str(i): {"ocr_batches": 1, "ocr_images": i, "ocr_seconds": i}
            for i in range(1, 60)
        }
        days = [(f"2026-10-{d:02d}", 1) for d in range(1, 15)]
        text = format_usage_report(14, by_guild, days, {})
        self.assertLess(len(text), 2000)
        self.assertIn(f"…and {59 - MAX_USAGE_ROWS} more server(s)", text)


if __name__ == "__main__":
    unittest.main()
