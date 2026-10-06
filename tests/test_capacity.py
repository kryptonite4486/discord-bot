"""Tests for the OCR capacity estimate and /ops capacity (no Discord or vision server)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.cogs.ops import MAX_CAPACITY_ROWS, Ops, format_capacity_report  # noqa: E402
from bot.db import Database  # noqa: E402
from bot.utils.capacity import estimate_capacity, summarize_usage  # noqa: E402

QUOTAS = {"Free": 25, "Alliance": 250, "Command": 1000}

# 2026-09-28 is a Monday; two whole weeks to 2026-10-11.
MON = date(2026, 9, 28)
SUN2 = date(2026, 10, 11)


def _day(images: float, **kw: float) -> dict[str, float]:
    return {"ocr_images": images, **kw}


class EstimateCapacityTests(unittest.TestCase):
    def test_slot_week_and_servers_at_even_demand(self) -> None:
        # 10 s/image -> 8,640 images per slot-day, 60,480 per slot-week.
        e = estimate_capacity(
            seconds_per_image=10, slots=1, peak_day_share=1 / 7, quotas=QUOTAS, utilization=1.0
        )
        self.assertAlmostEqual(e.images_per_slot_week, 60_480)
        self.assertAlmostEqual(e.usable_images_per_day, 8_640)
        self.assertAlmostEqual(e.usable_images_per_week, 60_480)
        # Even demand: a Command server sends 1000/7 ≈ 142.9 images a day.
        self.assertEqual(e.servers_by_tier["Command"], (1000, 60))
        self.assertEqual(e.servers_by_tier["Alliance"], (250, 241))
        self.assertEqual(e.servers_by_tier["Free"], (25, 2419))

    def test_peak_share_and_utilization_shrink_the_fit(self) -> None:
        # 20 s/image, 70% of a day -> 3,024 usable images on the peak day.
        # Half the week on reset day -> a Command server sends 500 that day.
        e = estimate_capacity(
            seconds_per_image=20, slots=1, peak_day_share=0.5, quotas=QUOTAS, utilization=0.7
        )
        self.assertAlmostEqual(e.usable_images_per_day, 3_024)
        self.assertAlmostEqual(e.usable_images_per_week, 6_048)
        self.assertEqual(e.servers_by_tier["Command"], (1000, 6))
        self.assertEqual(e.servers_by_tier["Alliance"], (250, 24))
        self.assertEqual(e.servers_by_tier["Free"], (25, 241))

    def test_more_slots_scale_linearly(self) -> None:
        one = estimate_capacity(seconds_per_image=15, slots=1, peak_day_share=0.3, quotas=QUOTAS)
        two = estimate_capacity(seconds_per_image=15, slots=2, peak_day_share=0.3, quotas=QUOTAS)
        self.assertAlmostEqual(two.usable_images_per_day, 2 * one.usable_images_per_day)
        # images_per_slot_week is per slot, so it doesn't change.
        self.assertAlmostEqual(two.images_per_slot_week, one.images_per_slot_week)

    def test_rejects_nonsense(self) -> None:
        good = dict(seconds_per_image=10, slots=1, peak_day_share=0.5, quotas=QUOTAS)
        for bad in (
            {"seconds_per_image": 0},
            {"slots": 0},
            {"peak_day_share": 0},
            {"peak_day_share": 1.5},
            {"utilization": 0},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                estimate_capacity(**{**good, **bad})


class SummarizeUsageTests(unittest.TestCase):
    def test_totals_weekday_pattern_and_waits(self) -> None:
        daily = {
            "2026-09-28": _day(70, ocr_batches=2, ocr_seconds=700, ocr_wait_seconds=60, ocr_failed=7),
            "2026-10-01": _day(10, ocr_batches=1, ocr_seconds=100),
            "2026-10-05": _day(30, ocr_batches=3, ocr_seconds=300, ocr_wait_seconds=300),
        }
        s = summarize_usage(daily, MON, SUN2)
        self.assertEqual(s.days, 14)
        self.assertEqual((s.images, s.batches, s.failed), (110, 6, 7))
        self.assertAlmostEqual(s.seconds_per_image, 10.0)
        self.assertAlmostEqual(s.failure_rate, 7 / 110)
        self.assertAlmostEqual(s.avg_wait, 60.0)
        # Both Mondays (70 + 30) average 50; Thursday 10 + 0 averages 5.
        self.assertEqual(s.weekday_avg, (50.0, 0.0, 0.0, 5.0, 0.0, 0.0, 0.0))
        self.assertEqual(s.peak_weekday, "Mon")
        self.assertAlmostEqual(s.peak_day_share, 50 / 55)
        self.assertEqual(
            s.busiest_days,
            [("2026-09-28", 70, 30.0), ("2026-10-05", 30, 100.0), ("2026-10-01", 10, 0.0)],
        )
        self.assertEqual(s.peak_wait, ("2026-10-05", 100.0))

    def test_empty_window(self) -> None:
        s = summarize_usage({}, MON, SUN2)
        self.assertIsNone(s.seconds_per_image)
        self.assertIsNone(s.failure_rate)
        self.assertIsNone(s.avg_wait)
        self.assertIsNone(s.peak_day_share)
        self.assertIsNone(s.peak_weekday)
        self.assertIsNone(s.peak_wait)
        self.assertEqual(s.busiest_days, [])

    def test_last_before_first_rejected(self) -> None:
        with self.assertRaises(ValueError):
            summarize_usage({}, SUN2, MON)


class FormatCapacityReportTests(unittest.TestCase):
    def _report(self, by_guild=None, estimate=True, daily=None):
        daily = daily if daily is not None else {
            "2026-09-28": _day(100, ocr_batches=4, ocr_seconds=1200, ocr_wait_seconds=400, ocr_failed=5),
            "2026-10-03": _day(20, ocr_batches=2, ocr_seconds=240),
        }
        stats = summarize_usage(daily, MON, SUN2)
        est = None
        if estimate:
            est = estimate_capacity(
                seconds_per_image=stats.seconds_per_image or 12,
                slots=1,
                peak_day_share=stats.peak_day_share or 1.0,
                quotas=QUOTAS,
            )
        by_guild = by_guild if by_guild is not None else {
            "1": {"ocr_images": 100, "ocr_seconds": 1200, "ocr_failed": 5},
            "2": {"ocr_images": 20, "ocr_seconds": 240},
        }
        return format_capacity_report(
            stats, by_guild, {"1": "Alpha"}, est,
            seconds_source="measured", share_source="measured",
        )

    def test_sections(self) -> None:
        text = self._report()
        self.assertIn("**OCR capacity, last 14 day(s)**", text)
        self.assertIn("OCR **12.0s/image**", text)
        self.assertIn("failed **5** (4.2%)", text)
        self.assertIn("Queue wait: average **1m 06s** per batch", text)
        self.assertIn("worst day 2026-09-28 averaged **1m 40s**", text)
        # Per-server s/img; unknown names fall back to the ID.
        self.assertRegex(text, r"Alpha\s+100\s+12\.0\s+5\.0%")
        self.assertRegex(text, r"\n2\s+20\s+12\.0\s+0\.0%")
        self.assertIn("Mon 50  Tue 0  Wed 0  Thu 0  Fri 0  Sat 10  Sun 0", text)
        self.assertIn("Peak weekday: **Mon**, **83%**", text)
        self.assertIn("Busiest days: 2026-09-28 100 (wait 1m 40s), 2026-10-03 20 (wait 0s)", text)
        self.assertIn("One slot, flat out: **50,400** images/week", text)
        self.assertIn("Busiest day so far (2026-09-28) kept the slots **1%** busy.", text)
        self.assertIn("Free (25/wk) **", text)
        self.assertIn("Command (1,000/wk) **", text)
        self.assertLess(len(text), 2000)

    def test_many_servers_truncated_under_discord_limit(self) -> None:
        by_guild = {
            str(10**17 + i): {"ocr_images": 100 + i, "ocr_seconds": 1000, "ocr_failed": 3}
            for i in range(40)
        }
        text = self._report(by_guild=by_guild)
        self.assertIn(f"…and {40 - MAX_CAPACITY_ROWS} more server(s)", text)
        self.assertLess(len(text), 2000)

    def test_no_data_no_estimate(self) -> None:
        text = self._report(by_guild={}, estimate=False, daily={})
        self.assertIn("No OCR usage recorded in this window.", text)
        self.assertIn("scripts/benchmark_ocr.py", text)
        self.assertNotIn("**Estimate**", text)

    def test_given_seconds_with_no_data_uses_assumed_share(self) -> None:
        stats = summarize_usage({}, MON, SUN2)
        est = estimate_capacity(seconds_per_image=10, slots=1, peak_day_share=1.0, quotas=QUOTAS)
        text = format_capacity_report(
            stats, {}, {}, est, seconds_source="given", share_source="assumed"
        )
        self.assertIn("10.0s/image given", text)
        self.assertIn("peak day 100% of the week assumed", text)
        self.assertNotIn("Busiest day so far", text)


class CapacityCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        await self.db.connect()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def _run(self, **options) -> str:
        sent: list[str] = []
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=mock.AsyncMock()),
            followup=SimpleNamespace(send=mock.AsyncMock(side_effect=lambda t, **_: sent.append(t))),
        )
        cog = Ops.__new__(Ops)
        cog.bot = SimpleNamespace(
            db=self.db,
            guilds=[SimpleNamespace(id=1, name="Alpha")],
            settings=SimpleNamespace(ocr_max_concurrency=2),
        )
        await Ops.capacity.callback(cog, interaction, **options)
        return sent[0]

    async def test_reads_ledger_and_estimates(self) -> None:
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).date().isoformat()
        await self.db.add_usage(
            "1", today,
            {"ocr_batches": 2, "ocr_images": 40, "ocr_seconds": 400, "ocr_wait_seconds": 30, "ocr_failed": 2},
        )
        text = await self._run(days=7, utilization=50)
        self.assertIn("OCR **10.0s/image**", text)
        self.assertIn("2 slot(s)", text)
        self.assertIn("50% utilization", text)
        # Everything on one weekday, so that day is the whole week.
        self.assertIn("peak day 100% of the week measured", text)
        self.assertRegex(text, r"Alpha\s+40\s+10\.0\s+5\.0%")

    async def test_empty_ledger_with_given_seconds(self) -> None:
        text = await self._run(days=28, utilization=70, seconds_per_image=20.0)
        self.assertIn("20.0s/image given", text)
        self.assertIn("assumed", text)

    async def test_empty_ledger_without_seconds(self) -> None:
        text = await self._run(days=28, utilization=70, seconds_per_image=None)
        self.assertIn("No estimate", text)


if __name__ == "__main__":
    unittest.main()
