"""Shared Discord helpers."""

from __future__ import annotations

from typing import Iterable

import discord
from discord.ext import commands

GUILD_ONLY_MESSAGE = (
    "This bot only works in a Discord server — DMs are not supported. "
    "Use commands in a server where the bot is installed."
)


async def reject_dm_interaction(interaction: discord.Interaction) -> bool:
    """Return True if the interaction is in a guild; otherwise reply and return False."""
    if interaction.guild is not None:
        return True
    if interaction.response.is_done():
        await interaction.followup.send(GUILD_ONLY_MESSAGE, ephemeral=True)
    else:
        await interaction.response.send_message(GUILD_ONLY_MESSAGE, ephemeral=True)
    return False


async def reject_dm_context(ctx: commands.Context) -> bool:
    """Return True if the context is in a guild; otherwise reply and return False."""
    if ctx.guild is not None:
        return True
    await ctx.reply(GUILD_ONLY_MESSAGE)
    return False


def guild_id_from_interaction(interaction: discord.Interaction) -> str:
    if interaction.guild_id is None:
        raise RuntimeError("guild_id required; interaction is not in a guild")
    return str(interaction.guild_id)


def guild_id_from_context(ctx: commands.Context) -> str:
    if ctx.guild is None:
        raise RuntimeError("guild_id required; context is not in a guild")
    return str(ctx.guild.id)


def channel_id_from_interaction(interaction: discord.Interaction) -> str:
    """Discord channel snowflake for the interaction (TEXT storage)."""
    if interaction.channel_id is None:
        raise RuntimeError("channel_id required; interaction has no channel")
    return str(interaction.channel_id)


def channel_id_from_context(ctx: commands.Context) -> str:
    if ctx.channel is None:
        raise RuntimeError("channel_id required; context has no channel")
    return str(ctx.channel.id)


def channel_id_from_message(message: discord.Message) -> str:
    return str(message.channel.id)


def channel_display_name(
    guild: discord.Guild | None,
    channel_id: str | None,
    *,
    names: dict[str, str] | None = None,
) -> str:
    """Human-readable channel label; prefer #name over snowflake."""
    if not channel_id:
        return "(unassigned)"
    cid = str(channel_id)
    if names and cid in names:
        return names[cid]
    if guild is not None:
        try:
            ch = guild.get_channel(int(cid))
        except (TypeError, ValueError):
            ch = None
        if ch is not None:
            return f"#{ch.name}"
    return f"#unknown-{cid[-4:]}"


def channel_name_map(
    guild: discord.Guild | None,
    channel_ids: Iterable[str],
) -> dict[str, str]:
    """Resolve many channel ids to #name labels for report tables."""
    return {
        cid: channel_display_name(guild, cid)
        for cid in channel_ids
        if cid is not None
    }
