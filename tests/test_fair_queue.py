"""Tests for the fair OCR queue (servers take turns, one whole request each)."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.utils.fair_queue import FairQueue  # noqa: E402


class _Recorder:
    """Runs fake requests through a queue and records what ran when."""

    def __init__(self, queue: FairQueue) -> None:
        self.queue = queue
        self.events: list[str] = []
        self.ahead: dict[str, int] = {}
        # Requests hold their slot until released, so every request in a
        # test is queued before the first one finishes.
        self.go = asyncio.Event()

    async def request(self, guild: str, name: str, images: int = 2) -> None:
        async def on_queued(ahead: int) -> None:
            self.ahead[name] = ahead

        async with self.queue.slot(guild, on_queued=on_queued):
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


if __name__ == "__main__":
    unittest.main()
