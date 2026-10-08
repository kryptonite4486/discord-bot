"""The bot's custom emojis, looked up by name.

The art lives in assets/emojis/ and is uploaded to the bot's application by
scripts/upload_emojis.py. At startup :func:`load` fetches the app's emojis,
so each bot instance (production, testing) uses its own IDs and nothing is
hard-coded. Until then, or if an emoji is missing, :func:`icon` returns the
fallback (often ""), so messages still read fine without them.

Custom emojis don't render inside code blocks; put them in plain text.
"""

from __future__ import annotations

import logging

import discord

log = logging.getLogger(__name__)

_EMOJIS: dict[str, str] = {}

METRIC_ICONS = {
    "VersusPoints": "vs_points",
    "TechContribution": "tech_contribution",
    "HQLevel": "hq_level",
    "Power": "power",
    "ArenaPower": "arena_power",
    "Kills": "kills",
}

_PLACE_FALLBACK = {1: "🥇", 2: "🥈", 3: "🥉"}


async def load(client: discord.Client) -> int:
    """Fetch the application's emojis; returns how many are available."""
    emojis = await client.fetch_application_emojis()
    _EMOJIS.clear()
    _EMOJIS.update({e.name: str(e) for e in emojis})
    return len(_EMOJIS)


def icon(name: str, fallback: str = "") -> str:
    """``<:name:id>`` for an uploaded emoji, else ``fallback``."""
    return _EMOJIS.get(name, fallback)


def metric_icon(metric: str) -> str:
    return icon(METRIC_ICONS.get(metric, ""))


def place_icon(place: int) -> str:
    """Medal for a leaderboard place (1-10), with 🥇🥈🥉 as fallbacks."""
    return icon(f"place_{place}", _PLACE_FALLBACK.get(place, f"#{place}"))


def with_icon(emoji: str, text: str) -> str:
    """``emoji text``, or just ``text`` when the emoji is unavailable."""
    return f"{emoji} {text}" if emoji else text
