"""Tests for slash-command registration (no Discord required)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import discord  # noqa: E402

from bot.cogs.admin import run_command_sync  # noqa: E402
from bot.utils.command_sync import clear_guild_copies  # noqa: E402


class _FakeTree:
    """Records sync calls; ``remote`` maps guild id -> registered command names."""

    def __init__(self, remote: dict[int, list[str]], local=("help", "report")) -> None:
        self.remote = remote
        self.local = list(local)
        self.local_guild: dict[int, list[str]] = {}
        self.calls: list[tuple] = []

    async def fetch_commands(self, *, guild=None):
        self.calls.append(("fetch", guild.id))
        return list(self.remote.get(guild.id, []))

    def clear_commands(self, *, guild=None, type=None):
        self.calls.append(("clear", guild.id))
        self.local_guild[guild.id] = []

    def copy_global_to(self, *, guild):
        self.calls.append(("copy", guild.id))
        self.local_guild[guild.id] = list(self.local)

    async def sync(self, *, guild=None):
        if guild is None:
            self.calls.append(("sync", "global"))
            return list(self.local)
        self.calls.append(("sync", guild.id))
        self.remote[guild.id] = list(self.local_guild.get(guild.id, []))
        return self.remote[guild.id]


def _guild(gid: int, name: str = "g") -> SimpleNamespace:
    return SimpleNamespace(id=gid, name=f"{name}{gid}")


class ClearGuildCopiesTests(unittest.IsolatedAsyncioTestCase):
    async def test_removes_copies_only_where_present(self) -> None:
        tree = _FakeTree({1: ["help", "report"], 2: []})
        cleared = await clear_guild_copies(tree, [_guild(1), _guild(2)])
        self.assertEqual([g.id for g in cleared], [1])
        self.assertEqual(tree.remote[1], [])
        self.assertNotIn(("sync", 2), tree.calls)  # nothing to clean, no write

    async def test_failure_in_one_guild_does_not_stop_others(self) -> None:
        tree = _FakeTree({1: ["help"], 2: ["help"]})
        real_fetch = tree.fetch_commands

        async def flaky_fetch(*, guild=None):
            if guild.id == 1:
                resp = SimpleNamespace(status=403, reason="Forbidden")
                raise discord.Forbidden(resp, "missing access")
            return await real_fetch(guild=guild)

        tree.fetch_commands = flaky_fetch
        with self.assertLogs("bot.utils.command_sync", level="ERROR"):
            cleared = await clear_guild_copies(tree, [_guild(1), _guild(2)])
        self.assertEqual([g.id for g in cleared], [2])


class RunCommandSyncTests(unittest.IsolatedAsyncioTestCase):
    def _bot(self, tree, dev_guild_id=None):
        return SimpleNamespace(
            tree=tree, settings=SimpleNamespace(dev_guild_id=dev_guild_id)
        )

    async def test_syncs_global_and_cleans_duplicates(self) -> None:
        tree = _FakeTree({1: ["help", "report"]})
        msg = await run_command_sync(self._bot(tree), [_guild(1)])
        self.assertIn(("sync", "global"), tree.calls)
        self.assertNotIn(("copy", 1), tree.calls)
        self.assertEqual(tree.remote[1], [])
        self.assertIn("Removed duplicate", msg)

    async def test_dev_guild_only_skips_global(self) -> None:
        tree = _FakeTree({})
        msg = await run_command_sync(self._bot(tree, dev_guild_id=9), [_guild(1)])
        self.assertNotIn(("sync", "global"), tree.calls)
        self.assertEqual(tree.remote[9], ["help", "report"])
        self.assertIn("dev server", msg)


if __name__ == "__main__":
    unittest.main()
