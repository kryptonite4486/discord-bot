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

from bot.cogs.ops import Ops  # noqa: E402
from bot.cogs.server_setup import ServerSetup  # noqa: E402
from bot.db import Database  # noqa: E402
from bot.main import LastZAssistant  # noqa: E402
from bot.utils.channel_rules import channel_rule, interaction_channel_ids  # noqa: E402
from bot.utils.report_channel import ReportChannelUnavailable, send_to_report_channel  # noqa: E402

TRIVIA, DATA = "500", "600"
UNSET = {"trivia_channel_id": None, "report_channel_id": None, "default_week": None}


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
            self.assertEqual(await db.guild_settings("1"), UNSET)
            await db.set_trivia_channel("1", TRIVIA)
            self.assertEqual(await db.guild_settings("1"), {**UNSET, "trivia_channel_id": TRIVIA})
            self.assertEqual(await db.guilds_with_data(), {"1"})
            await db.set_trivia_channel("1", None)
            self.assertEqual(await db.guild_settings("1"), UNSET)
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
            self.assertEqual(await db.guild_settings("1"), {**UNSET, "trivia_channel_id": "55"})
            self.assertEqual(await db.guild_settings("2"), UNSET)
            self.assertFalse((await db.trivia_settings("2"))["allow_global"])
            self.assertNotIn("AnnounceChannelId", await db._table_columns("TriviaSettings"))
        finally:
            await db.close()
        # Connecting again finds nothing left to migrate.
        db = Database(self.path)
        await db.connect()
        await db.close()


class ReportChannelAndWeekStorageTests(_DbCase):
    async def test_set_clear_and_helper(self) -> None:
        db = Database(self.path)
        await db.connect()
        try:
            self.assertIsNone(await db.report_channel_id("1"))
            await db.set_report_channel("1", "700")
            await db.set_default_week("1", "last")
            self.assertEqual(await db.report_channel_id("1"), "700")
            self.assertEqual(
                await db.guild_settings("1"),
                {**UNSET, "report_channel_id": "700", "default_week": "last"},
            )
            # Each setting is independent of the others.
            await db.set_trivia_channel("1", TRIVIA)
            await db.set_report_channel("1", None)
            self.assertEqual(
                await db.guild_settings("1"),
                {"trivia_channel_id": TRIVIA, "report_channel_id": None, "default_week": "last"},
            )
            with self.assertRaises(ValueError):
                await db.set_default_week("1", "2026-01-04")
        finally:
            await db.close()

    async def test_older_settings_table_gets_the_new_columns(self) -> None:
        con = sqlite3.connect(self.path)
        con.executescript(
            """
            CREATE TABLE GuildSettings (
                GuildId TEXT PRIMARY KEY, TriviaChannelId TEXT,
                UpdatedAt TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO GuildSettings (GuildId, TriviaChannelId) VALUES ('1', '55');
            """
        )
        con.commit()
        con.close()
        db = Database(self.path)
        await db.connect()
        try:
            self.assertEqual(await db.guild_settings("1"), {**UNSET, "trivia_channel_id": "55"})
            await db.set_report_channel("1", "700")
        finally:
            await db.close()
        # Connecting again finds nothing left to migrate and keeps the value.
        db = Database(self.path)
        await db.connect()
        try:
            self.assertEqual(await db.report_channel_id("1"), "700")
        finally:
            await db.close()


def _channel(cid: int, *, can_post: bool = True, embeds: bool = True):
    perms = SimpleNamespace(view_channel=True, send_messages=can_post, embed_links=embeds)
    return SimpleNamespace(
        id=cid, mention=f"<#{cid}>", permissions_for=lambda me: perms,
        send=AsyncMock(side_effect=lambda **kw: SimpleNamespace(channel=SimpleNamespace(mention=f"<#{cid}>"))),
    )


def _week(value: str):
    return discord.app_commands.Choice(name=value, value=value)


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
        await self.cog.setup_command.callback(self.cog, inter, **options)
        args, kwargs = inter.response.send_message.await_args
        self.assertTrue(kwargs["ephemeral"])
        return args[0]

    async def test_show_set_and_clear(self) -> None:
        text = await self._run()
        self.assertIn("Trivia channel: not set", text)
        self.assertIn("Report channel: not set", text)
        self.assertIn("Default week: not set, so the current week", text)
        self.assertIn("Server Settings → Integrations", text)
        text = await self._run(trivia_channel=_channel(500))
        self.assertIn("Trivia channel: <#500>", text)
        self.assertEqual((await self.db.guild_settings("1"))["trivia_channel_id"], "500")
        self.assertIn("<#500>", await self._run())  # shown again with no options
        self.assertIn("Trivia channel: not set", await self._run(clear_trivia_channel=True))
        self.assertIsNone((await self.db.guild_settings("1"))["trivia_channel_id"])

    async def test_report_channel_and_default_week(self) -> None:
        text = await self._run(report_channel=_channel(700), default_week=_week("last"))
        self.assertIn("Report channel: <#700>", text)
        self.assertIn("Default week: last week", text)
        self.assertEqual(
            await self.db.guild_settings("1"),
            {**UNSET, "report_channel_id": "700", "default_week": "last"},
        )
        # Changing one option leaves the others alone.
        await self._run(trivia_channel=_channel(500))
        self.assertEqual(await self.db.report_channel_id("1"), "700")
        text = await self._run(clear_report_channel=True, default_week=_week("default"))
        self.assertIn("Report channel: not set", text)
        self.assertIn("Default week: not set", text)
        self.assertEqual(await self.db.guild_settings("1"), {**UNSET, "trivia_channel_id": "500"})

    async def test_bot_default_week_is_shown(self) -> None:
        self.cog.bot.settings = SimpleNamespace(default_week_start="last")
        self.assertIn("the bot's default (`last`)", await self._run())

    async def test_refuses_channels_the_bot_cant_post_in(self) -> None:
        text = await self._run(trivia_channel=_channel(500, can_post=False))
        self.assertIn("I can't post in <#500>", text)
        self.assertIsNone((await self.db.guild_settings("1"))["trivia_channel_id"])
        text = await self._run(report_channel=_channel(700, embeds=False), default_week=_week("last"))
        self.assertIn("I can't post in <#700>. Give me Embed Links", text)
        # Nothing is saved when any option is refused.
        self.assertEqual(await self.db.guild_settings("1"), UNSET)

    async def test_set_and_clear_together_is_an_error(self) -> None:
        text = await self._run(trivia_channel=_channel(500), clear_trivia_channel=True)
        self.assertIn("not both", text)
        text = await self._run(report_channel=_channel(700), clear_report_channel=True)
        self.assertIn("`report_channel` or `clear_report_channel`", text)
        self.assertEqual(await self.db.guild_settings("1"), UNSET)

    def test_hidden_from_members_without_manage_server(self) -> None:
        perms = self.cog.setup_command.default_permissions
        self.assertTrue(perms.manage_guild)


