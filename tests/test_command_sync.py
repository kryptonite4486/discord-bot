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

from bot.cogs.ops import Ops  # noqa: E402
from bot.utils.command_sync import clear_guild_copies, sync_commands  # noqa: E402


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


class SyncCommandsTests(unittest.IsolatedAsyncioTestCase):
    async def test_syncs_global_and_cleans_duplicates(self) -> None:
        tree = _FakeTree({1: ["help", "report"]})
        result = await sync_commands(tree, [_guild(1)])
        self.assertEqual(result.global_count, 2)
        self.assertNotIn(("copy", 1), tree.calls)
        self.assertEqual(tree.remote[1], [])
        self.assertIn("Removed duplicate", result.summary())

    async def test_control_guild_keeps_its_commands(self) -> None:
        tree = _FakeTree({7: ["ops"]})
        tree.local_guild[7] = ["ops"]  # cog registered /ops to the control guild
        result = await sync_commands(tree, [_guild(1), _guild(7)], control_guild_id=7)
        self.assertNotIn(("clear", 7), tree.calls)
        self.assertEqual(tree.remote[7], ["ops"])
        self.assertEqual(result.control_count, 1)
        self.assertEqual(result.cleared, [])

    async def test_control_guild_not_joined_is_reported(self) -> None:
        tree = _FakeTree({})
        real_sync = tree.sync

        async def sync(*, guild=None):
            if guild is not None and guild.id == 7:
                raise discord.Forbidden(SimpleNamespace(status=403, reason="x"), "no")
            return await real_sync(guild=guild)

        tree.sync = sync
        with self.assertLogs("bot.utils.command_sync", level="WARNING"):
            result = await sync_commands(tree, [_guild(1)], control_guild_id=7)
        self.assertEqual(result.global_count, 2)
        self.assertIn("not in that server", result.summary())

    async def test_dev_guild_only_skips_global(self) -> None:
        tree = _FakeTree({})
        result = await sync_commands(tree, [_guild(1)], dev_guild_id=9)
        self.assertNotIn(("sync", "global"), tree.calls)
        self.assertEqual(tree.remote[9], ["help", "report"])
        self.assertIn("dev server", result.summary())


class _FakeResponse:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, text, ephemeral=False):
        self.sent.append(text)


class OperatorCheckTests(unittest.IsolatedAsyncioTestCase):
    OWNER, CONTROL = 100, 7

    def _cog(self) -> Ops:
        settings = SimpleNamespace(
            bot_owner_ids=frozenset({self.OWNER}), control_guild_id=self.CONTROL
        )
        return Ops(SimpleNamespace(settings=settings))

    def _interaction(self, user_id: int, guild_id: int):
        return SimpleNamespace(
            user=SimpleNamespace(id=user_id),
            guild_id=guild_id,
            response=_FakeResponse(),
        )

    async def _check(self, user_id: int, guild_id: int):
        inter = self._interaction(user_id, guild_id)
        with self.assertNoLogs("bot.cogs.ops", level="WARNING") if (
            user_id == self.OWNER and guild_id == self.CONTROL
        ) else self.assertLogs("bot.cogs.ops", level="WARNING"):
            ok = await self._cog().interaction_check(inter)
        return ok, inter.response.sent

    async def test_owner_in_control_guild_allowed(self) -> None:
        ok, sent = await self._check(self.OWNER, self.CONTROL)
        self.assertTrue(ok)
        self.assertEqual(sent, [])

    async def test_owner_in_other_guild_refused(self) -> None:
        ok, sent = await self._check(self.OWNER, 1)
        self.assertFalse(ok)
        self.assertEqual(len(sent), 1)

    async def test_other_user_in_control_guild_refused(self) -> None:
        ok, _ = await self._check(200, self.CONTROL)
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
