"""Catch screenshots ingested under the wrong dataset, and impossible values.

Some screens look identical in game (General vs Arena member cards, Power vs
Kills leaderboards), so the easiest mistake is picking the wrong dataset. These
checks compare a fresh batch of values with what is already stored. They only
warn; the caller still saves the data.

Two kinds of check:

* Over time (previous weeks): total Power rarely collapses or multiplies
  (a 5x+ jump is a dropped decimal), and Kills totals never fall.
* Within the same week: Arena Power is part of total Power, so it can never
  exceed it. A player breaking that is a misread value or name; many players
  at (or above) their total Power means a General/Arena mix-up. Comparing only
  within one week avoids false alarms from growth, and the check runs on
  whichever of the two uploads arrives second, so upload order doesn't matter.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bot.utils.parsing import format_value

if TYPE_CHECKING:
    from bot.db import Database

# Total Power rarely falls by 75%+ week over week; that suggests Arena Power
# (member cards) or Kills (leaderboard).
POWER_DROP_MIN = 0.25
# Weekly Power growth has peaked around +75%; 5x+ is a misread, typically a
# dropped decimal ("38.5M" read as 385M).
POWER_JUMP_MAX = 5.0
# Kills totals never fall, and a 5x jump suggests a Power leaderboard.
KILLS_JUMP_MAX = 5.0
# Same week: real Arena Power has been 1-80% of total Power. Arena within 5%
# of total Power means the same number landed under both metrics (a mix-up);
# more than 5% above it is impossible (a misread). The 5% allows for growth
# between screenshots taken on different days of the week.
SAME_VALUE_TOLERANCE = 0.05
# A screenshot is flagged when this many rows (or this share) look wrong.
MIN_FLAGGED_ROWS = 3
MIN_FLAGGED_SHARE = 0.3
# Players named in an "Arena above total Power" warning.
MAX_LISTED_PLAYERS = 5


@dataclass(frozen=True)
class _Rule:
    """Compare new values with the same metric from earlier weeks."""

    reference_metric: str
    flagged: Callable[[float, float], bool]  # (new value, reference) -> wrong?
    message: str


_POWER_DROPPED = _Rule(
    reference_metric="Power",
    flagged=lambda value, ref: ref > 0 and value <= ref * POWER_DROP_MIN,
    message=(
        "{n} of {total} Power value(s) dropped 75%+ from the previous week — "
        "is this an **Arena Power** (`dataset:arena`) or **Kills** "
        "(`dataset:kills`) screenshot?"
    ),
)
_POWER_JUMPED = _Rule(
    reference_metric="Power",
    flagged=lambda value, ref: ref > 0 and value >= ref * POWER_JUMP_MAX,
    message=(
        "{n} of {total} Power value(s) jumped 5x+ over the previous week — "
        "likely misread (e.g. a dropped decimal: 38.5M read as 385M). Check "
        "these values against the screenshot."
    ),
)
_KILLS_WRONG = _Rule(
    reference_metric="Kills",
    flagged=lambda value, ref: value < ref or (ref > 0 and value >= ref * KILLS_JUMP_MAX),
    message=(
        "{n} of {total} Kills total(s) went down or jumped 5x+ from before — "
        "is this a **Power** screenshot? (`dataset:power`) Otherwise check "
        "player names."
    ),
)

RULES: dict[str, tuple[_Rule, ...]] = {
    "Power": (_POWER_DROPPED, _POWER_JUMPED),
    "Kills": (_KILLS_WRONG,),
}

# Same-week pair: uploading either metric checks it against the other.
_ARENA_PAIR = {"ArenaPower": "Power", "Power": "ArenaPower"}


def _enough(flagged: int, compared: int) -> bool:
    return flagged >= MIN_FLAGGED_ROWS or flagged / compared >= MIN_FLAGGED_SHARE


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
    if flagged == 0 or not _enough(flagged, compared):
        return None
    return rule.message.format(n=flagged, total=compared)


def evaluate_arena_vs_power(pairs: Mapping[str, tuple[float, float]]) -> str | None:
    """
    Same-week check over ``pairs`` (player -> (arena power, total power)).

    Enough players with Arena Power equal to total Power (within 5%): a
    dataset mix-up. Otherwise, any player with Arena Power clearly above total
    Power is impossible and listed individually (a misread value or name).
    """
    compared = [(p, a, t) for p, (a, t) in pairs.items() if t > 0]
    if not compared:
        return None
    low, high = 1 - SAME_VALUE_TOLERANCE, 1 + SAME_VALUE_TOLERANCE
    matching = [row for row in compared if low <= row[1] / row[2] <= high]
    if matching and _enough(len(matching), len(compared)):
        return (
            f"{len(matching)} of {len(compared)} player(s) have the same Arena Power "
            "and total Power this week (within 5%). Arena Power is always lower, so "
            "one of this week's General/Power and Arena uploads was probably filed "
            "under the wrong dataset."
        )
    above = sorted(
        (row for row in compared if row[1] / row[2] > high),
        key=lambda r: r[1] / r[2],
        reverse=True,
    )
    if not above:
        return None
    listed = ", ".join(
        f"`{player}` (Arena {format_value('ArenaPower', arena)} vs Power "
        f"{format_value('Power', total)})"
        for player, arena, total in above[:MAX_LISTED_PLAYERS]
    )
    more = f" and {len(above) - MAX_LISTED_PLAYERS} more" if len(above) > MAX_LISTED_PLAYERS else ""
    return (
        f"Arena Power is higher than total Power for {listed}{more}. That's "
        "impossible, so a value or name was misread; check these players."
    )


async def check_batch(
    db: Database,
    guild_id: str,
    channel_id: str,
    week_start: str,
    metric_type: str,
    values: Mapping[str, float],
) -> list[str]:
    """Run every check for ``metric_type`` against stored values; return warnings."""
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
            include_week=False,
        )
        if warning := evaluate(rule, values, references):
            warnings.append(warning)

    other = _ARENA_PAIR.get(metric_type)
    if other is not None:
        stored = {
            row["PlayerName"].strip().lower(): float(row["Value"])
            for row in await db.get_week_metrics(
                guild_id, week_start, other, channel_id=channel_id
            )
        }
        pairs: dict[str, tuple[float, float]] = {}
        for player, value in values.items():
            reference = stored.get(player.strip().lower())
            if reference is None:
                continue
            pairs[player] = (
                (value, reference) if metric_type == "ArenaPower" else (reference, value)
            )
        if warning := evaluate_arena_vs_power(pairs):
            warnings.append(warning)
    return warnings
