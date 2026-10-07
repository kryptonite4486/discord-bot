"""Tests for the /help 🔒 marks on commands the server's plan doesn't include."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.cogs.admin import Admin  # noqa: E402
from bot.cogs.help_cmd import HELP_TEXT, HelpCmd  # noqa: E402
from bot.cogs.ingest import Ingest  # noqa: E402
from bot.cogs.reports import Reports  # noqa: E402
from bot.utils.help_locks import help_with_locks, locked_commands, mark_locked  # noqa: E402
from bot.utils.parsing import chunk_message  # noqa: E402
from bot.utils.tiers import FREE, FULL, MID, TierStatus, command_features  # noqa: E402

ALLIANCE_ONLY = {
    "report player", "report trend", "report growth",
    "ingest zip", "ingest batch", "admin duplicates", "admin rename-player",
}


def _commands():
    bot = SimpleNamespace(settings=SimpleNamespace(
        ocr_vision_base_url="http://x/v1", ocr_vision_model="m", ocr_vision_api_key="",
        ocr_vision_timeout=5.0, ocr_max_concurrency=1,
    ))
    return [
        cmd
        for cog in (Reports(bot), Ingest(bot), Admin(bot))  # type: ignore[arg-type]
        for cmd in cog.walk_app_commands()
    ]


def _interaction(policy, *, enforced: bool):
    tiers = SimpleNamespace(enforced=enforced, status=AsyncMock(return_value=TierStatus(policy)))
    return SimpleNamespace(client=SimpleNamespace(tiers=tiers), guild_id=1)


def _locked_lines(text: str) -> dict[str, str]:
    """{help line's command: tier name} for each 🔒-marked line."""
    out = {}
    for line in text.splitlines():
        if "🔒 " in line and line.lstrip().startswith("/"):
            words = line.split()
            out[" ".join(words[:2])[1:]] = line.rsplit("🔒 ", 1)[1]
    return out


class MappingTests(unittest.TestCase):
    def test_gates_read_from_requires_feature_checks(self) -> None:
        gates = command_features(_commands())
        self.assertEqual(set(gates), ALLIANCE_ONLY)
        self.assertEqual(gates["ingest zip"], "zip_batch")
        self.assertEqual(gates["admin duplicates"], "name_tools")

    def test_every_gated_command_has_a_help_line(self) -> None:
        # Fails if a command gets requires_feature but /help doesn't list it,
        # so it could never be marked.
        everything = locked_commands(_commands(), FREE)
        marked = _locked_lines(mark_locked(HELP_TEXT, everything))
        self.assertEqual(set(marked), set(command_features(_commands())))

    def test_similar_names_not_marked(self) -> None:
        text = "  /report trend     x\n  /report trendy    y\n  /report trend\n"
        out = mark_locked(text, {"report trend": MID})
        self.assertEqual(out, "  /report trend     x  🔒 Alliance\n  /report trendy    y\n  /report trend  🔒 Alliance\n")


class HelpTextTests(unittest.IsolatedAsyncioTestCase):
    async def _help(self, policy, *, enforced: bool) -> str:
        return await help_with_locks(_interaction(policy, enforced=enforced), HELP_TEXT, _commands())

    async def test_free_enforced_marks_alliance_commands(self) -> None:
        text = await self._help(FREE, enforced=True)
        self.assertEqual(_locked_lines(text), {name: "Alliance" for name in ALLIANCE_ONLY})
        footer = text.rstrip().splitlines()[-1]
        self.assertIn("**Free** plan", footer)
        self.assertIn("`/premium`", footer)
        self.assertNotIn("Command", "".join(_locked_lines(text).values()))

    async def test_alliance_and_command_enforced_unchanged(self) -> None:
        # No command-level gate needs Command yet (multi-channel reports are
        # refused at scope resolution), so neither paid plan sees marks.
        for policy in (MID, FULL):
            self.assertEqual(await self._help(policy, enforced=True), HELP_TEXT, policy.name)

    async def test_alliance_sees_command_only_gates(self) -> None:
        locked = locked_commands(_commands(), MID)
        self.assertEqual(locked, {})
        fake = SimpleNamespace(qualified_name="report week", checks=[_check("multi_channel_reports")])
        locked = locked_commands([*_commands(), fake], MID)
        self.assertEqual({k: t.name for k, t in locked.items()}, {"report week": "Command"})
        self.assertEqual(locked_commands([fake], FULL), {})

    async def test_not_enforced_shows_no_marks(self) -> None:
        for policy in (FREE, MID, FULL):
            self.assertEqual(await self._help(policy, enforced=False), HELP_TEXT, policy.name)

    async def test_no_tier_service_or_dm(self) -> None:
        inter = SimpleNamespace(client=SimpleNamespace(), guild_id=1)
        self.assertEqual(await help_with_locks(inter, HELP_TEXT, _commands()), HELP_TEXT)
        inter = _interaction(FREE, enforced=True)
        inter.guild_id = None
        self.assertEqual(await help_with_locks(inter, HELP_TEXT, _commands()), HELP_TEXT)

    async def test_marked_help_fits_discord_messages(self) -> None:
        text = await self._help(FREE, enforced=True)
        chunks = list(chunk_message(text, limit=1900))
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 2000)
            self.assertEqual(chunk.count("```") % 2, 0)
        self.assertLessEqual(len(chunks), 5)

    async def test_help_command_sends_marked_text(self) -> None:
        sent: list[str] = []
        views = []

        async def send(text, ephemeral=False, view=None):
            sent.append(text)
            views.append(view)

        inter = _interaction(FREE, enforced=True)
        inter.response = SimpleNamespace(defer=AsyncMock())
        inter.followup = SimpleNamespace(send=send)
        bot = SimpleNamespace(
            tree=SimpleNamespace(walk_commands=_commands),
            settings=SimpleNamespace(support_url="https://discord.gg/support"),
        )
        await HelpCmd.help_slash.callback(HelpCmd(bot), inter)  # type: ignore[arg-type]
        joined = "".join(sent)
        self.assertIn("/ingest zip       OCR every image in a .zip (up to 50)  🔒 Alliance", joined)
        self.assertIn("`/premium`", sent[-1])
        self.assertIn("support server", sent[-1])
        # Only the last message carries the support server button.
        self.assertTrue(all(v is None for v in views[:-1]))
        self.assertEqual([b.url for b in views[-1].children], ["https://discord.gg/support"])


def _check(feature):
    async def predicate(interaction):
        return True

    predicate.feature = feature
    return predicate


if __name__ == "__main__":
    unittest.main()
