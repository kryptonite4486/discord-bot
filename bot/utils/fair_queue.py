"""Fair OCR queue: servers take turns, one whole request at a time.

A request is one upload command (an image, an ``/ingest image`` set, a zip
or a ``/ingest batch``). Once started it holds its slot until every image
in it is done, so a request is never interleaved with another.

When a slot frees up, the next request comes from the waiting server whose
last turn was longest ago (servers that haven't had a turn yet go first, in
the order they started waiting), not the oldest request overall. A server
with several requests waiting gets one turn per round, so it can't hold up a
server that has a single request. Within one server, requests run in the
order they arrived.

Priority lanes (paid tiers): each request carries a priority level, 0
Standard (Free), 1 Priority (Mid) or 2 Highest (Full). A server's level is
the level of its oldest waiting request. When a slot frees up, a level is
picked first, then a server within that level as above.

Levels are picked by smooth weighted round-robin with weights 1 : 2 : 4,
not by strict priority. While every level has work waiting, each run of 7
turns gives Highest 4, Priority 2 and Standard 1, spread out rather than
bunched. So a waiting Free server always gets at least a 1-in-7 share of
turns (1-in-5 against Full alone, 1-in-3 against Mid alone), however much
paid work keeps arriving: it can be slowed, never starved. We chose this
over strict priority with an aging bound because the guarantee is counted
in turns, not seconds. An aging timer would have to be tuned against
request lengths that range from one image to a 50-image zip, and once it
fires a backlog of aged Free requests would jump ahead of paid ones all at
once. A level with nothing waiting builds up no credit, so it can't burst
when its work returns.

When tiers aren't enforced every request is Standard, there is only one
level, and the queue behaves exactly as plain server turns.

Users are only told about their own server's requests; ``snapshot()`` gives
the whole queue for the operator.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable

# Turns per round for each priority level: Standard, Priority, Highest.
LEVEL_WEIGHTS = (1, 2, 4)
LEVEL_NAMES = ("Standard", "Priority", "Highest")


@dataclass
class _Waiter:
    seq: int
    fut: asyncio.Future[None]
    priority: int = 0
    queued_at: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class GuildQueueState:
    guild_id: str
    running: int
    waiting: int
    longest_run_seconds: float  # 0 when nothing is running
    longest_wait_seconds: float  # 0 when nothing is waiting
    priority: int = 0  # level of its oldest waiting request (0 if none waiting)


class FairQueue:
    def __init__(self, slots: int = 1, weights: tuple[int, ...] = LEVEL_WEIGHTS) -> None:
        self._slots = max(1, slots)
        self._weights = tuple(max(1, w) for w in weights) or (1,)
        # Level -> smooth weighted round-robin credit (only levels with work waiting).
        self._credit: dict[int, int] = {}
        # Server -> its waiting requests, oldest first.
        self._waiting: dict[str, deque[_Waiter]] = {}
        # Server -> start times of its running requests.
        self._active: dict[str, list[float]] = {}
        # Server -> turn number of its most recent start.
        self._last_turn: dict[str, int] = {}
        self._seq = 0
        self._turn = 0

    @property
    def slots(self) -> int:
        return self._slots

    @property
    def running(self) -> int:
        return sum(len(starts) for starts in self._active.values())

    @property
    def waiting(self) -> int:
        return sum(len(q) for q in self._waiting.values())

    def snapshot(self) -> list[GuildQueueState]:
        """Every server with running or waiting requests, busiest first."""
        now = time.monotonic()
        states = []
        for guild_id in set(self._active) | set(self._waiting):
            starts = self._active.get(guild_id, [])
            waiters = [w for w in self._waiting.get(guild_id, ()) if not w.fut.done()]
            if not starts and not waiters:
                continue
            states.append(
                GuildQueueState(
                    guild_id=guild_id,
                    running=len(starts),
                    waiting=len(waiters),
                    longest_run_seconds=now - min(starts) if starts else 0.0,
                    longest_wait_seconds=(
                        now - min(w.queued_at for w in waiters) if waiters else 0.0
                    ),
                    priority=waiters[0].priority if waiters else 0,
                )
            )
        states.sort(key=lambda s: (-(s.running + s.waiting), -s.longest_wait_seconds))
        return states

    @asynccontextmanager
    async def slot(
        self,
        guild_id: str,
        *,
        priority: int = 0,
        on_queued: Callable[[int], Awaitable[None]] | None = None,
    ) -> AsyncIterator[None]:
        """Hold a slot for one whole request.

        ``priority`` is the request's level (0 Standard, 1 Priority, 2
        Highest); see the module docstring. If the request has to wait, ``on_queued(own_ahead)`` is awaited with
        the number of this server's own requests running or queued before it.
        Other servers are deliberately not counted.
        """
        await self._acquire(guild_id, priority, on_queued)
        try:
            yield
        finally:
            self._release(guild_id)

    async def _acquire(
        self,
        guild_id: str,
        priority: int,
        on_queued: Callable[[int], Awaitable[None]] | None,
    ) -> None:
        if self.running < self._slots and not self._waiting:
            self._start(guild_id)
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._seq += 1
        queue = self._waiting.setdefault(guild_id, deque())
        own_ahead = len(self._active.get(guild_id, [])) + sum(
            1 for w in queue if not w.fut.done()
        )
        level = min(max(priority, 0), len(self._weights) - 1)
        queue.append(_Waiter(self._seq, fut, level))
        try:
            if on_queued is not None:
                await on_queued(own_ahead)
            await fut
        except BaseException:
            if fut.done() and not fut.cancelled():
                self._release(guild_id)  # granted a slot but cancelled before using it
            else:
                fut.cancel()
                self._discard(guild_id, fut)
            raise

    def _start(self, guild_id: str) -> None:
        self._active.setdefault(guild_id, []).append(time.monotonic())
        self._turn += 1
        self._last_turn[guild_id] = self._turn

    def _release(self, guild_id: str) -> None:
        starts = self._active[guild_id]
        starts.remove(min(starts))  # requests finish in any order; drop one
        if not starts:
            del self._active[guild_id]
        self._dispatch()

    def _dispatch(self) -> None:
        while self.running < self._slots and self._drop_cancelled():
            level = self._next_level()
            # Waiting server in that level whose last turn is oldest; ties by arrival.
            guild_id = min(
                (g for g, q in self._waiting.items() if q[0].priority == level),
                key=lambda g: (self._last_turn.get(g, 0), self._waiting[g][0].seq),
            )
            queue = self._waiting[guild_id]
            waiter = queue.popleft()
            if not queue:
                del self._waiting[guild_id]
            waiter.fut.set_result(None)
            self._start(guild_id)

    def _drop_cancelled(self) -> bool:
        """Drop cancelled requests at the front of each server's line; True if any wait."""
        for guild_id in list(self._waiting):
            queue = self._waiting[guild_id]
            while queue and queue[0].fut.done():
                queue.popleft()
            if not queue:
                del self._waiting[guild_id]
        return bool(self._waiting)

    def _next_level(self) -> int:
        """Smooth weighted round-robin over the levels that have work waiting."""
        levels = {q[0].priority for q in self._waiting.values()}
        # Idle levels lose their credit so they can't burst when work returns.
        for level in list(self._credit):
            if level not in levels:
                del self._credit[level]
        if len(levels) == 1:
            self._credit.clear()
            return next(iter(levels))
        for level in levels:
            self._credit[level] = self._credit.get(level, 0) + self._weights[level]
        # Highest credit wins; ties go to the higher level.
        chosen = max(levels, key=lambda lv: (self._credit[lv], lv))
        self._credit[chosen] -= sum(self._weights[lv] for lv in levels)
        return chosen

    def _discard(self, guild_id: str, fut: asyncio.Future[None]) -> None:
        queue = self._waiting.get(guild_id)
        if queue is None:
            return
        for waiter in queue:
            if waiter.fut is fut:
                queue.remove(waiter)
                break
        if not queue:
            del self._waiting[guild_id]
