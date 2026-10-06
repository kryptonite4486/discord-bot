"""Hosting monitor: OCR probe, heartbeat ping, Docker health file and reconnect
alerts. The logic lives in bot/utils/health.py; this cog runs it every minute
and DMs alerts to BOT_OWNER_IDS."""

from __future__ import annotations

import asyncio
import logging
import math

import discord
import httpx
from discord.ext import commands, tasks

from bot.utils.health import HTTP_TIMEOUT, probe_ocr, set_ocr_tracker

log = logging.getLogger(__name__)

DB_CHECK_TIMEOUT = 5.0


class HealthMonitor(commands.Cog):
    """Watches the bot's own health and alerts the operator."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.state = bot.health
        self.client: httpx.AsyncClient | None = None

    async def cog_load(self) -> None:
        self.client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)
        self.state.alerts.send = self.dm_operators
        set_ocr_tracker(self.state.ocr)
        self.monitor.start()

    async def cog_unload(self) -> None:
        self.monitor.cancel()
        if self.client is not None:
            await self.client.aclose()

    async def dm_operators(self, text: str) -> None:
        delivered = 0
        for user_id in sorted(self.bot.settings.bot_owner_ids):
            try:
                user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                await user.send(text)
                delivered += 1
            except discord.HTTPException:
                log.warning("Couldn't DM alert to operator %s", user_id, exc_info=True)
        if not delivered:
            log.warning("Alert not delivered to any operator: %s", text)

    async def bot_problem(self) -> str | None:
        """None while connected to Discord with a working database."""
        if self.bot.is_closed() or not self.bot.is_ready() or not self.state.connection.connected:
            return "not connected to Discord"
        if not math.isfinite(self.bot.latency):
            return "no Discord heartbeat"

        async def select_one() -> None:
            async with self.bot.db.conn.execute("SELECT 1") as cursor:
                await cursor.fetchone()

        try:
            await asyncio.wait_for(select_one(), DB_CHECK_TIMEOUT)
        except Exception as exc:
            return f"database check failed ({type(exc).__name__})"
        return None

    async def _probe(self) -> str | None:
        s = self.bot.settings
        assert self.client is not None
        return await probe_ocr(
            self.client, s.ocr_vision_base_url, s.ocr_vision_model, s.ocr_vision_api_key
        )

    @tasks.loop(minutes=1)
    async def monitor(self) -> None:
        assert self.client is not None
        try:
            await self.state.tick(self.client, problem=await self.bot_problem(), probe=self._probe)
        except Exception:
            log.exception("Health monitor pass failed")

    @monitor.before_loop
    async def _wait_until_ready(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_disconnect(self) -> None:
        self.state.connection.disconnected()

    async def _connected(self) -> None:
        gap = self.state.connection.reconnected()
        if gap is not None:
            await self.state.report_back_online(gap)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self._connected()

    @commands.Cog.listener()
    async def on_resumed(self) -> None:
        await self._connected()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HealthMonitor(bot))
