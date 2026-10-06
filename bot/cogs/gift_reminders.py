"""Daily gift expiry reminders (see bot/utils/gift_reminders.py)."""

from __future__ import annotations

import logging

from discord.ext import commands, tasks

from bot.utils.gift_reminders import GiftReminders

log = logging.getLogger(__name__)


class GiftReminderTask(commands.Cog):
    """Reminds operators and gifted servers before gifts expire."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.reminders = GiftReminders(bot)

    async def cog_load(self) -> None:
        self.check_gifts.start()

    async def cog_unload(self) -> None:
        self.check_gifts.cancel()

    # A daily reminder, checked hourly like the backup and retention jobs so a
    # restart or a sleeping Mac delays it by at most an hour. EntitlementReminder
    # keeps each reminder to one send.
    @tasks.loop(hours=1)
    async def check_gifts(self) -> None:
        try:
            await self.reminders.run()
        except Exception:
            log.exception("Gift expiry reminders failed; will retry next hour")

    @check_gifts.before_loop
    async def _wait_until_ready(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GiftReminderTask(bot))
