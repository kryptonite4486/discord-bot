"""Custom emoji assets, lookup, and the report headlines that use them."""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from bot.reporting import formatters  # noqa: E402
from bot.utils import emojis  # noqa: E402

EMOJI_DIR = ROOT / "assets" / "emojis"
FAKE = {name: f"<:{name}:1>" for name in (
    "kills", "power", "report", "growth_up", "growth_down", "place_1", "place_2", "place_3",
)}


def _lb_rows():
    return [
        {"PlayerName": "Alice", "Value": 1_200_000, "WeekStart": "2026-09-28"},
        {"PlayerName": "Bob", "Value": 1_100_000, "WeekStart": "2026-09-28"},
        {"PlayerName": "Carol", "Value": 900_000, "WeekStart": "2026-09-28"},
        {"PlayerName": "Dan", "Value": 10, "WeekStart": "2026-09-28"},
    ]


class AssetTests(unittest.TestCase):
    def test_files_meet_discord_rules(self) -> None:
        files = [p for p in EMOJI_DIR.glob("*.png") if not p.name.startswith("_")]
        self.assertGreater(len(files), 100)
        self.assertLessEqual(len(files), 2000)
        for p in files:
            self.assertRegex(p.stem, r"^[A-Za-z0-9_]{2,32}$")
            self.assertLessEqual(p.stat().st_size, 256 * 1024, p.name)
            with Image.open(p) as im:
                self.assertEqual(im.size, (128, 128), p.name)

    def test_every_referenced_emoji_exists(self) -> None:
        names = {p.stem for p in EMOJI_DIR.glob("*.png")}
        used = set(emojis.METRIC_ICONS.values()) | {f"place_{i}" for i in range(1, 11)}
        for path in (ROOT / "bot").rglob("*.py"):
            used |= set(re.findall(r'icon\("([a-z0-9_]+)"', path.read_text()))
        self.assertEqual(used - names, set())


class LookupTests(unittest.TestCase):
    def test_fallbacks_when_not_loaded(self) -> None:
        with patch.dict(emojis._EMOJIS, {}, clear=True):
            self.assertEqual(emojis.metric_icon("Kills"), "")
            self.assertEqual(emojis.place_icon(1), "🥇")
            self.assertEqual(emojis.place_icon(7), "#7")
            self.assertEqual(emojis.with_icon("", "text"), "text")

    def test_loaded(self) -> None:
        with patch.dict(emojis._EMOJIS, FAKE, clear=True):
            self.assertEqual(emojis.metric_icon("Kills"), "<:kills:1>")
            self.assertEqual(emojis.place_icon(2), "<:place_2:1>")


class HeadlineTests(unittest.TestCase):
    def test_leaderboard_podium(self) -> None:
        with patch.dict(emojis._EMOJIS, FAKE, clear=True):
            text = formatters.leaderboard_headline("Kills", "2026-09-28", _lb_rows())
        title, podium = text.split("\n")
        self.assertEqual(title, "<:kills:1> **Kills leaderboard** · week of 2026-09-28")
        self.assertIn("<:place_1:1> **Alice** 1.2M", podium)
        self.assertIn("<:place_3:1> **Carol** 900.0K", podium)
        self.assertNotIn("Dan", podium)

    def test_plain_without_emojis(self) -> None:
        with patch.dict(emojis._EMOJIS, {}, clear=True):
            text = formatters.leaderboard_headline("Kills", "2026-09-28", _lb_rows())
        self.assertTrue(text.startswith("**Kills leaderboard**"))
        self.assertIn("🥇 **Alice**", text)

    def test_empty_rows_no_headline(self) -> None:
        self.assertEqual(formatters.leaderboard_headline("Kills", "w", []), "")
        self.assertEqual(formatters.week_summary_headline("w", []), "")
        self.assertEqual(formatters.player_headline("p", []), "")
        self.assertEqual(formatters.growth_headline("Power", 4, []), "")
        self.assertEqual(formatters.trend_headline("Power", []), "")

    def test_names_are_escaped(self) -> None:
        rows = [{"PlayerName": "*bold*\n@everyone", "Value": 5, "WeekStart": "w"}]
        text = formatters.leaderboard_headline("Kills", "w", rows)
        self.assertIn(r"**\*bold\* @everyone**", text)
        self.assertEqual(text.count("\n"), 1)

    def test_week_summary_leader_per_metric(self) -> None:
        rows = [
            {"MetricType": "Power", "PlayerName": "Bob", "Value": 5_000_000},
            {"MetricType": "Power", "PlayerName": "Alice", "Value": 9_000_000},
            {"MetricType": "Kills", "PlayerName": "Carol", "Value": 300},
        ]
        with patch.dict(emojis._EMOJIS, FAKE, clear=True):
            lines = formatters.week_summary_headline("2026-09-28", rows).split("\n")
        self.assertEqual(lines[0], "<:report:1> **Weekly summary** · week of 2026-09-28")
        self.assertEqual(lines[1], "<:kills:1> Kills: <:place_1:1> **Carol** 300 · 1 players")
        self.assertIn("**Alice** 9.0M · 2 players", lines[2])

    def test_player_change_only_for_cumulative_metrics(self) -> None:
        rows = [
            {"MetricType": "Power", "WeekStart": "2026-09-21", "Value": 10_000_000},
            {"MetricType": "Power", "WeekStart": "2026-09-28", "Value": 12_000_000,
             "Rank": 3, "Population": 40},
            {"MetricType": "VersusPoints", "WeekStart": "2026-09-21", "Value": 1_000_000},
            {"MetricType": "VersusPoints", "WeekStart": "2026-09-28", "Value": 2_000_000},
        ]
        with patch.dict(emojis._EMOJIS, FAKE, clear=True):
            lines = formatters.player_headline("Alice", rows).split("\n")
        self.assertEqual(
            lines[1],
            "<:power:1> Power: **12.0M** (#3 of 40) <:growth_up:1> +2.0M since 2026-09-21",
        )
        self.assertEqual(lines[2], "Versus Points: **2.0M**")

    def test_growth_movers(self) -> None:
        rows = [
            {"PlayerName": "Up", "GrowthPct": 12.34},
            {"PlayerName": "Flat", "GrowthPct": None},
            {"PlayerName": "Down", "GrowthPct": -4.0},
        ]
        with patch.dict(emojis._EMOJIS, {}, clear=True):
            text = formatters.growth_headline("Power", 4, rows)
        self.assertIn("▲ Top climber **Up** +12.3%", text)
        self.assertIn("▼ Biggest drop **Down** -4.0%", text)
        only_up = formatters.growth_headline("Power", 4, rows[:1])
        self.assertNotIn("drop", only_up)

    def test_trend_counts(self) -> None:
        rows = [{"PlayerName": p, "WeekStart": w, "Value": 1}
                for p in ("A", "B") for w in ("w1", "w2", "w3")]
        self.assertEqual(formatters.trend_headline("HQLevel", rows),
                         "**HQ Level trend** · top 2 over 3 weeks")


if __name__ == "__main__":
    unittest.main()
