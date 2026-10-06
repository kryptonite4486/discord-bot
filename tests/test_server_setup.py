"""Tests for /setup, the trivia channel, and the per-channel command rules."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import discord  # noqa: E402

from bot.cogs.server_setup import ServerSetup  # noqa: E402
from bot.db import Database  # noqa: E402
from bot.main import LastZAssistant  # noqa: E402
from bot.utils.channel_rules import channel_rule, interaction_channel_ids  # noqa: E402

TRIVIA, DATA = "500", "600"


class ChannelRuleTests(unittest.TestCase):
    def test_nothing_is_restricted_without_a_trivia_channel(self) -> None:
        for command in ("trivia start", "trivia join", "ingest batch", "add versus"):
            with self.subTest(command=command):
                self.assertIsNone(channel_rule(command, {DATA}, None))

    def test_trivia_matches_only_in_the_trivia_channel(self) -> None:
        for command in ("trivia start", "trivia join"):
            with self.subTest(command=command):
                self.assertIsNone(channel_rule(command, {TRIVIA}, TRIVIA))
                self.assertIn(f"<#{TRIVIA}>", channel_rule(command, {DATA}, TRIVIA))
        # Leaderboards, settings and stopping work anywhere.
        for command in ("trivia leaderboard", "trivia stop", "trivia settings", "trivia reset"):
            with self.subTest(command=command):
                self.assertIsNone(channel_rule(command, {DATA}, TRIVIA))

    def test_data_entry_is_refused_in_the_trivia_channel(self) -> None:
        for command in ("add versus", "add general", "ingest batch", "ingest image", "ingest text"):
            with self.subTest(command=command):
                self.assertIn("can't be added here", channel_rule(command, {TRIVIA}, TRIVIA))
                self.assertIsNone(channel_rule(command, {DATA}, TRIVIA))
        # Reports only read data, so they still work in the trivia channel.
        for command in ("report week", "report leaderboard", "help", "planner", "setup"):
            with self.subTest(command=command):
                self.assertIsNone(channel_rule(command, {TRIVIA}, TRIVIA))

    def test_threads_follow_their_parent_channel(self) -> None:
        thread = SimpleNamespace(channel_id=700, channel=SimpleNamespace(parent_id=int(TRIVIA)))
        ids = interaction_channel_ids(thread)  # type: ignore[arg-type]
        self.assertEqual(ids, {"700", TRIVIA})
        self.assertIsNone(channel_rule("trivia start", ids, TRIVIA))
        self.assertIsNotNone(channel_rule("ingest batch", ids, TRIVIA))


def _command_interaction(name: str, channel_id: str, *, guild_id: int = 1):
    return SimpleNamespace(
        type=discord.InteractionType.application_command,
        command=SimpleNamespace(qualified_name=name),
        guild=SimpleNamespace(id=guild_id), guild_id=guild_id,
        channel_id=int(channel_id), channel=SimpleNamespace(parent_id=None),
        response=SimpleNamespace(send_message=AsyncMock(), is_done=lambda: False),
    )


class GlobalCheckTests(unittest.IsolatedAsyncioTestCase):
    """The bot's own interaction check applies the rules to every command."""

    def setUp(self) -> None:
        settings = SimpleNamespace(app_id=None, database_path=Path(":memory:"), legacy_guild_id=None)
        self.bot = LastZAssistant(settings)  # type: ignore[arg-type]
        self.bot.db = SimpleNamespace(
            guild_settings=AsyncMock(return_value={"trivia_channel_id": TRIVIA})
        )

    async def _check(self, name: str, channel_id: str):
        inter = _command_interaction(name, channel_id)
        allowed = await self.bot._guild_only_interaction(inter)  # type: ignore[arg-type]
        return allowed, inter.response.send_message

    async def test_blocks_and_explains(self) -> None:
        allowed, reply = await self._check("ingest batch", TRIVIA)
        self.assertFalse(allowed)
        self.assertTrue(reply.await_args.kwargs["ephemeral"])
        allowed, reply = await self._check("trivia start", DATA)
        self.assertFalse(allowed)
        self.assertIn(f"<#{TRIVIA}>", reply.await_args.args[0])

    async def test_allows_everything_else(self) -> None:
        for name, channel in (("trivia start", TRIVIA), ("ingest batch", DATA), ("report week", TRIVIA)):
            with self.subTest(name=name, channel=channel):
                allowed, reply = await self._check(name, channel)
                self.assertTrue(allowed)
                reply.assert_not_awaited()

    async def test_buttons_are_not_checked(self) -> None:
        inter = _command_interaction("trivia start", DATA)
        inter.type = discord.InteractionType.component
        self.assertTrue(await self.bot._guild_only_interaction(inter))  # type: ignore[arg-type]
        self.bot.db.guild_settings.assert_not_awaited()


