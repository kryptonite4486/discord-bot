"""Subscription tiers: what each tier allows, and which tier a server has.

A server's tier is the highest tier among its active entitlements (paid,
gifted, trial or code; see GuildEntitlement), or Free with none. Limits are
only enforced when TIERS_ENFORCED is on; otherwise every check passes and
the bot logs what it would have blocked, so tiers can be rolled out (and
gifts granted) before anyone loses access.

Each tier also caps how many channels (datasets) hold data. A server over
its cap after a downgrade keeps all its data, and keeps adding data in its
most recently used channels; see writable_channels.

See docs/monetization-plan.md, sections 3 and 5.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from discord import app_commands

log = logging.getLogger(__name__)

# Features gated by tier. Only features that exist in the bot are listed;
# add a key here and to the tiers below when a new paid feature ships.
FEATURE_NAMES = {
    "zip_batch": "`/ingest zip` and `/ingest batch`",
    "advanced_reports": "`/report player`, `/report trend` and `/report growth`",
    "name_tools": "`/admin duplicates` and `/admin rename-player`",
    "multi_channel_reports": "reports covering more than one channel",
}


@dataclass(frozen=True)
class TierPolicy:
    key: str  # stored in GuildEntitlement.Tier ('free' is never stored)
    name: str  # shown to users
    rank: int
    ocr_images_per_week: int
    # Weeks of history reports show, counting the current week; None = all.
    # Older weeks are hidden, never deleted.
    history_weeks: int | None = None
    max_channels: int | None = None  # tracked channels (datasets); None = unlimited
    features: frozenset[str] = field(default_factory=frozenset)
    # OCR queue level: 0 Standard, 1 Priority, 2 Highest (bot/utils/fair_queue.py).
    queue_priority: int = 0

    def allows(self, feature: str) -> bool:
        return feature in self.features

    def min_week(self, today: date | None = None) -> str | None:
        """Oldest week start (Sunday, ISO) reports may show; None = no limit."""
        if self.history_weeks is None:
            return None
        start = quota_week_start_of(today or datetime.now(timezone.utc).date())
        return (start - timedelta(weeks=self.history_weeks - 1)).isoformat()


FREE = TierPolicy("free", "Free", 0, ocr_images_per_week=25, history_weeks=4, max_channels=1)
MID = TierPolicy(
    "mid",
    "Alliance",
    1,
    ocr_images_per_week=250,
    history_weeks=26,
    max_channels=3,
    features=frozenset({"zip_batch", "advanced_reports", "name_tools"}),
    queue_priority=1,
)
FULL = TierPolicy(
    "full",
    "Command",
    2,
    ocr_images_per_week=1000,
    max_channels=None,
    features=MID.features | {"multi_channel_reports"},
    queue_priority=2,
)
TIERS = {t.key: t for t in (FREE, MID, FULL)}
PAID_TIERS = (MID, FULL)

SOURCE_NAMES = {
    "discord": "subscribed",
    "stripe": "subscribed",
    "gift": "gifted",
    "trial": "trial",
    "code": "from a gift code",
}


def lowest_tier_with(feature: str) -> TierPolicy:
    return next(t for t in (FREE, MID, FULL) if t.allows(feature))


def lowest_tier_with_channels(count: int) -> TierPolicy:
    """Cheapest tier that can take new data in ``count`` channels."""
    return next(
        t for t in (FREE, MID, FULL) if t.max_channels is None or t.max_channels >= count
    )


def writable_channels(tracked: list[str], policy: TierPolicy) -> list[str]:
    """Tracked channels that may take new data under ``policy``.

    ``tracked`` is ordered most recently written first. A server over its
    limit (after a downgrade) keeps writing to its most recently used
    channels; the rest stay readable in reports but take no new data.
    """
    if policy.max_channels is None:
        return list(tracked)
    return tracked[: policy.max_channels]


def channel_allowed(channel_id: str, tracked: list[str], policy: TierPolicy) -> bool:
    """Whether a write to ``channel_id`` fits the tier's channel limit."""
    if policy.max_channels is None:
        return True
    if channel_id in tracked:
        return channel_id in writable_channels(tracked, policy)
    return len(tracked) < policy.max_channels


def quota_week_start(now: datetime | None = None) -> date:
    """Sunday (UTC) that starts the current OCR quota week."""
    return quota_week_start_of((now or datetime.now(timezone.utc)).date())


def quota_week_start_of(day: date) -> date:
    """Sunday that starts ``day``'s week (same weeks as WeeklyMetrics)."""
    return day - timedelta(days=(day.weekday() + 1) % 7)


def lowest_tier_showing(week_start: str, today: date | None = None) -> TierPolicy:
    """Cheapest tier whose history window includes ``week_start``."""
    for tier in (FREE, MID):
        if week_start >= tier.min_week(today):
            return tier
    return FULL


