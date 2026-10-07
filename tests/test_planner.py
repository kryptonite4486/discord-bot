"""Tests for /planner: share-link validation, replies, and PLANNER_URL config."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import discord  # noqa: E402

from bot.cogs.planner import Planner, parse_share_link  # noqa: E402
from bot.config import DEFAULT_PLANNER_URL, DEFAULT_SUPPORT_URL, Settings  # noqa: E402

BASE = "https://lastz-territory-planner.pages.dev"


class ParseShareLinkTests(unittest.TestCase):
    def test_accepts_share_link(self) -> None:
        link = f"{BASE}/#plan=XY5B_-abc123"
        self.assertEqual(parse_share_link(link, BASE), link)

    def test_strips_whitespace_and_angle_brackets(self) -> None:
        link = f"{BASE}/#plan=abc"
        self.assertEqual(parse_share_link(f"  <{link}>  ", BASE), link)

    def test_rejects_other_hosts(self) -> None:
        self.assertIsNone(parse_share_link("https://evil.example/#plan=abc", BASE))
        self.assertIsNone(
            parse_share_link("https://lastz-territory-planner.pages.dev.evil.example/#plan=abc", BASE)
        )

    def test_rejects_http_and_missing_or_bad_plan(self) -> None:
        self.assertIsNone(parse_share_link(f"http://{BASE[8:]}/#plan=abc", BASE))
        self.assertIsNone(parse_share_link(f"{BASE}/", BASE))
        self.assertIsNone(parse_share_link(f"{BASE}/#plan=", BASE))
        self.assertIsNone(parse_share_link(f"{BASE}/#plan=abc def", BASE))
        self.assertIsNone(parse_share_link("not a url", BASE))


def _interaction() -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(mention="<@1>"),
        response=SimpleNamespace(send_message=mock.AsyncMock()),
    )


class PlannerCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot = SimpleNamespace(settings=SimpleNamespace(planner_url=BASE))
        self.cog = Planner(bot)  # type: ignore[arg-type]

    async def _run(self, plan: str | None) -> tuple[tuple, dict]:
        inter = _interaction()
        await self.cog.planner.callback(self.cog, inter, plan)  # type: ignore[arg-type]
        inter.response.send_message.assert_awaited_once()
        return inter.response.send_message.await_args

    @staticmethod
    def _button_urls(kwargs: dict) -> list[str]:
        view = kwargs.get("view")
        if view is discord.utils.MISSING or view is None:
            return []
        return [item.url for item in view.children]

    async def test_no_plan_posts_planner_button_publicly(self) -> None:
        args, kwargs = await self._run(None)
        self.assertEqual(self._button_urls(kwargs), [BASE])
        self.assertFalse(kwargs.get("ephemeral", False))

    async def test_short_plan_gets_a_button(self) -> None:
        link = f"{BASE}/#plan=" + "a" * 300
        args, kwargs = await self._run(link)
        self.assertEqual(self._button_urls(kwargs), [link])
        self.assertNotIn(link, args[0])

    async def test_long_plan_falls_back_to_masked_link(self) -> None:
        # A fully planned map is ~850 chars, over Discord's 512-char button limit.
        link = f"{BASE}/#plan=" + "a" * 850
        args, kwargs = await self._run(link)
        self.assertIs(kwargs["view"], discord.utils.MISSING)
        self.assertIn(f"[Open shared plan]({link})", args[0])
        self.assertLess(len(args[0]), 2000)

    async def test_bad_link_is_rejected_privately(self) -> None:
        args, kwargs = await self._run("https://evil.example/#plan=abc")
        self.assertTrue(kwargs["ephemeral"])
        self.assertIn("isn't a planner share link", args[0])


class PlannerUrlSettingTests(unittest.TestCase):
    def _settings(self, **env: str) -> Settings:
        base = {"DISCORD_TOKEN": "test-token"}
        with mock.patch.dict(os.environ, {**base, **env}, clear=True):
            return Settings.from_env()

    def test_defaults_to_hosted_site(self) -> None:
        self.assertEqual(self._settings().planner_url, DEFAULT_PLANNER_URL)

    def test_override_drops_trailing_slash(self) -> None:
        s = self._settings(PLANNER_URL="https://planner.example.com/")
        self.assertEqual(s.planner_url, "https://planner.example.com")


class SupportUrlSettingTests(unittest.TestCase):
    _settings = PlannerUrlSettingTests._settings

    def test_defaults_to_help_invite(self) -> None:
        self.assertEqual(self._settings().support_url, DEFAULT_SUPPORT_URL)

    def test_override(self) -> None:
        s = self._settings(SUPPORT_URL=" https://discord.gg/other ")
        self.assertEqual(s.support_url, "https://discord.gg/other")


if __name__ == "__main__":
    unittest.main()