class _FakeBot:
    """Enough of the bot for send_to_report_channel: db plus get_guild."""

    def __init__(self, db, channels: dict[int, object] | None = None, in_guild: bool = True) -> None:
        self.db = db
        self._guild = SimpleNamespace(
            id=1, name="Alliance", me=object(), get_channel=lambda cid: (channels or {}).get(cid)
        ) if in_guild else None

    def get_guild(self, guild_id: int):
        return self._guild if guild_id == 1 else None


class SendToReportChannelTests(_DbCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.db = Database(self.path)
        await self.db.connect()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        await super().asyncTearDown()

    async def test_posts_in_the_report_channel(self) -> None:
        channel = _channel(700)
        await self.db.set_report_channel("1", "700")
        await send_to_report_channel(_FakeBot(self.db, {700: channel}), "1", content="hi")
        channel.send.assert_awaited_once_with(content="hi")

    async def test_explains_why_nothing_was_posted(self) -> None:
        with self.assertRaisesRegex(ReportChannelUnavailable, "no report channel set"):
            await send_to_report_channel(_FakeBot(self.db), "1", content="hi")
        await self.db.set_report_channel("1", "700")
        with self.assertRaisesRegex(ReportChannelUnavailable, "isn't in that server"):
            await send_to_report_channel(_FakeBot(self.db, in_guild=False), "1", content="hi")
        with self.assertRaisesRegex(ReportChannelUnavailable, "no longer exists"):
            await send_to_report_channel(_FakeBot(self.db, {}), "1", content="hi")
        channel = _channel(700, can_post=False)
        with self.assertRaisesRegex(ReportChannelUnavailable, "lacks Send Messages"):
            await send_to_report_channel(_FakeBot(self.db, {700: channel}), "1", content="hi")
        channel.send.assert_not_awaited()


class GrantNotifyTests(_DbCase):
    """/ops grant notify:true posts a thank-you in the report channel."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.db = Database(self.path)
        await self.db.connect()
        self.channel = _channel(700)
        self.bot = _FakeBot(self.db, {700: self.channel})
        self.bot.tiers = SimpleNamespace(invalidate=lambda guild_id: None)
        self.bot.guilds = []
        self.ops = Ops(self.bot)  # type: ignore[arg-type]

    async def asyncTearDown(self) -> None:
        await self.db.close()
        await super().asyncTearDown()

    async def _grant(self, *, notify: bool):
        inter = SimpleNamespace(
            user=SimpleNamespace(id=42, __str__=lambda self: "op"),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        tier = discord.app_commands.Choice(name="Full", value="full")
        duration = discord.app_commands.Choice(name="Permanent", value="permanent")
        await self.ops.grant.callback(self.ops, inter, "1", tier, duration, "thanks", notify)
        return inter

    async def test_posts_a_thank_you(self) -> None:
        await self.db.set_report_channel("1", "700")
        inter = await self._grant(notify=True)
        embed = self.channel.send.await_args.kwargs["embed"]
        self.assertIn("**Full**", embed.description)
        self.assertNotIn("thanks", embed.description)  # the reason stays private
        reply = inter.followup.send.await_args.args[0]
        self.assertIn("Thank-you posted in <#700>", reply)
        self.assertEqual(len(await self.db.entitlements_for("1")), 1)

    async def test_tells_the_operator_when_there_is_no_report_channel(self) -> None:
        inter = await self._grant(notify=True)
        reply = inter.followup.send.await_args.args[0]
        self.assertIn("Thank-you **not** posted", reply)
        self.assertIn("no report channel set", reply)
        self.assertIn("Gifted **Full**", reply)  # the gift still went through
        self.channel.send.assert_not_awaited()

    async def test_no_post_without_notify(self) -> None:
        await self.db.set_report_channel("1", "700")
        inter = await self._grant(notify=False)
        self.channel.send.assert_not_awaited()
        self.assertNotIn("Thank-you", inter.response.send_message.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
