"""Tests for the tracked-channel (dataset) limit of each tier."""

from __future__ import annotations

import sys
import inspect
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.cogs.ingest import Ingest  # noqa: E402
from bot.cogs.premium import format_premium  # noqa: E402
from bot.db import Database  # noqa: E402
from bot.utils.tiers import (  # noqa: E402
    FREE,
    FULL,
    MID,
    Tiers,
    TierStatus,
    channel_allowed,
    channel_limit_message,
    ensure_channel,
    writable_channels,
)

TS = "%Y-%m-%d %H:%M:%S"
NOW = datetime.now(timezone.utc)


def ts(delta_days: float) -> str:
    return (NOW + timedelta(days=delta_days)).strftime(TS)


class PolicyTests(unittest.TestCase):
    def test_limits_per_tier(self) -> None:
        self.assertEqual([t.max_channels for t in (FREE, MID, FULL)], [1, 3, None])

    def test_new_channel_fits_until_the_limit(self) -> None:
        self.assertTrue(channel_allowed("a", [], FREE))
        self.assertFalse(channel_allowed("b", ["a"], FREE))
        self.assertTrue(channel_allowed("c", ["a", "b"], MID))
        self.assertFalse(channel_allowed("d", ["a", "b", "c"], MID))
        self.assertTrue(channel_allowed("z", [str(i) for i in range(50)], FULL))

    def test_over_limit_keeps_most_recent_writable(self) -> None:
        tracked = ["new", "mid", "old"]  # most recently written first
        self.assertEqual(writable_channels(tracked, FREE), ["new"])
        self.assertEqual(writable_channels(tracked, MID), tracked)
        self.assertTrue(channel_allowed("new", tracked, FREE))
        self.assertFalse(channel_allowed("old", tracked, FREE))


class _DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "t.db")
        await self.db.connect()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def grant(self, tier: str) -> None:
        await self.db.add_entitlement(
            "1", tier, "gift", starts_at=ts(-1), ends_at=None, granted_by="op", reason="test"
        )

    async def write(self, channel: str, days_ago: float = 0) -> None:
        """Store a row in ``channel``, last written ``days_ago`` days ago."""
        await self.db.upsert_metric("1", "2026-10-05", "P", "Kills", 1, channel_id=channel)
        await self.db.conn.execute(
            "UPDATE WeeklyMetrics SET UpdatedAt = ? WHERE GuildId = '1' AND ChannelId = ?",
            (ts(-days_ago), channel),
        )
        await self.db.conn.commit()


class TrackedChannelTests(_DbCase):
    async def test_most_recent_first_and_unassigned_left_out(self) -> None:
        await self.write("10", days_ago=5)
        await self.write("20", days_ago=1)
        await self.write("30", days_ago=3)
        await self.write("", days_ago=0)
        self.assertEqual(await self.db.tracked_channels("1"), ["20", "30", "10"])
        self.assertEqual(await self.db.tracked_channels("2"), [])


class CheckChannelTests(_DbCase):
    async def allowed(self, tiers: Tiers, channel: str) -> bool:
        return (await tiers.check_channel("1", channel))[0]

    async def test_free_allows_one_channel(self) -> None:
        tiers = Tiers(self.db, enforced=True)
        self.assertTrue(await self.allowed(tiers, "10"))
        await self.write("10")
        self.assertTrue(await self.allowed(tiers, "10"))
        self.assertFalse(await self.allowed(tiers, "20"))

    async def test_alliance_allows_three_channels(self) -> None:
        await self.grant("mid")
        tiers = Tiers(self.db, enforced=True)
        for channel in ("10", "20", "30"):
            self.assertTrue(await self.allowed(tiers, channel))
            await self.write(channel)
        self.assertFalse(await self.allowed(tiers, "40"))

    async def test_command_is_unlimited(self) -> None:
        await self.grant("full")
        tiers = Tiers(self.db, enforced=True)
        for i in range(12):
            await self.write(str(i))
        self.assertTrue(await self.allowed(tiers, "99"))

    async def test_downgrade_keeps_data_and_recent_channels(self) -> None:
        await self.write("10", days_ago=9)
        await self.write("20", days_ago=1)
        await self.write("30", days_ago=4)
        await self.write("40", days_ago=6)
        before = await self.db.channel_row_counts("1")

        # Alliance → Free: only the most recently used channel takes new data.
        tiers = Tiers(self.db, enforced=True)
        self.assertTrue(await self.allowed(tiers, "20"))
        for channel in ("10", "30", "40", "50"):
            self.assertFalse(await self.allowed(tiers, channel), channel)

        # On Alliance, the three most recently used channels stay writable.
        await self.grant("mid")
        tiers.invalidate()
        self.assertEqual(
            [c for c in ("10", "20", "30", "40") if await self.allowed(tiers, c)],
            ["20", "30", "40"],
        )
        self.assertEqual(await self.db.channel_row_counts("1"), before)

    async def test_not_enforced_allows_and_logs(self) -> None:
        await self.write("10")
        tiers = Tiers(self.db, enforced=False)
        with self.assertLogs("bot.utils.tiers", level="INFO") as logs:
            self.assertTrue(await self.allowed(tiers, "20"))
        self.assertIn("would be refused data in channel 20", logs.output[0])


