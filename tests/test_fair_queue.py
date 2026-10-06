"""Tests for the fair OCR queue (servers take turns, one whole request each,
with paid tiers getting a bigger weighted share of turns)."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.utils.fair_queue import FairQueue  # noqa: E402
from bot.utils.tiers import Tiers  # noqa: E402


class _Recorder:
    """Runs fake requests through a queue and records what ran when."""

    def __init__(self, queue: FairQueue) -> None:
        self.queue = queue
        self.events: list[str] = []
        self.ahead: dict[str, int] = {}
        # Requests hold their slot until released, so every request in a
        # test is queued before the first one finishes.
        self.go = asyncio.Event()

    async def request(
        self, guild: str, name: str, images: int = 2, priority: int = 0
    ) -> None:
        async def on_queued(ahead: int) -> None:
            self.ahead[name] = ahead

        async with self.queue.slot(guild, priority=priority, on_queued=on_queued):
            await self.go.wait()
            for i in range(images):
                self.events.append(f"{name}.{i}")
                await asyncio.sleep(0)

    def order(self) -> list[str]:
        """Request names in the order they started."""
        seen: list[str] = []
        for event in self.events:
            name = event.split(".")[0]
            if name not in seen:
                seen.append(name)
        return seen


async def _start(*coros) -> list[asyncio.Task]:
    """Start tasks one at a time so they queue in this order."""
    tasks = []
    for coro in coros:
        tasks.append(asyncio.create_task(coro))
        await asyncio.sleep(0)
    return tasks


class FairQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_servers_take_turns_by_request(self) -> None:
        rec = _Recorder(FairQueue(1))
        tasks = await _start(
            rec.request("A", "a1"),
            rec.request("A", "a2"),
            rec.request("A", "a3"),
            rec.request("B", "b1"),
            rec.request("C", "c1"),
        )
        rec.go.set()
        await asyncio.gather(*tasks)
        # A just had a turn (a1), so B and C go next; then A's backlog.
        self.assertEqual(rec.order(), ["a1", "b1", "c1", "a2", "a3"])

    async def test_a_request_is_never_interleaved(self) -> None:
        rec = _Recorder(FairQueue(1))
        tasks = await _start(
            rec.request("A", "a1", images=5),
            rec.request("B", "b1", images=5),
            rec.request("A", "a2", images=5),
        )
        rec.go.set()
        await asyncio.gather(*tasks)
        expected = [f"{n}.{i}" for n in ("a1", "b1", "a2") for i in range(5)]
        self.assertEqual(rec.events, expected)

    async def test_same_server_runs_in_arrival_order(self) -> None:
        rec = _Recorder(FairQueue(1))
        tasks = await _start(*(rec.request("A", f"a{i}") for i in range(4)))
        rec.go.set()
        await asyncio.gather(*tasks)
        self.assertEqual(rec.order(), ["a0", "a1", "a2", "a3"])

    async def test_queued_count_only_includes_own_server(self) -> None:
        rec = _Recorder(FairQueue(1))
        tasks = await _start(
            rec.request("A", "a1"),
            rec.request("A", "a2"),
            rec.request("B", "b1"),
            rec.request("A", "a3"),
        )
        snapshot = {s.guild_id: s for s in rec.queue.snapshot()}
        rec.go.set()
        await asyncio.gather(*tasks)
        self.assertNotIn("a1", rec.ahead)  # started straight away
        self.assertEqual(rec.ahead["a2"], 1)  # a1 running
        self.assertEqual(rec.ahead["b1"], 0)  # A's requests are not B's business
        self.assertEqual(rec.ahead["a3"], 2)  # a1 running, a2 waiting
        self.assertEqual(rec.order(), ["a1", "b1", "a2", "a3"])
        # The operator view has everything.
        self.assertEqual((snapshot["A"].running, snapshot["A"].waiting), (1, 2))
        self.assertEqual((snapshot["B"].running, snapshot["B"].waiting), (0, 1))
        self.assertGreaterEqual(snapshot["B"].longest_wait_seconds, 0)
        self.assertEqual(snapshot["B"].longest_run_seconds, 0)
        self.assertEqual(rec.queue.snapshot(), [])

    async def test_snapshot_skips_cancelled_waiters(self) -> None:
        rec = _Recorder(FairQueue(1))
        a1, b1 = await _start(rec.request("A", "a1"), rec.request("B", "b1"))
        b1.cancel()
        await asyncio.sleep(0)
        self.assertEqual([s.guild_id for s in rec.queue.snapshot()], ["A"])
        rec.go.set()
        await a1

    async def test_slots_allow_parallel_requests(self) -> None:
        queue = FairQueue(2)
        running = peak = 0

        async def request(guild: str) -> None:
            nonlocal running, peak
            async with queue.slot(guild):
                running += 1
                peak = max(peak, running)
                await asyncio.sleep(0.01)
                running -= 1

        await asyncio.gather(*(request(g) for g in "AABBC"))
        self.assertEqual(peak, 2)
        self.assertEqual((queue.running, queue.waiting), (0, 0))

    async def test_cancelled_waiter_is_skipped(self) -> None:
        rec = _Recorder(FairQueue(1))
        a1, b1, c1 = await _start(
            rec.request("A", "a1"),
            rec.request("B", "b1"),
            rec.request("C", "c1"),
        )
        b1.cancel()
        rec.go.set()
        await asyncio.gather(a1, c1)
        with self.assertRaises(asyncio.CancelledError):
            await b1
        self.assertEqual(rec.order(), ["a1", "c1"])
        self.assertEqual((rec.queue.running, rec.queue.waiting), (0, 0))

    async def test_failed_request_frees_its_slot(self) -> None:
        queue = FairQueue(1)

        async def boom() -> None:
            async with queue.slot("A"):
                raise RuntimeError("vision server down")

        with self.assertRaises(RuntimeError):
            await boom()
        async with queue.slot("B"):
            self.assertEqual(queue.running, 1)
        self.assertEqual(queue.running, 0)


class _FakeEntitlementsDb:
    """Just enough of the database for Tiers.status()."""

    def __init__(self, tiers: dict[str, str]) -> None:
        self.tiers = tiers

    async def active_entitlements(self, guild_id: str, now: str) -> list[dict]:
        tier = self.tiers.get(guild_id)
        if tier is None:
            return []
        return [{"Tier": tier, "Source": "gift", "EndsAt": None}]


STANDARD, PRIORITY, HIGHEST = 0, 1, 2


class PriorityLaneTests(unittest.IsolatedAsyncioTestCase):
    async def test_higher_tiers_go_first(self) -> None:
        rec = _Recorder(FairQueue(1))
        tasks = await _start(
            rec.request("X", "x1"),  # holds the slot while the rest queue
            rec.request("F", "f1", priority=STANDARD),
            rec.request("M", "m1", priority=PRIORITY),
            rec.request("H", "h1", priority=HIGHEST),
        )
        rec.go.set()
        await asyncio.gather(*tasks)
        self.assertEqual(rec.order(), ["x1", "h1", "m1", "f1"])

    async def test_servers_take_turns_within_a_level(self) -> None:
        rec = _Recorder(FairQueue(1))
        tasks = await _start(
            rec.request("X", "x1"),
            rec.request("A", "a1", priority=HIGHEST),
            rec.request("A", "a2", priority=HIGHEST),
            rec.request("B", "b1", priority=HIGHEST),
        )
        rec.go.set()
        await asyncio.gather(*tasks)
        self.assertEqual(rec.order(), ["x1", "a1", "b1", "a2"])

    async def test_weighted_shares_when_every_level_is_busy(self) -> None:
        rec = _Recorder(FairQueue(1))
        tasks = await _start(
            rec.request("X", "x0"),
            *(rec.request("H", f"h{i}", priority=HIGHEST) for i in range(8)),
            *(rec.request("M", f"m{i}", priority=PRIORITY) for i in range(4)),
            *(rec.request("F", f"f{i}", priority=STANDARD) for i in range(2)),
        )
        rec.go.set()
        await asyncio.gather(*tasks)
        order = rec.order()[1:]
        for window in (order[:7], order[7:14]):
            lanes = [name[0] for name in window]
            self.assertEqual(
                (lanes.count("h"), lanes.count("m"), lanes.count("f")), (4, 2, 1), window
            )
        # Spread out, not bunched: Highest never runs more than twice in a row.
        self.assertNotIn("hhh", "".join(name[0] for name in order))

    async def test_free_server_is_not_starved_by_sustained_paid_load(self) -> None:
        queue = FairQueue(1)
        started: list[str] = []
        stop = asyncio.Event()

        async def paid_server(guild: str) -> None:
            # Always has another Full request waiting as soon as one finishes.
            while not stop.is_set():
                async with queue.slot(guild, priority=HIGHEST):
                    started.append(guild)
                    await asyncio.sleep(0)

        async def free_request(name: str) -> None:
            async with queue.slot("F", priority=STANDARD):
                started.append(name)
                await asyncio.sleep(0)

        paid = [asyncio.create_task(paid_server(g)) for g in ("H1", "H2", "H3")]
        # Full runs alone for a while first; that mustn't bank it extra turns.
        while len(started) < 20:
            await asyncio.sleep(0)
        arrived = len(started)
        free = [asyncio.create_task(free_request(f"f{i}")) for i in range(3)]
        await asyncio.gather(*free)
        stop.set()
        await asyncio.gather(*paid)
        # Against Full alone, Free gets one turn in every five.
        free_turns = [i for i, name in enumerate(started) if name.startswith("f")]
        self.assertEqual(len(free_turns), 3)
        gaps = [b - a for a, b in zip([arrived - 1, *free_turns], free_turns)]
        self.assertLessEqual(max(gaps), 5, started[arrived:])

    async def test_cancelled_request_does_not_use_up_its_levels_turn(self) -> None:
        rec = _Recorder(FairQueue(1))
        x0, h1, f1, f2 = await _start(
            rec.request("X", "x0"),
            rec.request("H", "h1", priority=HIGHEST),
            rec.request("F", "f1", priority=STANDARD),
            rec.request("G", "g1", priority=STANDARD),
        )
        f1.cancel()
        rec.go.set()
        await asyncio.gather(x0, h1, f2)
        self.assertEqual(rec.order(), ["x0", "h1", "g1"])

    async def test_snapshot_shows_each_servers_level(self) -> None:
        rec = _Recorder(FairQueue(1))
        tasks = await _start(
            rec.request("X", "x0"),
            rec.request("H", "h1", priority=HIGHEST),
            rec.request("M", "m1", priority=PRIORITY),
        )
        levels = {s.guild_id: s.priority for s in rec.queue.snapshot()}
        rec.go.set()
        await asyncio.gather(*tasks)
        self.assertEqual(levels, {"X": STANDARD, "H": HIGHEST, "M": PRIORITY})

    async def test_priority_comes_from_tier_only_when_enforced(self) -> None:
        db = _FakeEntitlementsDb({"M": "mid", "H": "full"})
        enforced = Tiers(db, enforced=True)
        shadow = Tiers(db, enforced=False)
        self.assertEqual(
            [await enforced.queue_priority(g) for g in ("F", "M", "H")],
            [STANDARD, PRIORITY, HIGHEST],
        )
        self.assertEqual(
            [await shadow.queue_priority(g) for g in ("F", "M", "H")],
            [STANDARD, STANDARD, STANDARD],
        )

    async def test_enforcement_off_matches_plain_server_turns(self) -> None:
        db = _FakeEntitlementsDb({"B": "mid", "C": "full"})

        async def run(tiers: Tiers) -> list[str]:
            rec = _Recorder(FairQueue(1))
            reqs = [("A", "a1"), ("A", "a2"), ("A", "a3"), ("B", "b1"), ("C", "c1"), ("A", "a4")]
            # Looked up at queue time, as the ingest cog does.
            priorities = [await tiers.queue_priority(g) for g, _ in reqs]
            tasks = await _start(
                *(rec.request(g, n, priority=p) for (g, n), p in zip(reqs, priorities))
            )
            rec.go.set()
            await asyncio.gather(*tasks)
            return rec.order()

        # Same order as a queue that knows nothing about tiers.
        self.assertEqual(
            await run(Tiers(db, enforced=False)), ["a1", "b1", "c1", "a2", "a3", "a4"]
        )
        # With enforcement on, Full and Mid would be served differently.
        self.assertEqual(
            await run(Tiers(db, enforced=True)), ["a1", "c1", "b1", "a2", "a3", "a4"]
        )


if __name__ == "__main__":
    unittest.main()
