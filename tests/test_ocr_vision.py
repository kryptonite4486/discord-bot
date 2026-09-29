"""Unit tests for vision OCR JSON parsing helpers."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.ocr.pipeline import OCREngine, extract_metrics_from_image  # noqa: E402
from bot.ocr.vision import (  # noqa: E402
    _parse_json_rows,
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
        metrics, warnings = _rows_to_metrics(
            rows, kind="versus", metric_type_override=None
        )
        self.assertEqual(len(warnings), 0)
        self.assertEqual(metrics[0].player_name, "Bob")
        self.assertEqual(metrics[0].metric_type, "VersusPoints")
        self.assertAlmostEqual(metrics[0].value, 65_400_000.0, places=0)

    def test_general_rows(self) -> None:
        rows = [{"player": "Carol", "hq": 32, "power": "12.5M"}]
        metrics, _ = _rows_to_metrics(rows, kind="general", metric_type_override=None)
        types = {m.metric_type: m.value for m in metrics}
        self.assertEqual(types["HQLevel"], 32.0)
        self.assertEqual(types["Power"], 12_500_000.0)

    def test_wrapped_object(self) -> None:
        raw = '{"players": [{"name": "Dan", "score": 42}]}'
        rows = _parse_json_rows(raw)
        metrics, _ = _rows_to_metrics(
            rows, kind="tech", metric_type_override="TechContribution"
        )
        self.assertEqual(metrics[0].player_name, "Dan")
        self.assertEqual(metrics[0].metric_type, "TechContribution")
        self.assertEqual(metrics[0].value, 42.0)


class VisionEngineRoutingTests(unittest.TestCase):
    def test_omlx_alias(self) -> None:
        engine = OCREngine("omlx")
        self.assertTrue(engine.is_vision)
        self.assertEqual(engine.engine_name, "vision")

    def test_extract_routes_to_vision(self) -> None:
        fake = MagicMock()
        fake.kind = "versus"
        fake.metrics = []
        fake.raw_text = "[]"
        fake.warnings = []
        engine = OCREngine(
            "vision",
            vision_base_url="http://127.0.0.1:8000/v1",
            vision_model="Qwen2.5-VL-7B-Instruct",
            vision_api_key="",
            vision_timeout=30.0,
        )
        with patch(
            "bot.ocr.vision.extract_metrics_via_vision", return_value=fake
        ) as mocked:
            result = extract_metrics_from_image(
                "/tmp/does-not-matter.png",
                kind="versus",
                engine=engine,
            )
        mocked.assert_called_once()
        self.assertIs(result, fake)


if __name__ == "__main__":
    unittest.main()
