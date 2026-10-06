"""Posting to a server's report channel (set with /setup report_channel).

The report channel is where the bot speaks to a server on its own, rather
than in reply to a command: gift thank-you notices (/ops grant notify:true)
and, later, gift expiry heads-ups and scheduled reports.

Use ``send_to_report_channel``. It raises ``ReportChannelUnavailable`` with a
sentence an operator can read when nothing can be posted (no channel set,
the bot isn't in the server, the channel is gone, or the bot lost its
permissions), so callers decide whether to tell someone or just log it.
"""

from __future__ import annotations

import discord

# What the bot needs in a report channel. /setup checks these when it's set.
REQUIRED_PERMISSIONS = ("view_channel", "send_messages", "embed_links")


class ReportChannelUnavailable(Exception):
    """The server's report channel can't be posted in; str() says why."""


def missing_permissions(channel, me) -> list[str]:
    """Names of REQUIRED_PERMISSIONS the bot (``me``) lacks in ``channel``."""
    perms = channel.permissions_for(me)
    return [name for name in REQUIRED_PERMISSIONS if not getattr(perms, name)]


def permission_names(names: list[str]) -> str:
    """["view_channel", "send_messages"] -> "View Channel and Send Messages"."""
    titles = [n.replace("_", " ").title() for n in names]
    return titles[0] if len(titles) == 1 else ", ".join(titles[:-1]) + " and " + titles[-1]


async def send_to_report_channel(bot, guild_id: str, **send_kwargs) -> discord.Message:
    """Post ``send_kwargs`` (content=, embed=, ...) in ``guild_id``'s report channel.

    Raises ReportChannelUnavailable if there's no usable report channel.
    """
    channel_id = await bot.db.report_channel_id(guild_id)
    if channel_id is None:
        raise ReportChannelUnavailable("the server has no report channel set (`/setup report_channel`)")
    guild = bot.get_guild(int(guild_id)) if guild_id.isdigit() else None
    if guild is None:
        raise ReportChannelUnavailable("the bot isn't in that server")
    channel = guild.get_channel(int(channel_id))
    if channel is None:
        raise ReportChannelUnavailable(f"the report channel ({channel_id}) no longer exists")
    missing = missing_permissions(channel, guild.me)
    if missing:
        raise ReportChannelUnavailable(
            f"the bot lacks {permission_names(missing)} in the report channel <#{channel_id}>"
        )
    try:
        return await channel.send(**send_kwargs)
    except discord.HTTPException as exc:
        raise ReportChannelUnavailable(f"Discord refused the post in <#{channel_id}>: {exc}") from exc
