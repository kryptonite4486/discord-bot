"""Checks that the policy site stays in step with the bot's behaviour."""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import Settings  # noqa: E402

SITE = ROOT / "site"
PAGES = ("index.html", "privacy.html", "terms.html")


def _defaults() -> Settings:
    env = {"DISCORD_TOKEN": "test-token"}
    with patch.dict(os.environ, env, clear=True):
        return Settings.from_env()


class PolicySiteTests(unittest.TestCase):
    def test_privacy_policy_states_current_retention_defaults(self) -> None:
        # If a default changes, update site/privacy.html to match.
        s = _defaults()
        text = (SITE / "privacy.html").read_text()
        self.assertIn(f"Data is kept for <strong>{s.data_retention_days} days</strong>", text)
        self.assertIn(f"Kept for at most <strong>{s.backup_max_age_days} days</strong>", text)
        self.assertIn(f"then for {s.data_retention_days} days after it ends", text)

    def test_pages_link_to_each_other_and_the_stylesheet(self) -> None:
        for page in PAGES:
            with self.subTest(page=page):
                html = (SITE / page).read_text()
                self.assertIn('<link rel="stylesheet" href="/style.css">', html)
                self.assertRegex(html, r"<title>[^<]+</title>")
                for target in re.findall(r'href="/([a-z]+)"', html):
                    self.assertTrue((SITE / f"{target}.html").exists(), target)


if __name__ == "__main__":
    unittest.main()
