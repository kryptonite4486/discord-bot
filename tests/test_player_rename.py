"""Tests for finding name variants and renaming/merging players."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

# Allow `python tests/test_player_rename.py` from repo root
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.db import Database  # noqa: E402
from bot.cogs.admin import format_variant_report  # noqa: E402
from bot.utils.names import group_variants  # noqa: E402

W1, W2 = "2026-09-27", "2026-10-04"


class RenameTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "t.db")
        await self.db.connect()
        await self.db.upsert_metrics(
            "g",
            [
                (W1, "Overlt", "Power", 100),
                (W1, "Overlt", "Kills", 10),
                (W2, "OverIt", "Power", 110),
                (W2, "Overit", "Kills", 12),
            ],
            channel_id="c1",
        )
        # Same names elsewhere must only change when the scope includes them.
        await self.db.upsert_metrics("g", [(W1, "Overlt", "Power", 5)], channel_id="c2")
        await self.db.upsert_metrics("other", [(W1, "Overlt", "Power", 7)], channel_id="c1")

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def _names(self, guild: str, channel: str) -> dict[str, int]:
        return await self.db.player_name_counts(guild, channel)

    async def test_rename_within_channel(self) -> None:
        result = await self.db.rename_player("g", "Overlt", "OverIt", channel_id="c1")
        self.assertEqual(result["moved"], 2)
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(await self._names("g", "c1"), {"OverIt": 3, "Overit": 1})
        self.assertEqual(await self._names("g", "c2"), {"Overlt": 1})
        self.assertEqual(await self._names("other", "c1"), {"Overlt": 1})

    async def test_rename_server_wide_stays_in_guild(self) -> None:
        result = await self.db.rename_player("g", "Overlt", "OverIt")
        self.assertEqual(result["moved"], 3)
        self.assertEqual(await self._names("g", "c2"), {"OverIt": 1})
        self.assertEqual(await self._names("other", "c1"), {"Overlt": 1})

    async def test_conflict_stops_by_default(self) -> None:
        await self.db.upsert_metrics("g", [(W2, "Overlt", "Power", 999)], channel_id="c1")
        result = await self.db.rename_player("g", "Overlt", "OverIt", channel_id="c1")
        self.assertEqual(result["moved"], 0)
        self.assertEqual(len(result["conflicts"]), 1)
        conflict = result["conflicts"][0]
        self.assertEqual((conflict["WeekStart"], conflict["MetricType"]), (W2, "Power"))
        self.assertEqual(await self._names("g", "c1"), {"Overlt": 3, "OverIt": 1, "Overit": 1})

    async def test_conflict_keep_target(self) -> None:
        await self.db.upsert_metrics("g", [(W2, "Overlt", "Power", 999)], channel_id="c1")
        result = await self.db.rename_player(
            "g", "Overlt", "OverIt", channel_id="c1", on_conflict="keep_target"
        )
        self.assertEqual((result["dropped"], result["moved"]), (1, 2))
        rows = await self.db.get_week_metrics("g", W2, "Power", channel_id="c1")
        self.assertEqual([(r["PlayerName"], r["Value"]) for r in rows], [("OverIt", 110.0)])

    async def test_conflict_keep_source(self) -> None:
        await self.db.upsert_metrics("g", [(W2, "Overlt", "Power", 999)], channel_id="c1")
        result = await self.db.rename_player(
            "g", "Overlt", "OverIt", channel_id="c1", on_conflict="keep_source"
        )
        self.assertEqual((result["replaced"], result["moved"]), (1, 3))
        rows = await self.db.get_week_metrics("g", W2, "Power", channel_id="c1")
        self.assertEqual([(r["PlayerName"], r["Value"]) for r in rows], [("OverIt", 999.0)])

    async def test_invalid_input(self) -> None:
        for args in (("Overlt", "Overlt"), ("", "X"), ("X", "  ")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                await self.db.rename_player("g", *args)

    async def test_concurrent_upload_cannot_split_a_rename(self) -> None:
        # Interleave uploads with a conflict-resolving rename on the shared connection.
        await self.db.upsert_metrics("g", [(W2, "Overlt", "Power", 999)], channel_id="c1")
        uploads = [
            self.db.upsert_metrics("g", [(W1, f"P{n}", "Power", n)], channel_id="c1")
            for n in range(20)
        ]
        await asyncio.gather(
            self.db.rename_player("g", "Overlt", "OverIt", channel_id="c1", on_conflict="keep_target"),
            *uploads,
        )
        names = await self._names("g", "c1")
        self.assertNotIn("Overlt", names)
        self.assertEqual(names["OverIt"], 3)
        self.assertEqual(sum(1 for n in names if n.startswith("P")), 20)

    async def test_variant_report(self) -> None:
        summary = await self.db.player_name_summary("g", channel_id="c1")
        groups = group_variants(summary)
        self.assertEqual(len(groups), 1)
        self.assertEqual(
            [(r["PlayerName"], r["Rows"]) for r in groups[0]],
            [("Overlt", 2), ("OverIt", 1), ("Overit", 1)],
        )
        conflicts = await self.db.name_conflicts(
            "g", [r["PlayerName"] for r in groups[0]], channel_id="c1"
        )
        self.assertEqual(conflicts, [])


class ReportTests(unittest.TestCase):
    GROUP = [
        {"ChannelId": "c1", "PlayerName": "Overlt", "Rows": 7, "FirstWeek": W1, "LastWeek": W2},
        {"ChannelId": "c1", "PlayerName": "OverIt", "Rows": 2, "FirstWeek": W1, "LastWeek": W2},
    ]

    def test_report_lists_groups_and_conflicts(self) -> None:
        clash = {"WeekStart": W2, "MetricType": "Power", "ValuesByName": "Overlt=1, OverIt=2"}
        text = format_variant_report([self.GROUP], [[clash]], server_scope=True)
        self.assertIn("**1.** <#c1> `Overlt` (7 rows, 2026-09-27→2026-10-04) · `OverIt` (2 rows", text)
        self.assertIn("⚠️ 1 conflict(s): 2026-10-04 Power: Overlt=1, OverIt=2", text)
        self.assertIn("/admin rename-player", text)

    def test_channel_scope_omits_channel_and_empty_report(self) -> None:
        text = format_variant_report([self.GROUP], [[]], server_scope=False)
        self.assertNotIn("<#c1>", text)
        self.assertNotIn("⚠️", text)
        self.assertEqual(
            format_variant_report([], [], server_scope=False),
            "No duplicate player names found in this channel.",
        )


if __name__ == "__main__":
    unittest.main()