def hidden_weeks_note(hidden: list[str], status: TierStatus, today: date | None = None) -> str:
    """Report footer for weeks left out by the server's history window."""
    if not hidden:
        return ""
    needed = lowest_tier_showing(min(hidden), today)
    n = len(hidden)
    return (
        f"🔒 {n} older week{'s' if n != 1 else ''} hidden: the **{status.policy.name}** plan "
        f"shows the last {status.policy.history_weeks} weeks. "
        f"The **{needed.name}** plan shows {'it' if n == 1 else 'them'}; see `/premium`."
    )


@dataclass(frozen=True)
class TierStatus:
    policy: TierPolicy
    source: str | None = None  # source of the entitlement giving the tier
    ends_at: str | None = None  # None: no end date (or Free)

    def describe(self) -> str:
        """e.g. 'Command — gifted until 2027-01-01' or 'Free'."""
        if self.source is None:
            return self.policy.name
        how = SOURCE_NAMES.get(self.source, self.source)
        until = f" until {self.ends_at[:10]}" if self.ends_at else ""
        return f"{self.policy.name} — {how}{until}"


def status_from_entitlements(rows: list[dict]) -> TierStatus:
    """Highest active entitlement wins; ties go to the one lasting longest."""
    best: TierStatus = TierStatus(FREE)
    for row in rows:
        policy = TIERS.get(row["Tier"])
        if policy is None or policy is FREE:
            continue
        candidate = TierStatus(policy, row["Source"], row["EndsAt"])
        if policy.rank > best.policy.rank or (
            policy.rank == best.policy.rank and _lasts_longer(candidate, best)
        ):
            best = candidate
    return best


def _lasts_longer(a: TierStatus, b: TierStatus) -> bool:
    if a.ends_at is None:
        return b.ends_at is not None
    return b.ends_at is not None and a.ends_at > b.ends_at


class Tiers:
    """Tier lookups with a short cache, feature checks and the OCR quota."""

    CACHE_SECONDS = 60.0

    def __init__(self, db, *, enforced: bool) -> None:
        self.db = db
        self.enforced = enforced
        self._cache: dict[str, tuple[float, TierStatus]] = {}
        self._ocr_in_flight: dict[str, int] = {}

    def invalidate(self, guild_id: str | None = None) -> None:
        if guild_id is None:
            self._cache.clear()
        else:
            self._cache.pop(guild_id, None)

    async def status(self, guild_id: str) -> TierStatus:
        hit = self._cache.get(guild_id)
        if hit and time.monotonic() - hit[0] < self.CACHE_SECONDS:
            return hit[1]
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        status = status_from_entitlements(await self.db.active_entitlements(guild_id, now))
        self._cache[guild_id] = (time.monotonic(), status)
        return status

    async def check_feature(self, guild_id: str, feature: str) -> tuple[bool, TierStatus]:
        """(allowed, status). Not enforced: always allowed, but logged."""
        status = await self.status(guild_id)
        if status.policy.allows(feature):
            return True, status
        if not self.enforced:
            log.info(
                "Tier check (not enforced): guild %s on %s would need %s for %s",
                guild_id,
                status.policy.name,
                lowest_tier_with(feature).name,
                feature,
            )
            return True, status
        return False, status

    async def history_window(self, guild_id: str) -> tuple[str | None, TierStatus]:
        """(min_week, status) for reports. min_week None: show every week.

        Not enforced: always None, so reports are unchanged.
        """
        status = await self.status(guild_id)
        if not self.enforced:
            return None, status
        return status.policy.min_week(), status

    async def check_channel(
        self, guild_id: str, channel_id: str
    ) -> tuple[bool, TierStatus, list[str]]:
        """(allowed, status, tracked) for a write that adds data in ``channel_id``.

        ``tracked`` lists the server's channels with data, most recently
        written first. Not enforced: always allowed, but logged.
        """
        status = await self.status(guild_id)
        if status.policy.max_channels is None:
            return True, status, []
        tracked = await self.db.tracked_channels(guild_id)
        if channel_allowed(channel_id, tracked, status.policy):
            return True, status, tracked
        if not self.enforced:
            log.info(
                "Channel limit (not enforced): guild %s on %s would be refused data in "
                "channel %s (%d channel(s) tracked, limit %d)",
                guild_id,
                status.policy.name,
                channel_id,
                len(tracked),
                status.policy.max_channels,
            )
            return True, status, tracked
        return False, status, tracked

    async def queue_priority(self, guild_id: str) -> int:
        """OCR queue level for a new request. Not enforced: everyone is Standard."""
        if not self.enforced:
            return 0
        return (await self.status(guild_id)).policy.queue_priority

    async def ocr_used_this_week(self, guild_id: str) -> int:
        since = quota_week_start().isoformat()
        return int(await self.db.usage_total(guild_id, since, "ocr_images"))

    async def reserve_ocr(self, guild_id: str, wanted: int) -> tuple[int, int, int]:
        """Reserve up to ``wanted`` OCR images; return (allowed, used, limit).

        ``used`` counts this week's finished images plus ones still being
        processed. Call release_ocr(guild_id, allowed) once the batch is done.
        """
        status = await self.status(guild_id)
        limit = status.policy.ocr_images_per_week
        used = await self.ocr_used_this_week(guild_id) + self._ocr_in_flight.get(guild_id, 0)
        allowed = max(0, min(wanted, limit - used))
        if allowed < wanted and not self.enforced:
            log.info(
                "OCR quota (not enforced): guild %s on %s would get %d of %d image(s) "
                "(%d/%d used this week)",
                guild_id,
                status.policy.name,
                allowed,
                wanted,
                used,
                limit,
            )
            allowed = wanted
        self._ocr_in_flight[guild_id] = self._ocr_in_flight.get(guild_id, 0) + allowed
        return allowed, used, limit

    def release_ocr(self, guild_id: str, reserved: int) -> None:
        left = self._ocr_in_flight.get(guild_id, 0) - reserved
        if left > 0:
            self._ocr_in_flight[guild_id] = left
        else:
            self._ocr_in_flight.pop(guild_id, None)