class _DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "test.db"

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()


class SettingsStorageTests(_DbCase):
    async def test_set_clear_and_purge(self) -> None:
        db = Database(self.path)
        await db.connect()
        try:
            self.assertEqual(await db.guild_settings("1"), {"trivia_channel_id": None})
            await db.set_trivia_channel("1", TRIVIA)
            self.assertEqual(await db.guild_settings("1"), {"trivia_channel_id": TRIVIA})
            self.assertEqual(await db.guilds_with_data(), {"1"})
            await db.set_trivia_channel("1", None)
            self.assertEqual(await db.guild_settings("1"), {"trivia_channel_id": None})
            await db.purge_guild("1")
            self.assertEqual(await db.guilds_with_data(), set())
        finally:
            await db.close()

    async def test_old_announcement_channel_becomes_the_trivia_channel(self) -> None:
        # The first trivia build stored an invitation channel in TriviaSettings.
        con = sqlite3.connect(self.path)
        con.executescript(
            """
            CREATE TABLE TriviaSettings (
                GuildId TEXT PRIMARY KEY, AllowGlobal INTEGER NOT NULL DEFAULT 1,
                AnnounceChannelId TEXT
            );
            INSERT INTO TriviaSettings VALUES ('1', 1, '55'), ('2', 0, NULL);
            """
        )
        con.commit()
        con.close()
        db = Database(self.path)
        await db.connect()
        try:
            self.assertEqual(await db.guild_settings("1"), {"trivia_channel_id": "55"})
            self.assertEqual(await db.guild_settings("2"), {"trivia_channel_id": None})
            self.assertFalse((await db.trivia_settings("2"))["allow_global"])
            self.assertNotIn("AnnounceChannelId", await db._table_columns("TriviaSettings"))
        finally:
            await db.close()
        # Connecting again finds nothing left to migrate.
        db = Database(self.path)
        await db.connect()
        await db.close()


def _channel(cid: int, *, can_post: bool = True):
    perms = SimpleNamespace(view_channel=True, send_messages=can_post, embed_links=True)
    return SimpleNamespace(id=cid, mention=f"<#{cid}>", permissions_for=lambda me: perms)


class SetupCommandTests(_DbCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.db = Database(self.path)
        await self.db.connect()
        self.cog = ServerSetup(SimpleNamespace(db=self.db))  # type: ignore[arg-type]

    async def asyncTearDown(self) -> None:
        await self.db.close()
        await super().asyncTearDown()

    async def _run(self, **options) -> str:
        inter = SimpleNamespace(
            guild_id=1, guild=SimpleNamespace(me=object()), user=SimpleNamespace(id=9),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        await self.cog.setup_command.callback(
            self.cog, inter, options.get("trivia_channel"), options.get("clear", False)
        )
        args, kwargs = inter.response.send_message.await_args
        self.assertTrue(kwargs["ephemeral"])
        return args[0]

    async def test_show_set_and_clear(self) -> None:
        text = await self._run()
        self.assertIn("not set", text)
        self.assertIn("Server Settings → Integrations", text)
        text = await self._run(trivia_channel=_channel(500))
        self.assertIn("Trivia channel: <#500>", text)
        self.assertEqual(await self.db.guild_settings("1"), {"trivia_channel_id": "500"})
        self.assertIn("<#500>", await self._run())  # shown again with no options
        self.assertIn("not set", await self._run(clear=True))
        self.assertEqual(await self.db.guild_settings("1"), {"trivia_channel_id": None})

    async def test_refuses_channels_the_bot_cant_post_in(self) -> None:
        text = await self._run(trivia_channel=_channel(500, can_post=False))
        self.assertIn("I can't post in <#500>", text)
        self.assertEqual(await self.db.guild_settings("1"), {"trivia_channel_id": None})

    async def test_set_and_clear_together_is_an_error(self) -> None:
        text = await self._run(trivia_channel=_channel(500), clear=True)
        self.assertIn("not both", text)

    def test_hidden_from_members_without_manage_server(self) -> None:
        perms = self.cog.setup_command.default_permissions
        self.assertTrue(perms.manage_guild)


if __name__ == "__main__":
    unittest.main()
