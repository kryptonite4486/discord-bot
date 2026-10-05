"""Catch screenshots ingested under the wrong dataset.

Some screens look identical in game (General vs Arena member cards, Power vs
Kills leaderboards), so the easiest mistake is picking the wrong dataset. These checks compare a fresh
batch of values with what is already stored and describe anything that looks
like a mix-up. They only warn; the caller still saves the data.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.db import Database

# Arena Power is a share of total Power that varies a lot by account (10-80%
# seen in practice), so share alone can't spot a mix-up. A General/Power
# screenshot filed as Arena instead gives values equal to total Power.
ARENA_TO_POWER_MAX = 0.95
# Mirror check: an Arena screenshot filed as General gives "Power" equal to the
# stored Arena Power. Real total Power exceeds Arena Power by well over 5%.
POWER_TO_ARENA_MIN = 1.05
# Total Power rarely falls by 75%+ week over week; that suggests Arena Power
# (member cards) or Kills (leaderboard).
POWER_DROP_MIN = 0.25
# Kills totals never fall, and a 5x jump suggests a Power leaderboard.
KILLS_JUMP_MAX = 5.0
# A screenshot is flagged when this many rows (or this share) look wrong.
MIN_FLAGGED_ROWS = 3
MIN_FLAGGED_SHARE = 0.3


@dataclass(frozen=True)
class _Rule:
    reference_metric: str
    include_week: bool  # compare with the same week too, not only earlier ones
    flagged: Callable[[float, float], bool]  # (new value, reference) -> wrong?
    message: str


_ARENA_MATCHES_POWER = _Rule(
    reference_metric="Power",
    include_week=True,
    flagged=lambda value, ref: ref > 0 and value >= ref * ARENA_TO_POWER_MAX,
    message=(
        "{n} of {total} Arena Power value(s) match the player's total Power "
        "(95%+) — is this a **General** or **Power** screenshot? "
        "(`dataset:general` / `dataset:power`)"
    ),
)
_POWER_DROPPED = _Rule(
    reference_metric="Power",
    include_week=False,
    flagged=lambda value, ref: ref > 0 and value <= ref * POWER_DROP_MIN,
    message=(
        "{n} of {total} Power value(s) dropped 75%+ from the previous week — "
        "is this an **Arena Power** (`dataset:arena`) or **Kills** "
        "(`dataset:kills`) screenshot?"
    ),
)
_POWER_MATCHES_ARENA = _Rule(
    reference_metric="ArenaPower",
    include_week=True,
    flagged=lambda value, ref: ref > 0 and value <= ref * POWER_TO_ARENA_MIN,
    message=(
        "{n} of {total} Power value(s) match the player's Arena Power (within "
        "5%) — is this an **Arena** screenshot? (`dataset:arena`)"
    ),
)
_KILLS_WRONG = _Rule(
    reference_metric="Kills",
    include_week=False,
    flagged=lambda value, ref: value < ref or (ref > 0 and value >= ref * KILLS_JUMP_MAX),
    message=(
        "{n} of {total} Kills total(s) went down or jumped 5x+ from before — "
        "is this a **Power** screenshot? (`dataset:power`) Otherwise check "
        "player names."
    ),
)

RULES: dict[str, tuple[_Rule, ...]] = {
    "ArenaPower": (_ARENA_MATCHES_POWER,),
    "Power": (_POWER_DROPPED, _POWER_MATCHES_ARENA),
    "Kills": (_KILLS_WRONG,),
}


def evaluate(
    rule: _Rule,
    values: Mapping[str, float],
    references: Mapping[str, float],
) -> str | None:
    """
    Return ``rule``'s warning when enough of ``values`` (player -> new value)
    look wrong against ``references`` (lowercased player -> stored value).
    """
    compared = 0
    flagged = 0
    for player, value in values.items():
        reference = references.get(player.strip().lower())
        if reference is None:
            continue
        compared += 1
        if rule.flagged(value, reference):
            flagged += 1
    if flagged == 0:
        return None
    if flagged < MIN_FLAGGED_ROWS and flagged / compared < MIN_FLAGGED_SHARE:
        return None
    return rule.message.format(n=flagged, total=compared)


async def check_batch(
    db: Database,
    guild_id: str,
    channel_id: str,
    week_start: str,
    metric_type: str,
    values: Mapping[str, float],
) -> list[str]:
    """Look up stored reference values and run every rule for ``metric_type``."""
    if not values:
        return []
    warnings: list[str] = []
    for rule in RULES.get(metric_type, ()):
        references = await db.get_latest_values(
            guild_id,
            rule.reference_metric,
            values.keys(),
            channel_id=channel_id,
            week_start=week_start,
            include_week=rule.include_week,
        )
        if warning := evaluate(rule, values, references):
            warnings.append(warning)
    return warnings
