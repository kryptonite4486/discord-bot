"""/premium: this server's plan, its usage, and what each plan includes.
/redeem: claim a gift code for this server.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from bot.utils.guild import guild_id_from_interaction
from bot.utils.gift_codes import AttemptLimiter, hash_code, normalize_code
from bot.utils.tiers import FEATURE_NAMES, FREE, FULL, MID, TIERS, TierStatus, quota_week_start

log = logging.getLogger(__name__)
CONTACT = "lastzassistant@gmail.com"


def _channel_cap(policy) -> str:
    return "any" if policy.max_channels is None else str(policy.max_channels)


def format_premium(
    status: TierStatus, used: int, *, enforced: bool, channels: int | None = None
) -> str:
    policy = status.policy
    limit = policy.ocr_images_per_week
    lines = [
        f"**Plan: {status.describe()}**",
        f"Screenshots this week: **{used} / {limit}** "
        f"(week started {quota_week_start().isoformat()}, resets Sunday UTC)",
    ]
    if channels is not None:
        lines.append(f"Channels with data: **{channels} / {_channel_cap(policy)}**")
    lines += [
        "",
        "**What each plan includes**",
        "```",
        f"{'':<34}{FREE.name:>8}{MID.name:>10}{FULL.name:>9}",
        f"{'Screenshots per week':<34}"
        f"{FREE.ocr_images_per_week:>8}{MID.ocr_images_per_week:>10}{FULL.ocr_images_per_week:>9}",
        f"{'Weeks of history in reports':<34}"
        f"{_weeks(FREE):>8}{_weeks(MID):>10}{_weeks(FULL):>9}",
        f"{'Channels with data':<34}"
        f"{_channel_cap(FREE):>8}{_channel_cap(MID):>10}{_channel_cap(FULL):>9}",
    ]
    for key, label in FEATURE_LABELS.items():
        marks = ["✓" if t.allows(key) else "–" for t in (FREE, MID, FULL)]
        lines.append(f"{label:<34}{marks[0]:>8}{marks[1]:>10}{marks[2]:>9}")
    lines.append("```")
    lines.append(
        "Manual entry, `/ingest image`, `/ingest text`, the weekly, versus, tech "
        "and leaderboard reports, and the mix-up flags are free on every plan."
    )
    if not enforced:
        lines.append(
            "\n_Plan limits aren't switched on yet, so everything currently works "
            "on every server._"
        )
    lines.append(f"Paid plans aren't on sale yet. Questions: {CONTACT}")
    return "\n".join(lines)


def _weeks(policy) -> str:
    return "All" if policy.history_weeks is None else str(policy.history_weeks)


# Short labels for the comparison table, in display order.
FEATURE_LABELS = {
    "zip_batch": "/ingest zip and /ingest batch",
    "advanced_reports": "Player, trend and growth reports",
    "name_tools": "Duplicate-name tools",
    "multi_channel_reports": "Reports across several channels",
}
assert set(FEATURE_LABELS) == set(FEATURE_NAMES), "label every gated feature"


REDEEM_FAILURES = {
    "format": "That doesn't look like a gift code. Codes look like `ABCD-EFGH-JKMN-PQRS`.",
    "invalid": "That code isn't valid. Check it and try again.",
    "revoked": "That code has been withdrawn and can't be redeemed.",
    "expired": "That code has expired.",
    "used_up": "That code has already been redeemed the maximum number of times.",
    "already": "This server has already redeemed that code.",
}


def redeem_success_message(tier: str, ends_at: str | None, status: TierStatus) -> str:
    name = TIERS[tier].name if tier in TIERS else tier
    lasts = f"until {ends_at[:10]}" if ends_at else "with no end date"
    text = f"🎉 Code redeemed: this server has the **{name}** plan {lasts}."
    if status.policy.key != tier or status.ends_at != ends_at:
        text += f" Its plan is now **{status.describe()}**."
    return text + " Run `/premium` to see what it includes."


class Premium(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Failed /redeem attempts per user, to stop codes being guessed.
        self.redeem_limiter = AttemptLimiter(max_failures=5, window=15 * 60)

    @app_commands.command(name="premium", description="This server's plan, usage and what each plan includes")
    async def premium(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = guild_id_from_interaction(interaction)
        tiers = self.bot.tiers
        status = await tiers.status(guild_id)
        used = await tiers.ocr_used_this_week(guild_id)
        channels = len(await self.bot.db.tracked_channels(guild_id))
        await interaction.followup.send(
            format_premium(status, used, enforced=tiers.enforced, channels=channels),
            ephemeral=True,
        )


    @app_commands.command(name="redeem", description="Redeem a gift code for this server (Manage Server)")
    @app_commands.describe(code="The gift code, e.g. ABCD-EFGH-JKMN-PQRS")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def redeem(self, interaction: discord.Interaction, code: str) -> None:
        guild_id = guild_id_from_interaction(interaction)
        await interaction.response.send_message(
            await self.redeem_code(guild_id, interaction.user.id, code), ephemeral=True
        )

    async def redeem_code(self, guild_id: str, user_id: int, code: str) -> str:
        """Try to redeem ``code`` for a server; return the reply text."""
        wait = self.redeem_limiter.retry_after(user_id)
        if wait > 0:
            log.warning("/redeem rate-limited: user %s in guild %s", user_id, guild_id)
            return (
                "Too many attempts with codes that didn't work. "
                f"Try again in {max(1, round(wait / 60))} minute(s)."
            )
        normalized = normalize_code(code)
        if normalized is None:
            outcome, details = "format", {}
        else:
            outcome, details = await self.bot.db.redeem_gift_code(
                hash_code(normalized), guild_id, str(user_id), now=datetime.now(timezone.utc)
            )
        if outcome != "ok":
            self.redeem_limiter.record_failure(user_id)
            log.info("/redeem failed (%s) for user %s in guild %s", outcome, user_id, guild_id)
            return REDEEM_FAILURES[outcome]
        self.bot.tiers.invalidate(guild_id)
        status = await self.bot.tiers.status(guild_id)
        log.warning("Guild %s redeemed code #%d (entitlement #%d) by user %s",
                    guild_id, details["code_id"], details["entitlement_id"], user_id)
        return redeem_success_message(details["tier"], details["ends_at"], status)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            text = "You need the Manage Server permission to redeem a code."
        else:
            log.exception("%s error: %s", interaction.command, error)
            text = f"Error: `{error}`"
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Premium(bot))
