"""Unit tests for vision OCR JSON parsing helpers."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.ocr.pipeline import VisionOCR, extract_metrics_from_image  # noqa: E402
from bot.ocr.vision import (  # noqa: E402
    _parse_json_rows,
    _prompt_for_kind,
    _rows_to_metrics,
    _strip_json_payload,
)


class VisionJsonParseTests(unittest.TestCase):
    def test_strip_markdown_fence(self) -> None:
        raw = '```json\n[{"player":"Alice","value":1000}]\n```'
        payload = _strip_json_payload(raw)
        self.assertTrue(payload.startswith("["))
        rows = _parse_json_rows(raw)
        self.assertEqual(rows[0]["player"], "Alice")

    def test_prose_around_array(self) -> None:
        raw = 'Here you go:\n[{"player":"Bob","value":"65.4M"}]\nDone.'
        rows = _parse_json_rows(raw)
        self.assertEqual(len(rows), 1)
        metrics, warnings = _rows_to_metrics(rows, kind="versus")
        self.assertEqual(len(warnings), 0)
        self.assertEqual(metrics[0].player_name, "Bob")
        self.assertEqual(metrics[0].metric_type, "VersusPoints")
        self.assertAlmostEqual(metrics[0].value, 65_400_000.0, places=0)

    def test_general_rows(self) -> None:
        rows = [{"player": "Carol", "hq": 32, "power": "12.5M"}]
        metrics, _ = _rows_to_metrics(rows, kind="general")
        types = {m.metric_type: m.value for m in metrics}
        self.assertEqual(types["HQLevel"], 32.0)
        self.assertEqual(types["Power"], 12_500_000.0)

    def test_wrapped_object(self) -> None:
        raw = '{"players": [{"name": "Dan", "score": 42}]}'
        rows = _parse_json_rows(raw)
        metrics, _ = _rows_to_metrics(rows, kind="tech")
        self.assertEqual(metrics[0].player_name, "Dan")
        self.assertEqual(metrics[0].metric_type, "TechContribution")
        self.assertEqual(metrics[0].value, 42.0)


    def test_kills_rows(self) -> None:
        rows = [{"player": "EnemyHelicopter", "value": "1,078,263"}]
        metrics, _ = _rows_to_metrics(rows, kind="kills")
        self.assertEqual(metrics[0].metric_type, "Kills")
        self.assertEqual(metrics[0].value, 1_078_263.0)
        self.assertIn("kill counts", _prompt_for_kind("kills"))

    def test_arena_member_cards_keep_only_arena_power(self) -> None:
        # Values from samples/arena_members.png, as the prompt asks: verbatim text.
        rows = [
            {"player": "Space C0wboy", "hq": 24, "power": "8.3M"},
            {"player": "EnemyHelicopter", "hq": 28, "power": "34.8M"},
        ]
        metrics, warnings = _rows_to_metrics(rows, kind="arena")
        self.assertEqual(warnings, [])
        self.assertEqual(
            {(m.player_name, m.metric_type, m.value) for m in metrics},
            {
                ("Space C0wboy", "ArenaPower", 8_300_000.0),
                ("EnemyHelicopter", "ArenaPower", 34_800_000.0),
            },
        )

    def test_member_card_prompt_asks_for_verbatim_power(self) -> None:
        # Models converting "8.3M" themselves returned 83000000.
        for kind in ("general", "arena"):
            with self.subTest(kind=kind):
                prompt = _prompt_for_kind(kind)
                self.assertIn("exactly as displayed", prompt)
                self.assertIn('"65.4M"', prompt)


_OCR = VisionOCR(
    base_url="http://127.0.0.1:8000/v1",
    model="Qwen2.5-VL-7B-Instruct",
    timeout=30.0,
)


class VisionRoutingTests(unittest.TestCase):
    def test_extract_passes_connection_settings(self) -> None:
        fake = MagicMock()
        with patch(
            "bot.ocr.vision.extract_metrics_via_vision", return_value=fake
        ) as mocked:
            result = extract_metrics_from_image(
                "/tmp/does-not-matter.png", kind="versus", ocr=_OCR
            )
        self.assertIs(result, fake)
        mocked.assert_called_once_with(
            "/tmp/does-not-matter.png",
            kind="versus",
            base_url="http://127.0.0.1:8000/v1",
            model="Qwen2.5-VL-7B-Instruct",
            api_key="",
            timeout=30.0,
        )

    def test_unknown_kind_rejected(self) -> None:
        # Auto-detect was removed; callers must name the dataset.
        with self.assertRaises(ValueError):
            extract_metrics_from_image(
                "/tmp/does-not-matter.png", kind="auto", ocr=_OCR  # type: ignore[arg-type]
            )

    def test_label(self) -> None:
        self.assertEqual(_OCR.label, "vision/Qwen2.5-VL-7B-Instruct")


class OcrEngineSettingTests(unittest.TestCase):
    def test_stale_engine_rejected(self) -> None:
        from bot.config import _check_ocr_engine

        for value in ("easyocr", "tesseract"):
            with self.subTest(value=value), patch.dict("os.environ", {"OCR_ENGINE": value}):
                with self.assertRaises(RuntimeError):
                    _check_ocr_engine()
        for value in ("", "vision", "omlx"):
            with self.subTest(value=value), patch.dict("os.environ", {"OCR_ENGINE": value}):
                _check_ocr_engine()


if __name__ == "__main__":
    unittest.main()