def upgrade_message(feature: str, status: TierStatus) -> str:
    needed = lowest_tier_with(feature)
    return (
        f"🔒 {FEATURE_NAMES.get(feature, feature)} need the **{needed.name}** plan "
        f"or higher. This server is on **{status.describe()}**. "
        f"Run `/premium` to see what each plan includes."
    )


def _channel_list(channel_ids: list[str]) -> str:
    return ", ".join(f"<#{c}>" for c in channel_ids)


def channel_limit_message(channel_id: str, tracked: list[str], status: TierStatus) -> str:
    """Why new data can't go in ``channel_id``, naming the channels that can take it."""
    policy = status.policy
    limit = policy.max_channels or 0
    writable = writable_channels(tracked, policy)
    needed = lowest_tier_with_channels(len(tracked) + (channel_id not in tracked))
    plural = "channel" if limit == 1 else "channels"
    lines = [
        f"🔒 The **{policy.name}** plan adds new data in up to **{limit}** {plural}. "
        f"This server is on **{status.describe()}**."
    ]
    if channel_id in tracked:
        lines.append(
            "This channel's data is kept and still shows in reports, but new data "
            f"can only be added in the {limit} most recently used {plural}: "
            f"{_channel_list(writable)}."
        )
    else:
        lines.append(f"Add data in {_channel_list(writable)} instead.")
    lines.append(
        f"The **{needed.name}** plan takes data here too. "
        "Run `/premium` to see what each plan includes."
    )
    return "\n".join(lines)


async def ensure_channel(interaction) -> bool:
    """Reply and return False if new data can't go in this channel on this plan.

    Same reply rules as ensure_feature. Call before writing data.
    """
    tiers = getattr(getattr(interaction, "client", None), "tiers", None)
    if tiers is None or interaction.guild_id is None or interaction.channel_id is None:
        return True
    channel_id = str(interaction.channel_id)
    allowed, status, tracked = await tiers.check_channel(str(interaction.guild_id), channel_id)
    if allowed:
        return True
    text = channel_limit_message(channel_id, tracked, status)
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)
    return False


async def ensure_feature(interaction, feature: str) -> bool:
    """Reply with an upgrade message and return False if the server lacks ``feature``.

    Works whether or not the interaction was already deferred. Without a
    tier service (tests, or tiers not set up) everything is allowed.
    """
    tiers = getattr(interaction.client, "tiers", None)
    if tiers is None or interaction.guild_id is None:
        return True
    allowed, status = await tiers.check_feature(str(interaction.guild_id), feature)
    if allowed:
        return True
    text = upgrade_message(feature, status)
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)
    return False


def requires_feature(feature: str):
    """Slash-command check: run the command only if the server has ``feature``.

    The upgrade message is sent by the check, so a refusal raises
    FeatureLocked, which error handlers should ignore.
    """

    async def predicate(interaction) -> bool:
        if await ensure_feature(interaction, feature):
            return True
        raise FeatureLocked(feature)

    return app_commands.check(predicate)


class FeatureLocked(app_commands.CheckFailure):
    """Raised after the upgrade message has been sent."""

    def __init__(self, feature: str) -> None:
        super().__init__(f"feature {feature} needs a higher plan")
        self.feature = feature
