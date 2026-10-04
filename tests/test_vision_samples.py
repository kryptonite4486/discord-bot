"""Sample screenshots through the configured vision model (the production OCR path).

Runs with the normal suite, using OCR_VISION_* from .env, so tests exercise the
same model and prompts as the deployed bot. Skips (with a reason) when the
server is unreachable; set RUN_VISION_TESTS=0 to skip deliberately.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

# Allow `python tests/test_vision_samples.py` from repo root
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import load_vision_env  # noqa: E402  (also loads .env)
from bot.ocr.pipeline import VisionOCR, extract_metrics_from_image  # noqa: E402

SAMPLES = ROOT / "samples"


def _base_url(configured: str) -> str:
    """host.docker.internal only resolves inside Docker; use loopback outside it."""
    override = os.getenv("OCR_VISION_TEST_BASE_URL", "").strip()
    if override:
        return override
    parts = urlsplit(configured)
    if parts.hostname == "host.docker.internal" and not Path("/.dockerenv").exists():
        netloc = parts.netloc.replace("host.docker.internal", "127.0.0.1")
        return urlunsplit(parts._replace(netloc=netloc))
    return configured


def _server_reachable(base_url: str, api_key: str) -> bool:
    try:
        import httpx
    except ImportError:
        return False
    try:
        httpx.get(
            base_url.rstrip("/") + "/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=5.0,
        )
    except httpx.HTTPError:
        return False
    return True


_base, _model, _api_key, _timeout = load_vision_env()
_OCR = VisionOCR(
    base_url=_base_url(_base), model=_model, api_key=_api_key, timeout=_timeout
)
_DISABLED = os.getenv("RUN_VISION_TESTS", "").strip().lower() in {"0", "false", "no"}


@unittest.skipIf(_DISABLED, "RUN_VISION_TESTS=0")
class VisionSampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not _server_reachable(_OCR.base_url, _OCR.api_key):
            raise unittest.SkipTest(f"vision server not reachable at {_OCR.base_url}")

    def _extract(self, filename: str, kind: str):
        # Same entry point the bot's ingest path uses.
        result = extract_metrics_from_image(
            SAMPLES / filename,
            kind=kind,  # type: ignore[arg-type]
            ocr=_OCR,
        )
        self.assertEqual(result.warnings, [], result.raw_text)
        return result

    def _by_metric(self, result, metric: str) -> dict[str, float]:
        return {
            m.player_name: m.value for m in result.metrics if m.metric_type == metric
        }

    def test_power_leaderboard(self) -> None:
        values = self._by_metric(self._extract("power_leaderboard.png", "power"), "Power")
        self.assertEqual(values.get("EnemyHelicopter"), 112_257_938.0)
        self.assertEqual(values.get("KBeezCONC"), 65_521_967.0)
        self.assertEqual(values.get("zena75"), 30_658_131.0)
        self.assertIn(56_705_138.0, values.values())
        self.assertIn(33_644_447.0, values.values())

    def test_versus_leaderboard(self) -> None:
        values = self._by_metric(
            self._extract("versus_leaderboard.png", "versus"), "VersusPoints"
        )
        self.assertEqual(
            values,
            {
                "1Parzival1": 167_040.0,
                "93NAT93": 161_180.0,
                "EscapefromNY": 144_960.0,
                "Babylon 4": 127_560.0,
                "zippyfinny": 121_220.0,
                "Zeus el great": 117_660.0,
                "lastwarrior": 117_600.0,
            },
        )

    def test_kills_leaderboard(self) -> None:
        result = self._extract("kills_leaderboard.webp", "kills")
        values = self._by_metric(result, "Kills")
        self.assertEqual(len(result.metrics), 8)
        self.assertEqual(values.get("EnemyHelicopter"), 1_078_263.0)
        self.assertEqual(values.get("KBeezCONC"), 1_005_842.0)
        self.assertEqual(values.get("Schmagalicious"), 815_445.0)
        self.assertEqual(values.get("2Face"), 688_506.0)
        self.assertEqual(values.get("S0FIA"), 601_931.0)
        self.assertEqual(values.get("EscapefromNY"), 585_082.0)
        self.assertEqual(values.get("Vexx23"), 512_140.0)
        self.assertEqual(values.get("Louis153"), 443_341.0)

    def test_general_member_cards(self) -> None:
        result = self._extract("general_members.png", "general")
        power = self._by_metric(result, "Power")
        hq = self._by_metric(result, "HQLevel")
        expected = {
            "Space C0wboy": (23, 50_000_000),
            "EnemyHelicopter": (27, 99_100_000),
            "KBeezCONC": (25, 96_100_000),
            "S0FIA": (25, 78_200_000),
            "MiniAB": (22, 49_300_000),
            "Chezstar800": (24, 52_900_000),
        }
        for name, (level, value) in expected.items():
            with self.subTest(player=name):
                self.assertEqual(hq.get(name), float(level))
                self.assertEqual(power.get(name), float(value))
        # "Wild Bèard" accent varies between reads; match by value.
        self.assertIn(60_400_000.0, power.values())

    def test_general_profile(self) -> None:
        result = self._extract("general_profile.png", "general")
        power = self._by_metric(result, "Power")
        hq = self._by_metric(result, "HQLevel")
        name = next((n for n in power if "PrincessPea" in n), None)
        self.assertIsNotNone(name, power)
        self.assertEqual(power[name], 65_400_000.0)
        self.assertEqual(hq.get(name), 24.0)

    def test_arena_member_cards(self) -> None:
        result = self._extract("arena_members.png", "arena")
        # Arena cards store only ArenaPower; the HQ level is ignored.
        self.assertEqual({m.metric_type for m in result.metrics}, {"ArenaPower"})
        self.assertEqual(
            self._by_metric(result, "ArenaPower"),
            {
                "Space C0wboy": 8_300_000.0,
                "Vexx23": 10_400_000.0,
                "S0FIA": 10_900_000.0,
                "EnemyHelicopter": 34_800_000.0,
                "MiniAB": 3_900_000.0,
                "Ciezee": 8_400_000.0,
                "KBeezCONC": 26_300_000.0,
                "Chezstar800": 13_100_000.0,
            },
        )


if __name__ == "__main__":
    unittest.main()
