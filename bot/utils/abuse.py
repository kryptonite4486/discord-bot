"""Abuse controls for OCR: a per-user rate limit, and a cap on Free servers per person.

**Rate limit.** Each person can start ``requests`` OCR requests (/ingest
image, zip or batch) and ``images`` screenshots per window in each server.
It protects the shared OCR server, not a paid feature, so it applies whether
or not tiers are enforced. Members with Manage Server get ``admin_multiplier``
times as much (they upload the whole roster on reset day, but a server's own
admin can also be the one abusing it, so they aren't exempt); operators
(BOT_OWNER_IDS) are exempt. Kept in memory: a restart clears it. /add and
/ingest text aren't limited: they never reach the OCR server, and each is a
small database write.

**Free-server cap.** One person can use OCR in at most ``limit`` Free
servers at a time, counting the servers they own and the ones where they
run the OCR commands (FreeOcrLink). Servers on a paid, gifted, code or trial
plan don't count. A person's servers are taken in order of first use, so the
extra ones are the newest; a server whose OCR hasn't been used for
FREE_LINK_DAYS drops out. It's a tier rule: while tiers aren't enforced it
only logs "(not enforced)" and allows the request.

See docs/monetization-plan.md, section 2 item 18.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from bot.utils.rate_limit import SlidingWindowLimiter
from bot.utils.tiers import FREE

log = logging.getLogger(__name__)

TS = "%Y-%m-%d %H:%M:%S"
# A person's link to a Free server lapses this long after its last OCR use.
FREE_LINK_DAYS = 30


def _ts(now: datetime) -> str:
    return now.strftime(TS)


@dataclass(frozen=True)
class UserWindow:
    """One person's OCR use in one server within the rate-limit window."""

    guild_id: str
    user_id: int
    requests: int
    images: int
    refused: int


class IngestRateLimiter:
    """Per-user, per-server limit on OCR requests and screenshots (0 = no cap)."""

    def __init__(
        self, *, requests: int, images: int, window: float, admin_multiplier: int = 3
    ) -> None:
        self.requests = SlidingWindowLimiter(requests, window)
        self.images = SlidingWindowLimiter(images, window)
        self.refusals = SlidingWindowLimiter(0, window)  # counted for /ops abuse only
        self.window = window
        self.admin_multiplier = max(1, admin_multiplier)

    def limits(self, *, admin: bool) -> tuple[int, int]:
        """(requests, images) allowed per window."""
        m = self.admin_multiplier if admin else 1
        return int(self.requests.limit) * m, int(self.images.limit) * m

    def retry_after(
        self, guild_id: str, user_id: int, images: int, *, admin: bool = False,
        now: float | None = None,
    ) -> float:
        """Seconds until this request (1 request, ``images`` screenshots) fits; 0 now."""
        key = (guild_id, user_id)
        max_requests, max_images = self.limits(admin=admin)
        waits = [0.0]
        if max_requests:
            waits.append(self.requests.retry_after(key, now, limit=max_requests))
        if max_images and images:
            waits.append(self.images.retry_after(key, now, amount=images, limit=max_images))
        return max(waits)

    def try_start(
        self, guild_id: str, user_id: int, images: int, *, admin: bool = False,
        record: bool = True, now: float | None = None,
    ) -> float:
        """Count the request if it fits (and ``record``); else return the wait.

        A refusal is counted for /ops abuse either way.
        """
        now = time.monotonic() if now is None else now
        wait = self.retry_after(guild_id, user_id, images, admin=admin, now=now)
        key = (guild_id, user_id)
        if wait > 0:
            self.refusals.record(key, now=now)
        elif record:
            self.requests.record(key, now=now)
            if images:
                self.images.record(key, images, now=now)
        return wait

    def top(self, n: int = 10, now: float | None = None) -> list[UserWindow]:
        """People with the most requests in the window (then refusals, then images)."""
        now = time.monotonic() if now is None else now
        keys = set(self.requests.active_keys(now)) | set(self.refusals.active_keys(now))
        rows = [
            UserWindow(
                guild_id=g, user_id=u,
                requests=int(self.requests.total((g, u), now)),
                images=int(self.images.total((g, u), now)),
                refused=int(self.refusals.total((g, u), now)),
            )
            for g, u in keys
        ]
        rows.sort(key=lambda r: (-r.requests, -r.refused, -r.images, r.guild_id, r.user_id))
        return rows[:n]


