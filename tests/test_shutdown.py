"""Tests that SIGTERM/SIGINT trigger a clean bot.close() (no Discord required)."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import unittest
from pathlib import Path

# Allow `python tests/test_shutdown.py` from repo root
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.main import _install_shutdown_handlers  # noqa: E402


class _FakeBot:
    def __init__(self) -> None:
        self.close_calls = 0
        self.closed = asyncio.Event()

    async def close(self) -> None:
        self.close_calls += 1
        self.closed.set()


@unittest.skipIf(sys.platform == "win32", "loop signal handlers need Unix")
class ShutdownSignalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)

    async def _assert_signal_closes(self, sig: signal.Signals) -> None:
        bot = _FakeBot()
        _install_shutdown_handlers(bot)  # type: ignore[arg-type]
        os.kill(os.getpid(), sig)
        await asyncio.wait_for(bot.closed.wait(), timeout=2)
        # A second signal while closing must not schedule another close.
        os.kill(os.getpid(), sig)
        await asyncio.sleep(0.05)
        self.assertEqual(bot.close_calls, 1)

    async def test_sigterm_closes_bot(self) -> None:
        await self._assert_signal_closes(signal.SIGTERM)

    async def test_sigint_closes_bot(self) -> None:
        await self._assert_signal_closes(signal.SIGINT)


if __name__ == "__main__":
    unittest.main()
