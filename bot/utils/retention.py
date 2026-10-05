"""Data retention rules for servers that removed the bot.

A removed server's data is deleted DATA_RETENTION_DAYS after the later of
the removal and the end of its last subscription. While any subscription
(paid, gifted or trial) is active, nothing is deleted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def fmt_time(moment: datetime) -> str:
    return moment.strftime(TIME_FORMAT)


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, TIME_FORMAT).replace(tzinfo=timezone.utc)


def deletion_date(
    removed_at: datetime,
    *,
    subscription_active: bool,
    subscription_ended: datetime | None,
    retention: timedelta,
) -> datetime | None:
    """When a removed server's data is deleted; None while a subscription is active.

    The retention period starts at the later of the removal and the end of
    the server's last subscription.
    """
    if subscription_active:
        return None
    start = max(removed_at, subscription_ended) if subscription_ended else removed_at
    return start + retention


@dataclass(frozen=True)
class RemovedServer:
    guild_id: str
    removed_at: datetime
    deletes_at: datetime | None  # None: kept while its subscription is active


async def removed_servers(db, retention: timedelta, now: datetime) -> list[RemovedServer]:
    """Every server that removed the bot, with its current deletion date."""
    out = []
    for p in await db.pending_purges():
        active, ended = await db.subscription_status(p["GuildId"], fmt_time(now))
        out.append(
            RemovedServer(
                guild_id=p["GuildId"],
                removed_at=parse_time(p["RemovedAt"]),
                deletes_at=deletion_date(
                    parse_time(p["RemovedAt"]),
                    subscription_active=active,
                    subscription_ended=parse_time(ended) if ended else None,
                    retention=retention,
                ),
            )
        )
    return out


def plan_reconcile(
    data_guilds: set[str], member_guilds: set[str], pending: set[str]
) -> tuple[set[str], set[str]]:
    """Return (servers to schedule for deletion, schedules to cancel).

    Servers with data that the bot is no longer in get scheduled (they removed
    it while the bot was offline). Scheduled servers the bot is in again get
    cancelled. Non-numeric ids (e.g. the ``legacy`` migration bucket) are never
    scheduled: they aren't real servers.
    """
    removed = {g for g in data_guilds - member_guilds if g.isdigit()}
    return removed - pending, pending & member_guilds