@dataclass(frozen=True)
class CapCheck:
    allowed: bool
    over: bool = False  # someone is over the cap (refused, or only logged)
    user_id: str | None = None  # who is over it
    role: str | None = None  # 'owner' | 'runner'
    servers: int = 0  # their Free servers using OCR, counting this one
    limit: int = 0


@dataclass
class PersonServers:
    """A person and the Free servers they use OCR in (for /ops abuse)."""

    user_id: str
    # guild ID -> (first use, roles), oldest first
    servers: dict[str, tuple[str, set[str]]] = field(default_factory=dict)


class FreeServerCap:
    """At most ``limit`` Free servers using OCR per person (owner or runner)."""

    def __init__(self, db, tiers, *, limit: int, exempt_ids: frozenset[int] = frozenset()) -> None:
        self.db = db
        self.tiers = tiers
        self.limit = limit
        self.exempt = {str(i) for i in exempt_ids}

    async def _is_free(self, guild_id: str) -> bool:
        return (await self.tiers.status(guild_id)).policy is FREE

    async def people(
        self, now: datetime, *, user_ids: list[str] | None = None
    ) -> dict[str, PersonServers]:
        """Each person's Free servers with OCR use in the last FREE_LINK_DAYS."""
        since = _ts(now - timedelta(days=FREE_LINK_DAYS))
        rows = await self.db.free_ocr_links(since, user_ids=user_ids)
        free: dict[str, bool] = {}
        out: dict[str, PersonServers] = {}
        for row in rows:  # ordered by FirstAt
            gid = row["GuildId"]
            if gid not in free:
                free[gid] = await self._is_free(gid)
            if not free[gid]:
                continue
            person = out.setdefault(row["UserId"], PersonServers(row["UserId"]))
            first, roles = person.servers.setdefault(gid, (row["FirstAt"], set()))
            roles.add(row["Role"])
        return out

    def _links(self, owner_id: int | None, runner_id: int | None) -> list[tuple[str, str]]:
        links = [(str(owner_id), "owner")] if owner_id is not None else []
        if runner_id is not None:
            links.append((str(runner_id), "runner"))
        return [(uid, role) for uid, role in links if uid not in self.exempt]

    async def check(
        self, guild_id: str, *, owner_id: int | None, runner_id: int | None,
        now: datetime | None = None,
    ) -> CapCheck:
        """Whether this server may use OCR under the cap. Not enforced: allowed, logged."""
        if self.limit <= 0 or not await self._is_free(guild_id):
            return CapCheck(True)
        links = self._links(owner_id, runner_id)
        if not links:
            return CapCheck(True)
        now = now or datetime.now(timezone.utc)
        people = await self.people(now, user_ids=sorted({uid for uid, _ in links}))
        for uid, role in links:  # owner first
            servers = list(people.get(uid, PersonServers(uid)).servers)
            if guild_id in servers:
                over = servers.index(guild_id) >= self.limit
                count = len(servers)
            else:
                over = len(servers) >= self.limit
                count = len(servers) + 1
            if not over:
                continue
            if not self.tiers.enforced:
                log.info(
                    "Free-server cap (not enforced): guild %s would be refused OCR; "
                    "%s %s uses OCR in %d Free servers (limit %d)",
                    guild_id, role, uid, count, self.limit,
                )
                return CapCheck(True, True, uid, role, count, self.limit)
            log.info(
                "Free-server cap: refused OCR in guild %s; %s %s uses OCR in %d Free "
                "servers (limit %d)", guild_id, role, uid, count, self.limit,
            )
            return CapCheck(False, True, uid, role, count, self.limit)
        return CapCheck(True)

    async def record(
        self, guild_id: str, *, owner_id: int | None, runner_id: int | None,
        now: datetime | None = None,
    ) -> None:
        """Note an OCR request in a Free server (paid servers aren't recorded)."""
        if self.limit <= 0 or not await self._is_free(guild_id):
            return
        links = self._links(owner_id, runner_id)
        if not links:
            return
        now = now or datetime.now(timezone.utc)
        await self.db.record_free_ocr_links(
            guild_id, links, _ts(now),
            expire_before=_ts(now - timedelta(days=FREE_LINK_DAYS)),
        )

    async def over_cap(self, now: datetime | None = None) -> list[PersonServers]:
        """People using OCR in more than ``limit`` Free servers, most servers first."""
        if self.limit <= 0:
            return []
        people = await self.people(now or datetime.now(timezone.utc))
        over = [p for p in people.values() if len(p.servers) > self.limit]
        over.sort(key=lambda p: (-len(p.servers), p.user_id))
        return over


