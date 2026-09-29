"""Shared Discord helpers."""

from __future__ import annotations

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
