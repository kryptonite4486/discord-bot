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
from bot.utils.plausibility import RULES, check_batch, evaluate  # noqa: E402


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


def _evaluate(metric: str, reference_metric: str, values, references):
    (rule,) = [r for r in RULES[metric] if r.reference_metric == reference_metric]
    return evaluate(rule, values, references)


# Real values from the SWag arena upload on 2026-10-04: legitimate Arena Power
# was 50-80% of total Power for these strong accounts (median 30% overall).
SWAG_ARENA = {"MohameD": 119_400_000, "Haydar": 105_600_000, "Pharaoh M": 124_300_000}
SWAG_POWER = {"mohamed": 148_900_000, "haydar": 145_100_000, "pharaoh m": 184_500_000}


class EvaluateTests(unittest.TestCase):
    def test_general_screenshot_filed_as_arena_flagged(self) -> None:
        # Values equal (or close) to total Power; C is a genuine arena value.
        values = {"A": 65_000_000, "B": 59_000_000, "C": 9_000_000}
        power = {"a": 65_000_000, "b": 60_000_000, "c": 90_000_000}
        alert = _evaluate("ArenaPower", "Power", values, power)
        self.assertIsNotNone(alert)
        self.assertIn("2 of 3", alert)
        self.assertIn("dataset:general", alert)

    def test_high_but_real_arena_share_quiet(self) -> None:
        self.assertIsNone(_evaluate("ArenaPower", "Power", SWAG_ARENA, SWAG_POWER))

    def test_arena_filed_as_general_compared_to_last_week_still_flagged(self) -> None:
        # Arena uploaded before this week's General: a mis-filed General's
        # values exceed last week's (lower) Power, so they still match.
        values = {"A": 66_000_000, "B": 61_000_000, "C": 91_000_000}
        last_week = {"a": 65_000_000, "b": 60_000_000, "c": 90_000_000}
        self.assertIsNotNone(_evaluate("ArenaPower", "Power", values, last_week))

    def test_arena_screenshot_filed_as_general_flagged(self) -> None:
        # "Power" values equal to the stored Arena Power.
        arena = {k.lower(): v for k, v in SWAG_ARENA.items()}
        alert = _evaluate("Power", "ArenaPower", SWAG_ARENA, arena)
        self.assertIsNotNone(alert)
        self.assertIn("dataset:arena", alert)

    def test_real_power_vs_arena_quiet(self) -> None:
        arena = {k.lower(): v for k, v in SWAG_ARENA.items()}
        real_power = {"MohameD": 148_900_000, "Haydar": 145_100_000, "Pharaoh M": 184_500_000}
        self.assertIsNone(_evaluate("Power", "ArenaPower", real_power, arena))

    def test_power_collapse_flagged(self) -> None:
        values = {"A": 6_500_000, "B": 6_000_000, "C": 5_000_000}
        previous = {"a": 65_000_000, "b": 60_000_000, "c": 50_000_000}
        alert = _evaluate("Power", "Power", values, previous)
        self.assertIsNotNone(alert)
        self.assertIn("dataset:arena", alert)

    def test_kills_going_down_flagged(self) -> None:
        values = {"A": 900_000, "B": 1_000_000}
        previous = {"a": 1_078_263, "b": 1_005_842}
        self.assertIsNotNone(_evaluate("Kills", "Kills", values, previous))

    def test_kills_jump_flagged_as_power(self) -> None:
        # A Power leaderboard filed as Kills: ~100x the previous kill totals.
        values = {"A": 112_257_938, "B": 65_521_967}
        previous = {"a": 1_078_263, "b": 1_005_842}
        alert = _evaluate("Kills", "Kills", values, previous)
        self.assertIsNotNone(alert)
        self.assertIn("dataset:power", alert)

    def test_kills_going_up_quiet(self) -> None:
        values = {"A": 1_100_000}
        previous = {"a": 1_078_263}
        self.assertIsNone(_evaluate("Kills", "Kills", values, previous))

    def test_single_outlier_in_large_upload_quiet(self) -> None:
        # One misread out of ten is below both the 3-row and 30% thresholds.
        values = {f"P{n}": 1_000_000 + n for n in range(10)}
        previous = {f"p{n}": 900_000 for n in range(10)}
        previous["p0"] = 2_000_000
        self.assertIsNone(_evaluate("Kills", "Kills", values, previous))

    def test_three_rows_flag_even_when_under_share(self) -> None:
        values = {f"P{n}": 1_000_000 for n in range(20)}
        previous = {f"p{n}": 900_000 for n in range(20)}
        for n in range(3):
            previous[f"p{n}"] = 2_000_000
        self.assertIsNotNone(_evaluate("Kills", "Kills", values, previous))

    def test_no_history_or_unchecked_metric(self) -> None:
        self.assertIsNone(_evaluate("Kills", "Kills", {"A": 1}, {}))
        self.assertNotIn("VersusPoints", RULES)


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
                alerts = await check_batch(
                    db, "g1", "c1", "2026-09-27", "Kills", lower
                )
                self.assertEqual(len(alerts), 1)

                higher = {"EnemyHelicopter": 1_100_000}
                self.assertEqual(
                    await check_batch(db, "g1", "c1", "2026-09-27", "Kills", higher), []
                )

                # Arena compares with Power from the same week.
                alerts = await check_batch(
                    db,
                    "g1",
                    "c1",
                    "2026-09-27",
                    "ArenaPower",
                    {"EnemyHelicopter": 110_000_000},
                )
                self.assertEqual(len(alerts), 1)
                # A real arena share, however high, passes.
                self.assertEqual(
                    await check_batch(
                        db, "g1", "c1", "2026-09-27", "ArenaPower",
                        {"EnemyHelicopter": 80_000_000},
                    ),
                    [],
                )

                # Power runs both of its checks: equal to stored Arena Power.
                await db.upsert_metrics(
                    "g1",
                    [("2026-09-27", "KBeezCONC", "ArenaPower", 30_000_000)],
                    channel_id="c1",
                )
                alerts = await check_batch(
                    db, "g1", "c1", "2026-09-27", "Power", {"KBeezCONC": 30_500_000}
                )
                self.assertEqual(len(alerts), 1)
                self.assertIn("dataset:arena", alerts[0])
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
