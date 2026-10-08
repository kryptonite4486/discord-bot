"""Discord subscriptions: entitlement events and the reconcile on connect
(see bot/utils/billing.py)."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from bot.utils.billing import Billing, DiscordEntitlement
from bot.utils.report_channel import ReportChannelUnavailable, send_to_report_channel
from bot.utils.skus import tier_for_sku
from bot.utils.tiers import TIERS

log = logging.getLogger(__name__)


def thanks_message(tier: str) -> str:
    name = TIERS[tier].name if tier in TIERS else tier
    return (
        f"🎉 Thanks for subscribing! This server now has the **{name}** plan. "
        "Run `/premium` to see what it includes."
    )


class BillingEvents(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.billing = Billing(bot.db, bot.tiers)

    async def _event(self, entitlement: discord.Entitlement) -> str:
        try:
            return await self.billing.apply(DiscordEntitlement.from_discord(entitlement))
        except Exception:
            log.exception("Couldn't record Discord entitlement %s; the next reconcile "
                          "will retry", entitlement.id)
            return "failed"

    @commands.Cog.listener()
    async def on_entitlement_create(self, entitlement: discord.Entitlement) -> None:
        if await self._event(entitlement) != "added" or entitlement.guild_id is None:
            return
        try:
            await send_to_report_channel(
                self.bot, str(entitlement.guild_id),
                content=thanks_message(tier_for_sku(entitlement.sku_id)),
            )
        except ReportChannelUnavailable as exc:
            # /premium still shows the plan.
            log.info("No thank-you posted for guild %s: %s", entitlement.guild_id, exc)

    @commands.Cog.listener()
    async def on_entitlement_update(self, entitlement: discord.Entitlement) -> None:
        await self._event(entitlement)

    @commands.Cog.listener()
    async def on_entitlement_delete(self, entitlement: discord.Entitlement) -> None:
        try:
            await self.billing.remove(entitlement.id)
        except Exception:
            log.exception("Couldn't revoke Discord entitlement %s; the next reconcile "
                          "will retry", entitlement.id)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Catch up on events missed while offline. on_ready fires for every
        new gateway session (not a resume, which replays missed events)."""
        await self.reconcile()

    async def reconcile(self):
        """Fetch every entitlement and apply the list; None if the fetch failed."""
        try:
            # The whole list first: a partial one would revoke what's missing.
            entitlements = [
                DiscordEntitlement.from_discord(e)
                async for e in self.bot.entitlements(limit=None, exclude_ended=False)
            ]
        except discord.HTTPException:
            log.exception("Couldn't fetch Discord entitlements; plans stay as they were")
            return None
        counts = await self.billing.reconcile(entitlements)
        log.info("Discord entitlements reconciled: %d listed, %s", len(entitlements),
                 ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "nothing to do")
        return counts


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BillingEvents(bot))
