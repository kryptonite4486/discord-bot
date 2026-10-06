"""Tests for gift expiry reminders: windows, dedupe, re-arming, paid overlap
and the report channel."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import discord  # noqa: E402

from bot.db import Database  # noqa: E402
from bot.utils.gift_reminders import (  # noqa: E402
    MAX_MESSAGE,
    STAGE_OPERATOR,
    GiftReminders,
    format_operator_summary,
)

TS = "%Y-%m-%d %H:%M:%S"
START = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
GUILD = "111"
CHANNEL = "222"
OPERATOR = 42


def at(days: float) -> str:
    return (START + timedelta(days=days)).strftime(TS)


class _Case(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "t.db")
        await self.db.connect()
        self.now = START
        self.channel = SimpleNamespace(send=AsyncMock())
        self.guild = SimpleNamespace(
            id=int(GUILD), name="Wolves",
            get_channel=lambda cid: self.channel if str(cid) == CHANNEL else None,
        )
        self.operator = SimpleNamespace(send=AsyncMock())
        self.bot = SimpleNamespace(
            db=self.db,
            settings=SimpleNamespace(bot_owner_ids=frozenset({OPERATOR})),
            guilds=[self.guild],
            get_guild=lambda gid: self.guild if str(gid) == GUILD else None,
            get_user=lambda uid: self.operator if uid == OPERATOR else None,
            fetch_user=AsyncMock(side_effect=discord.NotFound(SimpleNamespace(status=404, reason=""), "")),
        )
        self.reminders = GiftReminders(self.bot, clock=lambda: self.now)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def grant(self, ends_days, *, source="gift", tier="full", guild=GUILD) -> int:
        return await self.db.add_entitlement(
            guild, tier, source, starts_at=at(-30),
            ends_at=None if ends_days is None else at(ends_days),
            granted_by="op", reason="test",
        )

    async def set_report_channel(self, channel_id: str | None) -> None:
        await self.db.set_report_channel(GUILD, channel_id)

    def dm_text(self) -> str:
        return "\n".join(c.args[0] for c in self.operator.send.await_args_list)


class OperatorSummaryTests(_Case):
    async def test_seven_day_window(self) -> None:
        soon = await self.grant(6.9)
        await self.grant(7.1)  # not yet
        await self.grant(None)  # permanent
        await self.grant(-1)  # already ended
        await self.reminders.run()
        self.operator.send.assert_awaited_once()
        text = self.dm_text()
        self.assertIn("**Wolves** `111`: Command (gift)", text)
        self.assertIn(at(6.9)[:16], text)
        self.assertIn(f"`/ops extend entitlement_id:{soon} duration:30 days`", text)
        self.assertEqual(text.count("/ops extend"), 1)

    async def test_code_and_gift_only(self) -> None:
        await self.grant(3, source="code", tier="mid")
        await self.grant(3, source="discord")
        await self.grant(3, source="trial")
        await self.reminders.run()
        text = self.dm_text()
        self.assertIn("Alliance (code)", text)
        self.assertEqual(text.count("/ops extend"), 1)

    async def test_one_summary_for_several_gifts(self) -> None:
        await self.grant(2)
        await self.grant(5, guild="999")
        await self.reminders.run()
        self.operator.send.assert_awaited_once()
        self.assertIn("**2 gifted plan(s)", self.dm_text())
        self.assertIn("**Unknown server (bot not in it)** `999`", self.dm_text())

    async def test_sent_once_across_runs_and_restarts(self) -> None:
        await self.grant(6)
        await self.reminders.run()
        self.now += timedelta(days=1)
        await self.reminders.run()
        # A restart: new reminder object, same database.
        await GiftReminders(self.bot, clock=lambda: self.now).run()
        self.operator.send.assert_awaited_once()

    async def test_new_gift_entering_window_gets_its_own_summary(self) -> None:
        await self.grant(6)
        await self.reminders.run()
        await self.grant(9)
        self.now += timedelta(days=3)
        await self.reminders.run()
        self.assertEqual(self.operator.send.await_count, 2)
        second = self.operator.send.await_args_list[1].args[0]
        self.assertIn("**1 gifted plan(s)", second)
        self.assertIn(at(9)[:16], second)

    async def test_extension_rearms(self) -> None:
        gift = await self.grant(6)
        await self.reminders.run()
        await self.db.set_entitlement_end(gift, at(36), actor_id="op")
        await self.reminders.run()  # 36 days out: nothing yet
        self.operator.send.assert_awaited_once()
        self.now += timedelta(days=30)
        await self.reminders.run()
        self.assertEqual(self.operator.send.await_count, 2)
        self.assertIn(at(36)[:16], self.operator.send.await_args_list[1].args[0])

    async def test_failed_dm_is_retried(self) -> None:
        await self.grant(6)
        self.operator.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason=""), "")
        with self.assertLogs("bot.utils.gift_reminders", level="WARNING"):
            await self.reminders.run()
        self.assertFalse(await self.db.reminder_sent(1, STAGE_OPERATOR, at(6)))
        self.operator.send.side_effect = None
        await self.reminders.run()
        self.assertTrue(await self.db.reminder_sent(1, STAGE_OPERATOR, at(6)))

    async def test_no_operators_configured(self) -> None:
        self.bot.settings.bot_owner_ids = frozenset()
        await self.grant(6)
        await self.reminders.run()
        self.operator.send.assert_not_awaited()

    def test_long_summary_is_split(self) -> None:
        gifts = [
            {"Id": i, "GuildId": str(1000 + i), "Tier": "full", "Source": "gift", "EndsAt": at(3)}
            for i in range(60)
        ]
        messages = format_operator_summary(gifts, {})
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(m) <= MAX_MESSAGE for m in messages))
        self.assertEqual(sum(m.count("/ops extend") for m in messages), 60)


class ServerHeadsUpTests(_Case):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot.settings.bot_owner_ids = frozenset()  # server stage only
        await self.set_report_channel(CHANNEL)

    async def test_three_day_window(self) -> None:
        await self.grant(3.5)
        await self.reminders.run()
        self.channel.send.assert_not_awaited()
        self.now += timedelta(days=0.6)
        await self.reminders.run()
        self.channel.send.assert_awaited_once()
        text = self.channel.send.await_args.args[0]
        self.assertIn("gifted **Command** plan ends on **2026-10-09**", text)
        self.assertIn("will be on **Free**", text)

    async def test_names_plan_after_expiry(self) -> None:
        await self.grant(2, tier="full")
        await self.grant(None, tier="mid")
        await self.reminders.run()
        self.assertIn("will be on **Alliance — gifted**", self.channel.send.await_args.args[0])

    async def test_posted_once(self) -> None:
        await self.grant(2)
        await self.reminders.run()
        self.now += timedelta(hours=1)
        await self.reminders.run()
        await GiftReminders(self.bot, clock=lambda: self.now).run()
        self.channel.send.assert_awaited_once()

    async def test_extension_rearms(self) -> None:
        gift = await self.grant(2)
        await self.reminders.run()
        await self.db.set_entitlement_end(gift, at(32), actor_id="op")
        self.now += timedelta(days=29.5)
        await self.reminders.run()
        self.assertEqual(self.channel.send.await_count, 2)
        self.assertIn(at(32)[:10], self.channel.send.await_args.args[0])

    async def test_skipped_when_paid_outlasts_gift(self) -> None:
        await self.grant(2)
        await self.grant(20, source="discord", tier="mid")
        await self.reminders.run()
        self.channel.send.assert_not_awaited()

    async def test_skipped_when_paid_has_no_end(self) -> None:
        await self.grant(2)
        await self.grant(None, source="stripe")
        await self.reminders.run()
        self.channel.send.assert_not_awaited()

    async def test_posted_when_paid_ends_first(self) -> None:
        await self.grant(2)
        await self.grant(1, source="discord")
        await self.reminders.run()
        self.channel.send.assert_awaited_once()

    async def test_paid_in_other_server_does_not_count(self) -> None:
        await self.grant(2)
        await self.grant(None, source="discord", guild="999")
        await self.reminders.run()
        self.channel.send.assert_awaited_once()

    async def test_no_report_channel_skips_then_posts_once_set(self) -> None:
        await self.set_report_channel(None)
        await self.grant(2)
        await self.reminders.run()
        self.channel.send.assert_not_awaited()
        await self.set_report_channel(CHANNEL)
        await self.reminders.run()
        self.channel.send.assert_awaited_once()

    async def test_report_channel_deleted(self) -> None:
        await self.set_report_channel("333")  # not in the guild any more
        await self.grant(2)
        await self.reminders.run()
        self.channel.send.assert_not_awaited()

    async def test_operator_and_server_stages_are_separate(self) -> None:
        self.bot.settings.bot_owner_ids = frozenset({OPERATOR})
        await self.grant(2)
        await self.reminders.run()
        self.operator.send.assert_awaited_once()
        self.channel.send.assert_awaited_once()


class ReportChannelHelperTests(_Case):
    async def test_none_when_unset(self) -> None:
        self.assertIsNone(await self.db.report_channel_id(GUILD))

    async def test_reads_configured_channel(self) -> None:
        await self.set_report_channel(CHANNEL)
        self.assertEqual(await self.db.report_channel_id(GUILD), CHANNEL)
        self.assertIsNone(await self.db.report_channel_id("999"))


if __name__ == "__main__":
    unittest.main()
