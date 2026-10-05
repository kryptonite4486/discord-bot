"""Slash-command registration.

Commands are registered once, globally. Older builds also copied them into
each server, so Discord listed every command twice there; those per-server
copies are removed here. The one exception is DEV_GUILD_ID, which keeps a
per-server copy so command changes show up instantly while developing.
"""

from __future__ import annotations

import logging
from typing import Iterable

import discord
from discord import app_commands

log = logging.getLogger(__name__)


async def sync_global(tree: app_commands.CommandTree) -> int:
    """Register every command globally; return how many were synced."""
    synced = await tree.sync()
    return len(synced)


async def sync_dev_guild(tree: app_commands.CommandTree, guild_id: int) -> int:
    """Copy commands into the dev server only, for instant updates."""
    guild = discord.Object(id=guild_id)
    tree.copy_global_to(guild=guild)
    synced = await tree.sync(guild=guild)
    return len(synced)


async def clear_guild_copies(
    tree: app_commands.CommandTree,
    guilds: Iterable[discord.abc.Snowflake],
) -> list[discord.abc.Snowflake]:
    """Remove per-server command copies that duplicate the global ones.

    Only servers that still have copies are written to. Returns the servers
    that were cleaned.
    """
    cleared = []
    for guild in guilds:
        try:
            if not await tree.fetch_commands(guild=guild):
                continue
            tree.clear_commands(guild=guild)
            await tree.sync(guild=guild)
            cleared.append(guild)
        except discord.HTTPException:
            log.exception("Failed to clear guild commands for %s", guild.id)
    return cleared
