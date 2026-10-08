"""bot/utils/skus.json: the store listing matches the tiers and Discord's limits."""

from __future__ import annotations

import copy
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.utils import skus  # noqa: E402
from bot.utils.tiers import FULL, MID  # noqa: E402


def _manifest() -> dict:
    return copy.deepcopy(skus.load_manifest())


class ManifestTests(unittest.TestCase):
    def test_manifest_is_consistent(self) -> None:
        self.assertEqual(skus.validate(skus.load_manifest()), [])

    def test_lookups(self) -> None:
        mid_id = skus.sku_id_for("mid")
        self.assertTrue(mid_id)
        self.assertEqual(skus.tier_for_sku(mid_id), "mid")
        self.assertEqual(skus.tier_for_sku(int(skus.sku_id_for("full"))), "full")
        self.assertIsNone(skus.tier_for_sku("1"))
        self.assertIsNone(skus.sku_id_for("free"))

    def test_site_prices_match(self) -> None:
        html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
        prices = {s["tier"]: s["price_usd"] for s in skus.load_manifest()["skus"]}
        for policy in (MID, FULL):
            found = re.search(rf'<h3>{policy.name}</h3>\s*<p class="price">\$([\d.]+)', html)
            self.assertIsNotNone(found, policy.name)
            self.assertEqual(float(found.group(1)), prices[policy.key], policy.name)


class ValidationTests(unittest.TestCase):
    def test_benefit_promising_wrong_limit(self) -> None:
        m = _manifest()
        m["skus"][0]["benefits"][0]["limits"]["ocr_images_per_week"] = MID.ocr_images_per_week + 1
        self.assertTrue(any("ocr_images_per_week" in p for p in skus.validate(m)))

    def test_benefit_promising_missing_feature(self) -> None:
        m = _manifest()
        mid = next(s for s in m["skus"] if s["tier"] == "mid")
        mid["benefits"][0]["features"] = ["multi_channel_reports"]
        self.assertTrue(any("multi_channel_reports" in p for p in skus.validate(m)))

    def test_discord_limits(self) -> None:
        m = _manifest()
        m["skus"][0]["description"] = "x" * 161
        m["skus"][1]["benefits"] *= 2
        problems = skus.validate(m)
        self.assertTrue(any("description is 161" in p for p in problems))
        self.assertTrue(any("benefits (has 12)" in p for p in problems))

    def test_names_and_tiers(self) -> None:
        m = _manifest()
        m["skus"][0]["name"] = "Alliance Tier"
        del m["skus"][1]
        problems = skus.validate(m)
        self.assertTrue(any("tier name" in p for p in problems))
        self.assertTrue(any("'full' needs exactly one" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
