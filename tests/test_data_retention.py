"""Tests for data deletion and retention after the bot is removed."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.cogs import data as data_cog  # noqa: E402
from bot.cogs.admin import Admin  # noqa: E402
from bot.cogs.data import Data  # noqa: E402
from bot.utils.retention import (  # noqa: E402
    RemovedServer,
    deletion_date,
    fmt_time,
    plan_reconcile,
    removed_servers,
)
from bot.cogs.ops import Ops, format_purges_report  # noqa: E402
from bot.db import Database  # noqa: E402

A, B, C = "111", "222", "333"


async def _seed(db: Database) -> None:
    for guild, channel, player in (
        (A, "c1", "Ann"),
        (A, "c1", "Bob"),
        (A, "c2", "Cy"),
        (B, "c9", "Dee"),
    ):
        await db.upsert_metric(guild, "2026-10-04", player, "Power", 1.0, channel_id=channel)


class _DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "test.db")
        await self.db.connect()
        await _seed(self.db)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()


class DatabaseTests(_DbCase):
    async def test_delete_one_channel(self) -> None:
        self.assertEqual(await self.db.delete_guild_data(A, channel_id="c1"), 2)
        self.assertEqual((await self.db.stats(A, include_unassigned=True))["rows"], 1)
        self.assertEqual((await self.db.stats(B, include_unassigned=True))["rows"], 1)

    async def test_delete_whole_server_keeps_usage(self) -> None:
        await self.db.add_usage(A, "2026-10-05", {"ocr_images": 4})
        self.assertEqual(await self.db.delete_guild_data(A), 3)
        self.assertEqual(await self.db.guilds_with_data(), {B})
        self.assertEqual((await self.db.usage_by_guild("2026-10-01"))[A]["ocr_images"], 4)

    async def test_schedule_keeps_first_date(self) -> None:
        self.assertTrue(await self.db.schedule_purge(A, "2026-10-01 00:00:00"))
        self.assertFalse(await self.db.schedule_purge(A, "2026-10-05 00:00:00"))
        (pending,) = await self.db.pending_purges()
        self.assertEqual(pending["RemovedAt"], "2026-10-01 00:00:00")

    async def test_cancel_and_purge(self) -> None:
        await self.db.schedule_purge(A, "t0")
        self.assertTrue(await self.db.cancel_purge(A))
        self.assertFalse(await self.db.cancel_purge(A))
        await self.db.schedule_purge(A, "t0")
        self.assertEqual(await self.db.purge_guild(A), 3)
        self.assertEqual(await self.db.pending_purges(), [])
        self.assertEqual(await self.db.guilds_with_data(), {B})

    async def _entitle(self, starts, ends=None, revoked=None, guild=A) -> None:
        await self.db.conn.execute(
            "INSERT INTO GuildEntitlement (GuildId, Tier, Source, StartsAt, EndsAt, RevokedAt)"
            " VALUES (?, 'full', 'gift', ?, ?, ?)",
            (guild, starts, ends, revoked),
        )
        await self.db.conn.commit()

    async def test_subscription_status(self) -> None:
        now = "2026-10-05 12:00:00"
        self.assertEqual(await self.db.subscription_status(A, now), (False, None))
        await self._entitle("2026-01-01 00:00:00", "2026-03-01 00:00:00")
        await self._entitle("2026-03-01 00:00:00", "2026-12-01 00:00:00", revoked="2026-06-15 00:00:00")
        self.assertEqual(
            await self.db.subscription_status(A, now), (False, "2026-06-15 00:00:00")
        )
        await self._entitle("2026-09-01 00:00:00")  # permanent gift
        self.assertEqual((await self.db.subscription_status(A, now))[0], True)
        # Other servers and future subscriptions don't count.
        await self._entitle("2027-01-01 00:00:00", guild=B)
        self.assertEqual(await self.db.subscription_status(B, now), (False, None))


class DeletionDateTests(unittest.TestCase):
    T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    DAYS30 = timedelta(days=30)

    def test_no_subscription(self) -> None:
        self.assertEqual(
            deletion_date(self.T0, subscription_active=False, subscription_ended=None, retention=self.DAYS30),
            self.T0 + self.DAYS30,
        )

    def test_active_subscription_keeps_data(self) -> None:
        self.assertIsNone(
            deletion_date(self.T0, subscription_active=True, subscription_ended=None, retention=self.DAYS30)
        )

    def test_subscription_ended_after_removal_starts_clock_then(self) -> None:
        ended = self.T0 + timedelta(days=90)
        self.assertEqual(
            deletion_date(self.T0, subscription_active=False, subscription_ended=ended, retention=self.DAYS30),
            ended + self.DAYS30,
        )

    def test_subscription_ended_before_removal_uses_removal(self) -> None:
        ended = self.T0 - timedelta(days=90)
        self.assertEqual(
            deletion_date(self.T0, subscription_active=False, subscription_ended=ended, retention=self.DAYS30),
            self.T0 + self.DAYS30,
        )


class ReconcileTests(unittest.TestCase):
    def test_schedules_removed_and_cancels_returned(self) -> None:
        to_schedule, to_cancel = plan_reconcile(
            data_guilds={A, B, C, "legacy"},
            member_guilds={A, C},
            pending={C},
        )
        self.assertEqual(to_schedule, {B})  # legacy bucket is never scheduled
        self.assertEqual(to_cancel, {C})

    def test_already_scheduled_not_rescheduled(self) -> None:
        to_schedule, _ = plan_reconcile({A, B}, {A}, {B})
        self.assertEqual(to_schedule, set())


class LifecycleTests(_DbCase):
    def _cog(self, member=(A,), backups=False) -> Data:
        bot = SimpleNamespace(
            db=self.db,
            settings=SimpleNamespace(data_retention_days=30, backup_dir=None, backup_keep=14),
            guilds=[SimpleNamespace(id=int(g)) for g in member],
            backups_enabled=backups,
        )
        return Data(bot)  # type: ignore[arg-type]

    async def test_removal_schedules_deletion_30_days_out(self) -> None:
        cog = self._cog()
        with self.assertLogs("bot.cogs.data", level="WARNING"):
            await cog.on_guild_remove(SimpleNamespace(id=int(A)))
        (server,) = await removed_servers(self.db, timedelta(days=30), datetime.now(timezone.utc))
        self.assertEqual(server.deletes_at - server.removed_at, timedelta(days=30))

    async def test_rejoin_cancels_deletion(self) -> None:
        cog = self._cog()
        await cog.on_guild_remove(SimpleNamespace(id=int(A)))
        await cog.on_guild_join(SimpleNamespace(id=int(A)))
        self.assertEqual(await self.db.pending_purges(), [])

    async def test_startup_schedules_server_removed_while_offline(self) -> None:
        cog = self._cog(member=(A,))  # B has data but the bot isn't in it
        await cog.on_ready()
        self.assertEqual([p["GuildId"] for p in await self.db.pending_purges()], [B])

    async def test_startup_with_no_guilds_schedules_nothing(self) -> None:
        cog = self._cog(member=())
        with self.assertLogs("bot.cogs.data", level="WARNING"):
            await cog.on_ready()
        self.assertEqual(await self.db.pending_purges(), [])

    async def test_purge_only_deletes_due_servers(self) -> None:
        cog = self._cog(member=())
        now = datetime.now(timezone.utc)
        await self.db.schedule_purge(A, fmt_time(now - timedelta(days=31)))
        await self.db.schedule_purge(B, fmt_time(now))
        with self.assertLogs("bot.cogs.data", level="WARNING"):
            await cog.purge_due.coro(cog)
        self.assertEqual(await self.db.guilds_with_data(), {B})
        self.assertEqual([p["GuildId"] for p in await self.db.pending_purges()], [B])

    async def test_purge_skips_server_the_bot_is_back_in(self) -> None:
        cog = self._cog(member=(A,))
        await self.db.schedule_purge(A, "2026-01-01 00:00:00")
        await cog.purge_due.coro(cog)
        self.assertIn(A, await self.db.guilds_with_data())
        self.assertEqual(await self.db.pending_purges(), [])

    async def test_active_subscription_keeps_removed_server(self) -> None:
        cog = self._cog(member=())
        now = datetime.now(timezone.utc)
        await self.db.schedule_purge(A, fmt_time(now - timedelta(days=200)))
        await self.db.conn.execute(
            "INSERT INTO GuildEntitlement (GuildId, Tier, Source, StartsAt, EndsAt)"
            " VALUES (?, 'full', 'discord', ?, ?)",
            (A, fmt_time(now - timedelta(days=300)), fmt_time(now + timedelta(days=5))),
        )
        await self.db.conn.commit()
        await cog.purge_due.coro(cog)
        self.assertIn(A, await self.db.guilds_with_data())
        (server,) = await removed_servers(self.db, timedelta(days=30), now)
        self.assertIsNone(server.deletes_at)

    async def test_lapsed_subscription_starts_the_30_days(self) -> None:
        cog = self._cog(member=())
        now = datetime.now(timezone.utc)
        await self.db.schedule_purge(A, fmt_time(now - timedelta(days=200)))
        await self.db.conn.execute(
            "INSERT INTO GuildEntitlement (GuildId, Tier, Source, StartsAt, EndsAt)"
            " VALUES (?, 'mid', 'discord', ?, ?)",
            (A, fmt_time(now - timedelta(days=300)), fmt_time(now - timedelta(days=10))),
        )
        await self.db.conn.commit()
        await cog.purge_due.coro(cog)  # 10 days after it lapsed: still kept
        self.assertIn(A, await self.db.guilds_with_data())
        (server,) = await removed_servers(self.db, timedelta(days=30), now)
        self.assertEqual(server.deletes_at.date(), (now + timedelta(days=20)).date())

    async def test_failed_backup_postpones_purge(self) -> None:
        cog = self._cog(member=(), backups=True)
        await self.db.schedule_purge(A, "2026-01-01 00:00:00")

        async def broken_backup(*args, **kwargs):
            raise OSError("backup disk full")

        with patch.object(data_cog, "create_backup", broken_backup):
            with self.assertLogs("bot.cogs.data", level="ERROR"):
                await cog.purge_due.coro(cog)
        self.assertIn(A, await self.db.guilds_with_data())
        self.assertEqual(len(await self.db.pending_purges()), 1)


class _Response:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.view = None

    async def send_message(self, content, view=None, ephemeral=False):
        self.messages.append(content)
        self.view = view

    def is_done(self) -> bool:
        return bool(self.messages)


class DeleteCommandTests(_DbCase):
    def _run(self, *, scope=None, outcome="confirm"):
        settings = SimpleNamespace(data_retention_days=30)
        cog = Data(SimpleNamespace(db=self.db, settings=settings, backups_enabled=False))  # type: ignore[arg-type]
        edits: list[str] = []

        async def edit_original_response(content=None, view=None):
            edits.append(content)

        interaction = SimpleNamespace(
            guild_id=int(A),
            channel_id="c1",
            guild=None,
            user=SimpleNamespace(id=5),
            response=_Response(),
            edit_original_response=edit_original_response,
        )

        async def fake_wait(view):
            view.confirmed = outcome == "confirm"
            return outcome == "timeout"

        async def run():
            with patch.object(data_cog.ConfirmDeleteView, "wait", fake_wait):
                with patch.object(data_cog, "channel_id_from_interaction", lambda i: "c1"):
                    await cog.delete.callback(cog, interaction, scope)
            return interaction.response.messages, edits

        return run()

    async def test_confirm_deletes_only_this_channel(self) -> None:
        with self.assertLogs("bot.cogs.data", level="WARNING"):
            prompts, edits = await self._run()
        self.assertIn("**2** row(s)", prompts[0])
        self.assertIn("Deleted **2** row(s)", edits[-1])
        self.assertEqual((await self.db.stats(A, include_unassigned=True))["rows"], 1)

    async def test_entire_server_scope(self) -> None:
        scope = SimpleNamespace(value="server")
        with self.assertLogs("bot.cogs.data", level="WARNING"):
            await self._run(scope=scope)
        self.assertEqual(await self.db.guilds_with_data(), {B})

    async def test_cancel_and_timeout_delete_nothing(self) -> None:
        for outcome in ("cancel", "timeout"):
            with self.subTest(outcome=outcome):
                await self._run(outcome=outcome)
                self.assertEqual((await self.db.stats(A, include_unassigned=True))["rows"], 3)
        _, edits = await self._run(outcome="timeout")
        self.assertIn("Nothing was deleted", edits[-1])


class ReportAndHandlerTests(unittest.TestCase):
    def test_purges_report(self) -> None:
        self.assertIn("None.", format_purges_report([], 30))
        t0 = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
        text = format_purges_report(
            [
                RemovedServer(B, t0, None),
                RemovedServer(A, t0, t0 + timedelta(days=30)),
            ],
            30,
        )
        self.assertIn("kept 30 day(s)", text)
        lines = text.splitlines()
        a_line = next(i for i, l in enumerate(lines) if l.startswith(A))
        b_line = next(i for i, l in enumerate(lines) if l.startswith(B))
        self.assertIn("2026-10-31 12:00", lines[a_line])
        self.assertIn("kept: subscribed", lines[b_line])
        self.assertLess(a_line, b_line)  # soonest deletion first

    def test_cogs_with_own_error_handlers_are_detected(self) -> None:
        # The bot-wide handler skips these so users don't get two error replies.
        settings = SimpleNamespace(bot_owner_ids=frozenset(), control_guild_id=1, data_retention_days=30)
        bot = SimpleNamespace(settings=settings)
        for cog in (Ops(bot), Data(bot), Admin(bot)):  # type: ignore[arg-type]
            for command in cog.walk_app_commands():
                if hasattr(command, "_has_any_error_handlers"):
                    self.assertTrue(command._has_any_error_handlers(), command.qualified_name)

    def test_admin_and_data_commands_require_administrator(self) -> None:
        bot = SimpleNamespace(settings=SimpleNamespace())
        for cog, group in ((Admin(bot), "admin"), (Data(bot), "data")):  # type: ignore[arg-type]
            (top,) = cog.get_app_commands()
            self.assertEqual(top.name, group)
            # Hidden from members without Administrator...
            self.assertTrue(top.default_permissions.administrator, group)
            # ...and refused if a server widens who can see it.
            for command in top.walk_commands():
                self.assertTrue(command.checks, command.qualified_name)


if __name__ == "__main__":
    unittest.main()
