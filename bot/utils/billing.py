"""Discord subscriptions: keep GuildEntitlement in step with Discord's entitlements.

Each Discord entitlement for one of the SKUs in bot/utils/skus.json becomes
one GuildEntitlement row (Source='discord', ExternalId = the entitlement's
ID), so tiers.py treats a paid plan like any gift or trial: the highest
active plan wins, and a lapsed subscription falls back to a gift.

How Discord reports a guild subscription (docs.discord.com, "Implementing
App Subscriptions"):
- Subscribing creates an entitlement with no end date. Renewals don't
  touch it, and neither does cancelling: the plan runs to the end of the
  paid month.
- When the subscription actually ends (cancelled, or payments stopped), an
  update sets ``ends_at``. We add PAID_GRACE_DAYS to it: Discord doesn't say
  whether it ended by choice or a failed payment, and a week of slack costs
  little next to a server losing its plan over a card problem.
- A refund deletes the entitlement; the row is revoked at once, no grace.
- Test entitlements (scripts/sync_skus.py entitlements) have no dates and
  last until deleted, which revokes them the same way.

Events can be missed while the bot is offline, so every new gateway session
reconciles against the full list (reconcile): rows are added or updated from
it, and an active row whose entitlement no longer comes back was deleted.

See docs/monetization-plan.md, sections 4 and 2 item 10.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from bot.utils.skus import tier_for_sku

log = logging.getLogger(__name__)

TS = "%Y-%m-%d %H:%M:%S"
PAID_GRACE_DAYS = 7


@dataclass(frozen=True)
class DiscordEntitlement:
    """The parts of a discord.Entitlement the bot uses (tests build these too)."""

    id: int
    sku_id: int
    guild_id: int | None
    starts_at: datetime | None
    ends_at: datetime | None
    deleted: bool = False
    test: bool = False

    @classmethod
    def from_discord(cls, ent: Any) -> DiscordEntitlement:
        return cls(
            id=ent.id,
            sku_id=ent.sku_id,
            guild_id=ent.guild_id,
            starts_at=ent.starts_at,
            ends_at=ent.ends_at,
            deleted=bool(ent.deleted),
            test=getattr(ent.type, "name", "") == "test_mode_purchase",
        )


def _fmt(when: datetime) -> str:
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc)
    return when.strftime(TS)


def row_values(ent: DiscordEntitlement, now: datetime, grace_days: int = PAID_GRACE_DAYS):
    """(tier, starts_at, ends_at, reason) for the row, or None to ignore it."""
    tier = tier_for_sku(ent.sku_id)
    if tier is None or ent.guild_id is None:
        return None
    starts_at = _fmt(ent.starts_at or now)
    if ent.ends_at is None:
        ends_at = None
        reason = "Discord test entitlement" if ent.test else "Discord subscription"
    else:
        ends_at = _fmt(ent.ends_at + timedelta(days=grace_days))
        reason = (
            f"Discord subscription ended {_fmt(ent.ends_at)[:10]}"
            + (f", {grace_days}-day grace" if grace_days else "")
        )
    return tier, starts_at, ends_at, reason


class Billing:
    """Applies Discord entitlements to the database and clears the tier cache."""

    def __init__(self, db, tiers, *, grace_days: int = PAID_GRACE_DAYS) -> None:
        self.db = db
        self.tiers = tiers
        self.grace_days = grace_days

    async def apply(self, ent: DiscordEntitlement, now: datetime | None = None) -> str:
        """Record one entitlement event. Returns 'added', 'updated',
        'unchanged', 'revoked' or 'ignored' (another SKU, or not a server's)."""
        now = now or datetime.now(timezone.utc)
        if ent.deleted:
            return await self.remove(ent.id, now, reason="deleted by Discord (refund or removed)")
        values = row_values(ent, now, self.grace_days)
        if values is None:
            log.info("Ignoring Discord entitlement %s (SKU %s, guild %s)",
                     ent.id, ent.sku_id, ent.guild_id)
            return "ignored"
        tier, starts_at, ends_at, reason = values
        outcome, row_id = await self.db.sync_discord_entitlement(
            str(ent.id), str(ent.guild_id), tier,
            starts_at=starts_at, ends_at=ends_at, reason=reason,
        )
        if outcome != "unchanged":
            self.tiers.invalidate(str(ent.guild_id))
            log.warning("Discord entitlement %s %s: guild %s %s until %s (#%d)",
                        ent.id, outcome, ent.guild_id, tier, ends_at or "no end", row_id)
        return outcome

    async def remove(self, entitlement_id: int, now: datetime | None = None, *,
                     reason: str = "deleted by Discord") -> str:
        """Revoke the row for a deleted entitlement: 'revoked' or 'ignored'."""
        now = now or datetime.now(timezone.utc)
        row = await self.db.revoke_discord_entitlement(str(entitlement_id), at=_fmt(now), reason=reason)
        if row is None:
            return "ignored"
        self.tiers.invalidate(row["GuildId"])
        log.warning("Discord entitlement %s revoked: guild %s lost %s (#%d)",
                    entitlement_id, row["GuildId"], row["Tier"], row["Id"])
        return "revoked"

    async def reconcile(
        self, entitlements: Iterable[DiscordEntitlement], now: datetime | None = None
    ) -> Counter:
        """Bring every Discord row in line with Discord's full list.

        ``entitlements`` must be the complete list for the app, deleted ones
        left out: a row whose entitlement is missing is revoked.
        """
        now = now or datetime.now(timezone.utc)
        counts: Counter = Counter()
        seen: set[str] = set()
        for ent in entitlements:
            seen.add(str(ent.id))
            counts[await self.apply(ent, now)] += 1
        for row in await self.db.unrevoked_discord_entitlements():
            if row["ExternalId"] not in seen:
                counts[await self.remove(
                    int(row["ExternalId"]), now, reason="no longer listed by Discord"
                )] += 1
        return counts