def rate_limit_message(wait: float, requests: int, images: int, window: float) -> str:
    caps = [f"{requests} uploads" if requests else "", f"{images} screenshots" if images else ""]
    per = " or ".join(c for c in caps if c)
    at = int(time.time() + wait + 0.999)
    return (
        f"⏳ You've sent a lot of screenshots for reading in this server recently "
        f"(each person can start up to {per} every {round(window / 60)} minutes, "
        f"so the reader stays quick for everyone). You can try again <t:{at}:R>. "
        f"`/add` and `/ingest text` still work meanwhile."
    )


def free_cap_message(check: CapCheck) -> str:
    who = "This server's owner" if check.role == "owner" else "You"
    verb = "uses" if check.role == "owner" else "use"
    return (
        f"🔒 {who} already {verb} screenshot reading in {check.servers - 1} other "
        f"servers on the **Free** plan. Each person can use it in up to "
        f"**{check.limit}** Free servers at a time; servers on a paid, gifted or "
        f"trial plan don't count, and a server drops out after {FREE_LINK_DAYS} days "
        f"without screenshots. Run `/premium` to see the plans, or use `/add` and "
        f"`/ingest text` meanwhile."
    )


class AbuseControls:
    """The rate limit and Free-server cap, checked before OCR is queued."""

    def __init__(self, db, tiers, settings) -> None:
        self.operators = frozenset(settings.bot_owner_ids)
        self.limiter = IngestRateLimiter(
            requests=settings.ingest_rate_requests,
            images=settings.ingest_rate_images,
            window=settings.ingest_rate_window_minutes * 60,
            admin_multiplier=settings.ingest_rate_admin_multiplier,
        )
        self.free_cap = FreeServerCap(
            db, tiers, limit=settings.free_servers_per_owner, exempt_ids=self.operators
        )

    async def check_ocr(
        self, *, guild_id: str, user_id: int, owner_id: int | None, images: int,
        admin: bool, start: bool,
    ) -> str | None:
        """None if the request may go ahead, else the reply explaining why not.

        ``start``: the request is about to be queued, so count it and note
        who used OCR where. Without it (an early check, before uploads are
        collected) nothing is counted except a refusal.
        """
        cap = await self.free_cap.check(guild_id, owner_id=owner_id, runner_id=user_id)
        if not cap.allowed:
            return free_cap_message(cap)
        if user_id not in self.operators:
            # Checked and counted with no await in between, so two requests
            # can't both take the last place.
            wait = self.limiter.try_start(
                guild_id, user_id, images if start else min(images, 1),
                admin=admin, record=start,
            )
            if wait > 0:
                requests_cap, images_cap = self.limiter.limits(admin=admin)
                log.info(
                    "Ingest rate limit: user %s in guild %s refused %d image(s) "
                    "(retry in %.0fs)", user_id, guild_id, images, wait,
                )
                return rate_limit_message(wait, requests_cap, images_cap, self.limiter.window)
        if start:
            await self.free_cap.record(guild_id, owner_id=owner_id, runner_id=user_id)
        return None


def _is_admin(interaction) -> bool:
    perms = getattr(interaction, "permissions", None)
    return bool(getattr(perms, "manage_guild", False) or getattr(perms, "administrator", False))


async def ensure_ocr_allowed(interaction, images: int, *, start: bool = True) -> bool:
    """Reply and return False if this person can't start OCR now.

    Call before OCR is queued, with ``start=True`` and the number of
    screenshots; commands that collect uploads first can also call it early
    with ``start=False``. Works whether or not the interaction was deferred.
    Without abuse controls (tests) everything is allowed.
    """
    abuse = getattr(getattr(interaction, "client", None), "abuse", None)
    if abuse is None or interaction.guild_id is None:
        return True
    guild = getattr(interaction, "guild", None)
    text = await abuse.check_ocr(
        guild_id=str(interaction.guild_id),
        user_id=interaction.user.id,
        owner_id=getattr(guild, "owner_id", None),
        images=images,
        admin=_is_admin(interaction),
        start=start,
    )
    if text is None:
        return True
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)
    return False
