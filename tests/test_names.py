"""Tests for matching OCR-read player names to stored players."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Allow `python tests/test_names.py` from repo root
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.cogs.ingest import Ingest  # noqa: E402
from bot.db import Database  # noqa: E402
from bot.ocr.pipeline import ExtractedMetric, OCRResult  # noqa: E402
from bot.utils.archive import ImageSource  # noqa: E402
from bot.utils.names import lookalike_key, reconcile_names  # noqa: E402


class ReconcileTests(unittest.TestCase):
    def test_case_and_symbol_misreads_map_to_stored_spelling(self) -> None:
        known = {"KryOGeN": 12, "KyoKilla": 9, "S0FIA": 20}
        self.assertEqual(
            reconcile_names(["KryOGEN", "Kyokilla", "SOFIA"], known),
            {"KryOGEN": "KryOGeN", "Kyokilla": "KyoKilla", "SOFIA": "S0FIA"},
        )

    def test_stray_backslash_variant(self) -> None:
        self.assertEqual(reconcile_names(["KryOGE\\N"], {"KryOGeN": 3}), {"KryOGE\\N": "KryOGeN"})

    def test_different_players_never_merge(self) -> None:
        known = {"Player1": 5, "Louis153": 4, "Vexx23": 4}
        self.assertEqual(reconcile_names(["Player2", "Louis154", "Vexx"], known), {})

    def test_new_player_kept(self) -> None:
        self.assertEqual(reconcile_names(["Brand New"], {"KryOGeN": 3}), {})

    def test_most_used_spelling_wins_for_existing_duplicates(self) -> None:
        known = {"MeOW": 30, "MeOw": 2}
        self.assertEqual(reconcile_names(["meow"], known), {"meow": "MeOW"})

    def test_ambiguous_lookalike_is_left_alone(self) -> None:
        # "Ali" could be "A1i" or "Al1"; with two candidates, don't guess.
        self.assertEqual(reconcile_names(["Ail"], {"A1i": 3, "Al1": 3}), {})

    def test_target_already_on_screenshot_is_not_reused(self) -> None:
        # Both spellings on one screenshot are two players; don't fold one into the other.
        self.assertEqual(reconcile_names(["KyoKilla", "Kyokilla"], {"KyoKilla": 9}), {})

    def test_two_reads_competing_for_one_target(self) -> None:
        self.assertEqual(reconcile_names(["kyokilla", "KYOKILLA"], {"KyoKilla": 9}), {})

    def test_lookalike_key(self) -> None:
        self.assertEqual(lookalike_key("S0F1A"), lookalike_key("sofla"))
        self.assertEqual(lookalike_key("KryOGE\\N"), lookalike_key("KryOGeN"))
        self.assertNotEqual(lookalike_key("Player1"), lookalike_key("Player2"))


class IngestMatchingTests(unittest.IsolatedAsyncioTestCase):
    async def test_misread_name_saved_under_existing_player(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "t.db")
            await db.connect()
            try:
                await db.upsert_metrics(
                    "g", [("2026-09-27", "KryOGeN", "Power", 47_000_000)], channel_id="c"
                )
                settings = SimpleNamespace(
                    ocr_vision_base_url="http://x/v1",
                    ocr_vision_model="m",
                    ocr_vision_api_key="",
                    ocr_vision_timeout=5.0,
                    ocr_max_concurrency=1,
                )
                cog = Ingest(SimpleNamespace(settings=settings, db=db))  # type: ignore[arg-type]
                result = OCRResult(
                    kind="general",
                    metrics=[
                        ExtractedMetric("KryOGEN", "HQLevel", 21),
                        ExtractedMetric("KryOGEN", "Power", 48_800_000),
                    ],
                    raw_text="",
                    warnings=[],
                )
                with patch("bot.cogs.ingest.extract_metrics_from_image", return_value=result):
                    detail, count, names, alerts = await cog._process_attachment(
                        ImageSource(key="k", filename="general.zip/x.png", data=b""),
                        "general",
                        "2026-10-04",
                        "g",
                        "c",
                    )
                self.assertEqual(names, {"KryOGeN"})
                self.assertEqual(alerts, ["🔤 matched `KryOGEN` → `KryOGeN`"])
                self.assertEqual(await db.player_name_counts("g", "c"), {"KryOGeN": 3})
            finally:
                await db.close()


if __name__ == "__main__":
    unittest.main()