class MessageTests(unittest.TestCase):
    def test_new_channel_names_tracked_channels(self) -> None:
        text = channel_limit_message("99", ["10"], TierStatus(FREE))
        self.assertIn("adds new data in up to **1** channel.", text)
        self.assertIn("Add data in <#10> instead.", text)
        self.assertIn("The **Alliance** plan", text)

    def test_read_only_channel_after_downgrade(self) -> None:
        text = channel_limit_message("30", ["10", "20", "30", "40"], TierStatus(MID, "gift"))
        self.assertIn("This channel's data is kept", text)
        self.assertIn("3 most recently used channels: <#10>, <#20>, <#30>.", text)
        self.assertIn("The **Command** plan", text)

    def test_premium_shows_channel_limits(self) -> None:
        text = format_premium(TierStatus(MID), 0, enforced=True, channels=2)
        self.assertIn("Channels with data: **2 / 3**", text)
        self.assertRegex(text, r"Channels with data\s+1\s+3\s+any")
        self.assertNotIn("Channels with data: **", format_premium(TierStatus(FREE), 0, enforced=True))


class _Response:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.deferred = False

    def is_done(self) -> bool:
        return self.deferred or bool(self.sent)

    async def defer(self, **_) -> None:
        self.deferred = True

    async def send_message(self, text, ephemeral=False) -> None:
        self.sent.append(text)


def _interaction(tiers, channel_id=20):
    followups: list[str] = []

    async def followup_send(text, ephemeral=False):
        followups.append(text)

    inter = SimpleNamespace(
        client=SimpleNamespace(tiers=tiers), guild_id=1, channel_id=channel_id,
        response=_Response(), followup=SimpleNamespace(send=followup_send), user="u",
    )
    return inter, followups


class IngestGateTests(_DbCase):
    def _cog(self, tiers) -> Ingest:
        settings = SimpleNamespace(
            ocr_vision_base_url="http://x/v1", ocr_vision_model="m", ocr_vision_api_key="",
            ocr_vision_timeout=5.0, ocr_max_concurrency=1,
        )
        return Ingest(SimpleNamespace(settings=settings, db=self.db, tiers=tiers))  # type: ignore[arg-type]

    async def test_ensure_channel_without_tier_service(self) -> None:
        inter, _ = _interaction(None)
        self.assertTrue(await ensure_channel(inter))

    async def test_add_refused_in_extra_channel(self) -> None:
        await self.write("10")
        tiers = Tiers(self.db, enforced=True)
        inter, followups = _interaction(tiers, channel_id=20)
        await self._cog(tiers)._add_single(inter, "Kills", "P", "5", "2026-10-05")
        self.assertEqual(await self.db.tracked_channels("1"), ["10"])
        self.assertIn("<#10>", followups[0])

    async def test_add_works_when_not_enforced(self) -> None:
        await self.write("10")
        tiers = Tiers(self.db, enforced=False)
        inter, followups = _interaction(tiers, channel_id=20)
        await self._cog(tiers)._add_single(inter, "Kills", "P", "5", "2026-10-05")
        self.assertEqual(sorted(await self.db.tracked_channels("1")), ["10", "20"])
        self.assertIn("Saved", followups[0])

    async def test_text_ingest_refused_in_extra_channel(self) -> None:
        await self.write("10")
        tiers = Tiers(self.db, enforced=True)
        inter, followups = _interaction(tiers, channel_id=20)
        dataset = SimpleNamespace(value="kills")
        await self._cog(tiers).ingest_text.callback(
            self._cog(tiers), inter, dataset, "P,5", "2026-10-05"
        )
        self.assertEqual(await self.db.tracked_channels("1"), ["10"])
        self.assertIn("🔒", followups[0])

    def test_every_write_command_checks_the_channel(self) -> None:
        for name in ("_add_single", "add_general", "ingest_image", "ingest_zip",
                     "ingest_batch", "ingest_text"):
            attr = getattr(Ingest, name)
            source = inspect.getsource(getattr(attr, "callback", attr))
            self.assertIn("ensure_channel(interaction)", source, name)


if __name__ == "__main__":
    unittest.main()
