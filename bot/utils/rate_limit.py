"""A sliding-window limiter, kept in memory.

Used for failed /redeem attempts (bot/utils/gift_codes.py) and for the
per-user OCR ingest limit (bot/utils/abuse.py). State is lost on restart,
which is fine for both: the windows are minutes long.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Hashable, Iterator


class SlidingWindowLimiter:
    """Allows each key ``limit`` units (events, or weighted amounts) per ``window`` seconds."""

    def __init__(self, limit: float, window: float) -> None:
        self.limit = limit
        self.window = window
        self._events: dict[Hashable, deque[tuple[float, float]]] = {}

    def _recent(self, key: Hashable, now: float) -> deque[tuple[float, float]]:
        events = self._events.get(key)
        if events is None:
            return deque()
        while events and now - events[0][0] >= self.window:
            events.popleft()
        if not events:
            self._events.pop(key, None)
        return events

    def total(self, key: Hashable, now: float | None = None) -> float:
        """Units recorded for ``key`` within the window."""
        now = time.monotonic() if now is None else now
        return sum(amount for _, amount in self._recent(key, now))

    def retry_after(
        self,
        key: Hashable,
        now: float | None = None,
        *,
        amount: float = 1,
        limit: float | None = None,
    ) -> float:
        """Seconds until ``amount`` more units fit; 0 if they fit now.

        ``limit`` overrides the default for this check (e.g. a higher limit
        for some users). An amount over the limit is treated as the limit,
        so one maximal request still fits in an empty window.
        """
        now = time.monotonic() if now is None else now
        limit = self.limit if limit is None else limit
        events = self._recent(key, now)
        excess = sum(a for _, a in events) + min(amount, limit) - limit
        if excess <= 0:
            return 0.0
        freed = 0.0
        for at, units in events:
            freed += units
            if freed >= excess:
                return self.window - (now - at)
        return self.window  # unreachable: every event ages out within the window

    def record(self, key: Hashable, amount: float = 1, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._recent(key, now)
        self._events.setdefault(key, deque()).append((now, amount))

    def active_keys(self, now: float | None = None) -> Iterator[Hashable]:
        """Keys with units still in the window."""
        now = time.monotonic() if now is None else now
        for key in list(self._events):
            if self._recent(key, now):
                yield key
