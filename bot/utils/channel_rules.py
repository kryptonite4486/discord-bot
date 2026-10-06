"""Which commands may run in which channel, from the server's /setup choices.

Once a server sets a trivia channel:

- trivia matches (``/trivia start`` and ``/trivia join``) run only there, or
  in a thread inside it;
- data entry (``/add`` and ``/ingest``) is refused there, so screenshots and
  stats never land in the trivia channel. Reports still work, since they only
  read data.

With no trivia channel set, nothing is restricted. Discord itself can't be
told by the bot to hide commands per channel (only an admin can, under
Server Settings → Integrations), so the bot refuses with a pointer instead.
"""

from __future__ import annotations

import discord

TRIVIA_CHANNEL_ONLY = frozenset({"trivia start", "trivia join"})
NOT_IN_TRIVIA_CHANNEL = frozenset({"add", "ingest"})


def channel_rule(command: str, channel_ids: set[str], trivia_channel_id: str | None) -> str | None:
    """Why ``command`` can't run here, or None if it can.

    ``command`` is the qualified name (e.g. ``"ingest batch"``);
    ``channel_ids`` holds the channel and, for a thread, its parent.
    """
    if not trivia_channel_id:
        return None
    here = trivia_channel_id in channel_ids
    if command in TRIVIA_CHANNEL_ONLY and not here:
        return f"Trivia is played in <#{trivia_channel_id}> on this server."
    if command.split(" ", 1)[0] in NOT_IN_TRIVIA_CHANNEL and here:
        return (
            "This is the trivia channel, so data can't be added here. "
            "Use the channel where your alliance's stats are kept."
        )
    return None


def interaction_channel_ids(interaction: discord.Interaction) -> set[str]:
    ids = {str(interaction.channel_id)}
    parent_id = getattr(interaction.channel, "parent_id", None)
    if parent_id:
        ids.add(str(parent_id))
    return ids


async def enforce_channel_rules(bot, interaction: discord.Interaction) -> bool:
    """Reply and return False if this server's channel rules forbid the command."""
    command = interaction.command
    if command is None or interaction.guild_id is None:
        return True
    settings = await bot.db.guild_settings(str(interaction.guild_id))
    reason = channel_rule(
        command.qualified_name,
        interaction_channel_ids(interaction),
        settings["trivia_channel_id"],
    )
    if reason is None:
        return True
    await interaction.response.send_message(reason, ephemeral=True)
    return False
