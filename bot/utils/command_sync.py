"""Slash-command registration.

Commands are registered once, globally. Older builds also copied them into
each server, so Discord listed every command twice there; those per-server
copies are removed here. Two servers are exceptions:

- CONTROL_GUILD_ID holds the operator-only /ops commands, which are
  registered to that server alone and never cleared.
- DEV_GUILD_ID gets a per-server copy of everything (and no global sync) so
  command changes show up instantly while developing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable

import discord
from discord import app_commands

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    global_count: int | None = None
    dev_count: int | None = None
    control_count: int | None = None
    control_error: str | None = None
    cleared: list[discord.abc.Snowflake] = field(default_factory=list)

    def summary(self) -> str:
        lines = []
        if self.global_count is not None:
            lines.append(
                f"Synced **{self.global_count}** global commands (changes can take "
                "a few minutes to appear; restart Discord if they don't)."
            )
        if self.dev_count is not None:
            lines.append(
                f"Synced **{self.dev_count}** commands to the dev server only "
                "(`DEV_GUILD_ID`)."
            )
        if self.control_count is not None:
            lines.append(f"Synced **{self.control_count}** commands to the control server.")
        if self.control_error:
            lines.append(f"⚠️ Control server not synced: {self.control_error}")
        if self.cleared:
            names = ", ".join(f"**{getattr(g, 'name', g.id)}**" for g in self.cleared)
            lines.append(f"Removed duplicate command copies from {names}.")
        return "\n".join(lines)


async def sync_commands(
    tree: app_commands.CommandTree,
    guilds: Iterable[discord.abc.Snowflake],
    *,
    dev_guild_id: int | None = None,
    control_guild_id: int | None = None,
) -> SyncResult:
    """Register commands and clean duplicate per-server copies in ``guilds``."""
    result = SyncResult()
    if dev_guild_id:
        dev = discord.Object(id=dev_guild_id)
        tree.copy_global_to(guild=dev)
        result.dev_count = len(await tree.sync(guild=dev))
    else:
        result.global_count = len(await tree.sync())
        result.cleared = await clear_guild_copies(
            tree, guilds, skip_ids={control_guild_id}
        )
    if control_guild_id and control_guild_id != dev_guild_id:
        try:
            synced = await tree.sync(guild=discord.Object(id=control_guild_id))
            result.control_count = len(synced)
        except discord.Forbidden:
            result.control_error = "the bot is not in that server yet. Invite it."
            log.warning(
                "Cannot register /ops in control guild %s: the bot is not in it",
                control_guild_id,
            )
    return result


async def sync_control_guild(
    tree: app_commands.CommandTree, control_guild_id: int
) -> int:
    """Register the control server's own commands (/ops) there."""
    return len(await tree.sync(guild=discord.Object(id=control_guild_id)))


async def clear_guild_copies(
    tree: app_commands.CommandTree,
    guilds: Iterable[discord.abc.Snowflake],
    *,
    skip_ids: set[int | None] | None = None,
) -> list[discord.abc.Snowflake]:
    """Remove per-server command copies that duplicate the global ones.

    Only servers that still have copies are written to. Returns the servers
    that were cleaned. Servers in ``skip_ids`` are left alone.
    """
    skip_ids = skip_ids or set()
    cleared = []
    for guild in guilds:
        if guild.id in skip_ids:
            continue
        try:
            if not await tree.fetch_commands(guild=guild):
                continue
            tree.clear_commands(guild=guild)
            await tree.sync(guild=guild)
            cleared.append(guild)
        except discord.HTTPException:
            log.exception("Failed to clear guild commands for %s", guild.id)
    return cleared
