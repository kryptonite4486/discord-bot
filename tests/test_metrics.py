"""Tests for the Arena Power / Kills metrics and wrong-dataset checks."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

# Allow `python tests/test_metrics.py` from repo root
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import (  # noqa: E402
    DATASET_KINDS,
    LEADERBOARD_DATASETS,
    MEMBER_CARD_DATASETS,
    METRIC_TYPES,
    resolve_metric,
)
from bot.ocr.pipeline import ExtractedMetric, relabel_member_cards  # noqa: E402
from bot.db import Database  # noqa: E402
from bot.utils.parsing import format_value  # noqa: E402
from bot.utils.plausibility import check_batch, evaluate  # noqa: E402


class MetricConfigTests(unittest.TestCase):
    def test_aliases(self) -> None:
        for alias in ("arena", "Arena Power", "arena_power", "ap"):
            self.assertEqual(resolve_metric(alias), "ArenaPower")
        for alias in ("kills", "Kills", "kill", "k"):
            self.assertEqual(resolve_metric(alias), "Kills")

    def test_datasets_map_to_known_metrics(self) -> None:
        self.assertNotIn("auto", DATASET_KINDS)
        self.assertEqual(
            set(DATASET_KINDS),
            {"versus", "tech", "power", "kills", "general", "arena"},
        )
        for metric in (*LEADERBOARD_DATASETS.values(), *MEMBER_CARD_DATASETS.values()):
            self.assertIn(metric, METRIC_TYPES)
        # /ingest text resolves leaderboard datasets through the alias table.
        for kind, metric in LEADERBOARD_DATASETS.items():
            self.assertEqual(resolve_metric(kind), metric)

    def test_relabel_member_cards(self) -> None:
        cards = [
            ExtractedMetric("A", "HQLevel", 24),
            ExtractedMetric("A", "Power", 8_300_000),
        ]
        self.assertEqual(relabel_member_cards(cards, "general"), cards)
        self.assertEqual(
            relabel_member_cards(cards, "arena"),
            [ExtractedMetric("A", "ArenaPower", 8_300_000)],
        )

    def test_format(self) -> None:
        self.assertEqual(format_value("Kills", 1_078_263), "1.1M")
        self.assertEqual(format_value("ArenaPower", 11_200_000), "11.2M")


class EvaluateTests(unittest.TestCase):
    def test_arena_near_total_power_flagged(self) -> None:
        values = {"A": 60_000_000, "B": 55_000_000, "C": 9_000_000}
        power = {"a": 65_000_000, "b": 60_000_000, "c": 90_000_000}
        alert = evaluate("ArenaPower", values, power)
        self.assertIsNotNone(alert)
        self.assertIn("2 of 3", alert)
        self.assertIn("dataset:power", alert)

    def test_realistic_arena_quiet(self) -> None:
        values = {"A": 6_500_000, "B": 6_000_000}
        power = {"a": 65_000_000, "b": 60_000_000}
        self.assertIsNone(evaluate("ArenaPower", values, power))

    def test_power_collapse_flagged(self) -> None:
        values = {"A": 6_500_000, "B": 6_000_000, "C": 5_000_000}
        previous = {"a": 65_000_000, "b": 60_000_000, "c": 50_000_000}
        alert = evaluate("Power", values, previous)
        self.assertIsNotNone(alert)
        self.assertIn("dataset:arena", alert)

    def test_kills_going_down_flagged(self) -> None:
        values = {"A": 900_000, "B": 1_000_000}
        previous = {"a": 1_078_263, "b": 1_005_842}
        self.assertIsNotNone(evaluate("Kills", values, previous))

    def test_kills_jump_flagged_as_power(self) -> None:
        # A Power leaderboard filed as Kills: ~100x the previous kill totals.
        values = {"A": 112_257_938, "B": 65_521_967}
        previous = {"a": 1_078_263, "b": 1_005_842}
        alert = evaluate("Kills", values, previous)
        self.assertIsNotNone(alert)
        self.assertIn("dataset:power", alert)

    def test_kills_going_up_quiet(self) -> None:
        values = {"A": 1_100_000}
        previous = {"a": 1_078_263}
        self.assertIsNone(evaluate("Kills", values, previous))

    def test_single_outlier_in_large_upload_quiet(self) -> None:
        # One misread out of ten is below both the 3-row and 30% thresholds.
        values = {f"P{n}": 1_000_000 + n for n in range(10)}
        previous = {f"p{n}": 900_000 for n in range(10)}
        previous["p0"] = 2_000_000
        self.assertIsNone(evaluate("Kills", values, previous))

    def test_three_rows_flag_even_when_under_share(self) -> None:
        values = {f"P{n}": 1_000_000 for n in range(20)}
        previous = {f"p{n}": 900_000 for n in range(20)}
        for n in range(3):
            previous[f"p{n}"] = 2_000_000
        self.assertIsNotNone(evaluate("Kills", values, previous))

    def test_no_history_or_unchecked_metric(self) -> None:
        self.assertIsNone(evaluate("Kills", {"A": 1}, {}))
        self.assertIsNone(evaluate("VersusPoints", {"A": 1}, {"a": 10}))


class CheckBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_channel_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            await db.connect()
            try:
                await db.upsert_metrics(
                    "g1",
                    [
                        ("2026-09-20", "EnemyHelicopter", "Kills", 1_078_263),
                        ("2026-09-20", "KBeezCONC", "Kills", 1_005_842),
                        ("2026-09-27", "EnemyHelicopter", "Power", 112_000_000),
                    ],
                    channel_id="c1",
                )
                # Same players in another channel must not be compared.
                await db.upsert_metrics(
                    "g1",
                    [("2026-09-20", "EnemyHelicopter", "Kills", 9_999_999)],
                    channel_id="c2",
                )

                lower = {"enemyhelicopter": 900_000, "kbeezconc": 1_000_000}
                alert = await check_batch(
                    db, "g1", "c1", "2026-09-27", "Kills", lower
                )
                self.assertIsNotNone(alert)

                higher = {"EnemyHelicopter": 1_100_000}
                self.assertIsNone(
                    await check_batch(db, "g1", "c1", "2026-09-27", "Kills", higher)
                )

                # Arena compares with Power from the same week.
                alert = await check_batch(
                    db,
                    "g1",
                    "c1",
                    "2026-09-27",
                    "ArenaPower",
                    {"EnemyHelicopter": 100_000_000},
                )
                self.assertIsNotNone(alert)
            finally:
                await db.close()

    async def test_latest_values_respects_week_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.db")
            await db.connect()
            try:
                await db.upsert_metrics(
                    "g1",
                    [
                        ("2026-09-13", "A", "Kills", 100),
                        ("2026-09-20", "A", "Kills", 200),
                        ("2026-09-27", "A", "Kills", 300),
                    ],
                    channel_id="c1",
                )
                before = await db.get_latest_values(
                    "g1",
                    "Kills",
                    ["a"],
                    channel_id="c1",
                    week_start="2026-09-27",
                    include_week=False,
                )
                self.assertEqual(before, {"a": 200.0})
                through = await db.get_latest_values(
                    "g1",
                    "Kills",
                    ["A"],
                    channel_id="c1",
                    week_start="2026-09-27",
                    include_week=True,
                )
                self.assertEqual(through, {"a": 300.0})
            finally:
                await db.close()


if __name__ == "__main__":
    unittest.main()
