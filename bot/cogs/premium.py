"""/premium: this server's plan, its usage, and what each plan includes."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.utils.guild import guild_id_from_interaction
from bot.utils.tiers import FEATURE_NAMES, FREE, FULL, MID, TierStatus, quota_week_start

CONTACT = "lastzassistant@gmail.com"


def format_premium(status: TierStatus, used: int, *, enforced: bool) -> str:
    policy = status.policy
    limit = policy.ocr_images_per_week
    lines = [
        f"**Plan: {status.describe()}**",
        f"Screenshots this week: **{used} / {limit}** "
        f"(week started {quota_week_start().isoformat()}, resets Sunday UTC)",
        "",
        "**What each plan includes**",
        "```",
        f"{'':<34}{FREE.name:>8}{MID.name:>10}{FULL.name:>9}",
        f"{'Screenshots per week':<34}"
        f"{FREE.ocr_images_per_week:>8}{MID.ocr_images_per_week:>10}{FULL.ocr_images_per_week:>9}",
        f"{'Weeks of history in reports':<34}"
        f"{_weeks(FREE):>8}{_weeks(MID):>10}{_weeks(FULL):>9}",
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


class Premium(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="premium", description="This server's plan, usage and what each plan includes")
    async def premium(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = guild_id_from_interaction(interaction)
        tiers = self.bot.tiers
        status = await tiers.status(guild_id)
        used = await tiers.ocr_used_this_week(guild_id)
        await interaction.followup.send(
            format_premium(status, used, enforced=tiers.enforced), ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Premium(bot))
