"""Tests for subscription tiers, feature checks, the OCR quota and gifting."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.cogs.admin import Admin  # noqa: E402
from bot.cogs.ingest import Ingest  # noqa: E402
from bot.cogs.ops import (  # noqa: E402
    extended_end,
    format_entitlement_list,
    format_show,
    gift_end,
)
from bot.cogs.premium import format_premium  # noqa: E402
from bot.cogs.reports import ReportScope, Reports  # noqa: E402
from bot.db import Database  # noqa: E402
from bot.reporting import charts  # noqa: E402
from bot.utils.archive import ImageSource  # noqa: E402
from bot.utils.tiers import (  # noqa: E402
    FREE,
    FULL,
    MID,
    Tiers,
    TierStatus,
    ensure_feature,
    quota_week_start,
    status_from_entitlements,
    upgrade_message,
)

TS = "%Y-%m-%d %H:%M:%S"
NOW = datetime.now(timezone.utc)


def ts(delta_days: float) -> str:
    return (NOW + timedelta(days=delta_days)).strftime(TS)


def row(tier, source="gift", ends=None, **extra):
    return {"Tier": tier, "Source": source, "EndsAt": ends, **extra}


class StatusTests(unittest.TestCase):
    def test_no_entitlements_is_free(self) -> None:
        self.assertIs(status_from_entitlements([]).policy, FREE)
        self.assertEqual(TierStatus(FREE).describe(), "Free")

    def test_highest_tier_wins(self) -> None:
        status = status_from_entitlements([row("mid", "discord", ts(30)), row("full", "gift")])
        self.assertIs(status.policy, FULL)
        self.assertEqual(status.describe(), "Command — gifted")

    def test_same_tier_prefers_longest(self) -> None:
        status = status_from_entitlements(
            [row("mid", "discord", "2026-11-01 00:00:00"), row("mid", "gift", "2027-01-01 00:00:00")]
        )
        self.assertEqual(status.describe(), "Alliance — gifted until 2027-01-01")
        status = status_from_entitlements([row("mid", "gift", "2027-01-01 00:00:00"), row("mid", "trial")])
        self.assertIsNone(status.ends_at)

    def test_unknown_tier_ignored(self) -> None:
        self.assertIs(status_from_entitlements([row("platinum")]).policy, FREE)

    def test_tier_features(self) -> None:
        self.assertFalse(FREE.allows("zip_batch"))
        self.assertTrue(MID.allows("advanced_reports"))
        self.assertFalse(MID.allows("multi_channel_reports"))
        self.assertTrue(FULL.allows("multi_channel_reports"))
        self.assertEqual([t.ocr_images_per_week for t in (FREE, MID, FULL)], [25, 250, 1000])

    def test_quota_week_starts_sunday(self) -> None:
        wed = datetime(2026, 10, 7, 23, 0, tzinfo=timezone.utc)
        self.assertEqual(quota_week_start(wed).isoformat(), "2026-10-04")
        sun = datetime(2026, 10, 4, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(quota_week_start(sun).isoformat(), "2026-10-04")


class _DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "t.db")
        await self.db.connect()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def grant(self, guild="g1", tier="full", starts=-1, ends=None, source="gift"):
        return await self.db.add_entitlement(
            guild, tier, source, starts_at=ts(starts), ends_at=ends,
            granted_by="op", reason="test",
        )


class EntitlementDbTests(_DbCase):
    async def test_active_excludes_revoked_expired_and_future(self) -> None:
        keep = await self.grant(ends=ts(10))
        await self.grant(ends=ts(-0.5))  # expired
        await self.grant(starts=5)  # not started
        revoked = await self.grant()
        await self.db.revoke_entitlement(revoked, at=ts(0), actor_id="op", reason="x")
        active = await self.db.active_entitlements("g1", ts(0))
        self.assertEqual([r["Id"] for r in active], [keep])

    async def test_changes_are_audited(self) -> None:
        eid = await self.grant(ends=ts(30))
        await self.db.set_entitlement_end(eid, None, actor_id="op")
        self.assertTrue(await self.db.revoke_entitlement(eid, at=ts(0), actor_id="op", reason="done"))
        self.assertFalse(await self.db.revoke_entitlement(eid, at=ts(0), actor_id="op", reason="again"))
        actions = [a["Action"] for a in await self.db.entitlement_audit("g1")]
        self.assertEqual(actions, ["revoke", "extend", "grant"])

    async def test_usage_total(self) -> None:
        await self.db.add_usage("g1", "2026-10-04", {"ocr_images": 5})
        await self.db.add_usage("g1", "2026-10-03", {"ocr_images": 99})
        await self.db.add_usage("g2", "2026-10-05", {"ocr_images": 7})
        self.assertEqual(await self.db.usage_total("g1", "2026-10-04", "ocr_images"), 5)


class TierServiceTests(_DbCase):
    async def test_status_and_cache_invalidation(self) -> None:
        tiers = Tiers(self.db, enforced=True)
        self.assertIs((await tiers.status("g1")).policy, FREE)
        await self.grant(tier="mid")
        self.assertIs((await tiers.status("g1")).policy, FREE)  # cached
        tiers.invalidate("g1")
        self.assertIs((await tiers.status("g1")).policy, MID)

    async def test_feature_blocked_only_when_enforced(self) -> None:
        enforced = Tiers(self.db, enforced=True)
        self.assertFalse((await enforced.check_feature("g1", "zip_batch"))[0])
        shadow = Tiers(self.db, enforced=False)
        with self.assertLogs("bot.utils.tiers", level="INFO") as logs:
            self.assertTrue((await shadow.check_feature("g1", "zip_batch"))[0])
        self.assertIn("would need Alliance for zip_batch", logs.output[0])

    async def test_ocr_quota_counts_usage_and_in_flight(self) -> None:
        tiers = Tiers(self.db, enforced=True)
        await self.db.add_usage("g1", quota_week_start().isoformat(), {"ocr_images": 20})
        allowed, used, limit = await tiers.reserve_ocr("g1", 10)
        self.assertEqual((allowed, used, limit), (5, 20, 25))
        # A second batch while the first is still running gets nothing.
        self.assertEqual((await tiers.reserve_ocr("g1", 3))[0], 0)
        tiers.release_ocr("g1", 5)
        tiers.release_ocr("g1", 0)
        self.assertEqual(tiers._ocr_in_flight, {})

    async def test_ocr_quota_not_enforced_allows_all(self) -> None:
        tiers = Tiers(self.db, enforced=False)
        await self.db.add_usage("g1", quota_week_start().isoformat(), {"ocr_images": 25})
        with self.assertLogs("bot.utils.tiers", level="INFO") as logs:
            allowed, _, _ = await tiers.reserve_ocr("g1", 4)
        self.assertEqual(allowed, 4)
        self.assertIn("would get 0 of 4", logs.output[0])

    async def test_paid_tier_quota(self) -> None:
        tiers = Tiers(self.db, enforced=True)
        await self.grant(tier="mid")
        self.assertEqual(await tiers.reserve_ocr("g1", 40), (40, 0, 250))


class _Response:
    def __init__(self, done=False):
        self.sent: list[str] = []
        self._done = done

    def is_done(self):
        return self._done

    async def send_message(self, text, ephemeral=False):
        self.sent.append(text)


def _interaction(tiers, *, done=False):
    followups: list[str] = []

    async def followup_send(text, ephemeral=False):
        followups.append(text)

    inter = SimpleNamespace(
        client=SimpleNamespace(tiers=tiers), guild_id=1,
        response=_Response(done), followup=SimpleNamespace(send=followup_send),
    )
    return inter, followups


class GateTests(_DbCase):
    async def test_ensure_feature_replies_with_upgrade_message(self) -> None:
        inter, _ = _interaction(Tiers(self.db, enforced=True))
        self.assertFalse(await ensure_feature(inter, "advanced_reports"))
        self.assertIn("need the **Alliance** plan", inter.response.sent[0])
        self.assertIn("This server is on **Free**", inter.response.sent[0])

    async def test_ensure_feature_uses_followup_after_defer(self) -> None:
        inter, followups = _interaction(Tiers(self.db, enforced=True), done=True)
        self.assertFalse(await ensure_feature(inter, "multi_channel_reports"))
        self.assertIn("**Command** plan", followups[0])

    async def test_ensure_feature_allows_without_tier_service(self) -> None:
        inter, _ = _interaction(None)
        self.assertTrue(await ensure_feature(inter, "zip_batch"))

    def test_gated_commands_have_checks(self) -> None:
        bot = SimpleNamespace(settings=SimpleNamespace(
            ocr_vision_base_url="http://x/v1", ocr_vision_model="m", ocr_vision_api_key="",
            ocr_vision_timeout=5.0, ocr_max_concurrency=1,
        ))
        gated = {
            "report player", "report trend", "report growth",
            "ingest zip", "ingest batch", "admin duplicates", "admin rename-player",
        }
        found = set()
        for cog in (Reports(bot), Ingest(bot), Admin(bot)):  # type: ignore[arg-type]
            for cmd in cog.walk_app_commands():
                if cmd.qualified_name in gated:
                    self.assertTrue(cmd.checks, cmd.qualified_name)
                    found.add(cmd.qualified_name)
                elif cmd.qualified_name in {"report week", "ingest image", "admin stats"}:
                    self.assertFalse(
                        [c for c in cmd.checks if "requires_feature" in c.__qualname__],
                        cmd.qualified_name,
                    )
        self.assertEqual(found, gated)

    async def test_multi_channel_report_needs_command_plan(self) -> None:
        cog = Reports(SimpleNamespace())  # type: ignore[arg-type]
        inter, followups = _interaction(Tiers(self.db, enforced=True), done=True)
        for channel_ids, blocked in ((None, True), (["1", "2"], True), (["1"], False)):
            scope = ReportScope(guild_id="1", channel_ids=channel_ids, show_channel=False, channel_names={})
            with patch.object(cog, "_resolve_scope_or_prompt_any", AsyncMock(return_value=scope)):
                result = await cog._resolve_scope_or_prompt(inter, None)
            self.assertEqual(result is None, blocked, channel_ids)
        self.assertEqual(len(followups), 2)


class QuotaBatchTests(_DbCase):
    def _cog(self, tiers):
        settings = SimpleNamespace(
            ocr_vision_base_url="http://x/v1", ocr_vision_model="m", ocr_vision_api_key="",
            ocr_vision_timeout=5.0, ocr_max_concurrency=1,
        )

        async def add_usage(*args):
            return None

        bot = SimpleNamespace(settings=settings, db=SimpleNamespace(add_usage=add_usage), tiers=tiers)
        return Ingest(bot)  # type: ignore[arg-type]

    def _images(self, n):
        return [ImageSource(key=str(i), filename=f"s{i}.png", data=b"") for i in range(n)]

    async def test_batch_over_quota_is_trimmed_and_explained(self) -> None:
        tiers = Tiers(self.db, enforced=True)
        await self.db.add_usage("g1", quota_week_start().isoformat(), {"ocr_images": 22})
        cog = self._cog(tiers)
        processed = []

        async def fake_process(image, *args):
            processed.append(image.filename)
            return "detail", 1, {"P"}, []

        with patch.object(cog, "_process_attachment", side_effect=fake_process):
            summary = await cog._process_attachments(self._images(5), "kills", "w", "g1", "c")
        self.assertEqual(processed, ["s0.png", "s1.png", "s2.png"])
        self.assertIn("Weekly screenshot limit: 2 image(s) not processed", summary)
        self.assertIn("`s3.png`, `s4.png`", summary)
        self.assertEqual(tiers._ocr_in_flight, {})

    async def test_quota_used_up_processes_nothing(self) -> None:
        tiers = Tiers(self.db, enforced=True)
        await self.db.add_usage("g1", quota_week_start().isoformat(), {"ocr_images": 25})
        cog = self._cog(tiers)
        with patch.object(cog, "_process_attachment", side_effect=AssertionError("ran")):
            summary = await cog._process_attachments(self._images(2), "kills", "w", "g1", "c")
        self.assertIn("has used its **25** screenshots for this week", summary)


class OpsFormattingTests(unittest.TestCase):
    def test_gift_and_extend_dates(self) -> None:
        start = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
        self.assertEqual(gift_end("30", start), "2026-11-05 12:00:00")
        self.assertIsNone(gift_end("permanent", start))
        # Extending a lapsed gift counts from now, not from the old end.
        self.assertEqual(extended_end("2026-01-01 00:00:00", "30", start), "2026-11-05 12:00:00")
        self.assertEqual(extended_end("2026-12-01 00:00:00", "30", start), "2026-12-31 00:00:00")
        self.assertIsNone(extended_end("2026-12-01 00:00:00", "permanent", start))

    def test_show_and_list(self) -> None:
        now = "2026-10-06 12:00:00"
        rows = [
            {"Id": 2, "GuildId": "123", "Tier": "full", "Source": "gift", "StartsAt": "2026-10-01 00:00:00",
             "EndsAt": None, "RevokedAt": None, "Reason": "own alliance"},
            {"Id": 1, "GuildId": "123", "Tier": "mid", "Source": "gift", "StartsAt": "2026-09-01 00:00:00",
             "EndsAt": "2026-10-01 00:00:00", "RevokedAt": None, "Reason": None},
        ]
        audit = [{"At": "2026-10-01 00:00:00", "Action": "grant", "ActorId": "42", "Detail": "#2 full gift"}]
        text = format_show("123", "SWag", rows, audit, 12, now)
        self.assertIn("Plan: **Command — gifted**", text)
        self.assertIn("12/1000", text)
        self.assertIn("#2 Command (gift): active, no end — own alliance", text)
        self.assertIn("#1 Alliance (gift): ended 2026-10-01", text)
        self.assertIn("grant by `42`", text)
        listing = format_entitlement_list(rows[:1], {"123": "SWag"}, None)
        self.assertIn("#2 SWag: Command (gift) until no end", listing)

    def test_premium_text(self) -> None:
        text = format_premium(TierStatus(MID, "gift", "2027-01-01 00:00:00"), 37, enforced=False)
        self.assertIn("Plan: Alliance — gifted until 2027-01-01", text)
        self.assertIn("**37 / 250**", text)
        self.assertIn("aren't switched on yet", text)
        self.assertLess(len(text), 2000)
        self.assertNotIn("aren't switched on", format_premium(TierStatus(FREE), 0, enforced=True))

    def test_upgrade_message_names_plan(self) -> None:
        self.assertIn("**Command** plan", upgrade_message("multi_channel_reports", TierStatus(MID)))



class ChartWatermarkTests(_DbCase):
    async def test_watermark_only_for_free_when_enforced(self) -> None:
        enforced = Tiers(self.db, enforced=True)
        self.assertTrue(await enforced.chart_watermark("g1"))
        await self.grant(guild="g2", tier="mid")
        await self.grant(guild="g3", tier="full")
        self.assertFalse(await enforced.chart_watermark("g2"))
        self.assertFalse(await enforced.chart_watermark("g3"))
        shadow = Tiers(self.db, enforced=False)
        self.assertFalse(await shadow.chart_watermark("g1"))

    async def test_report_cog_asks_tier_service(self) -> None:
        self.assertFalse(await Reports(SimpleNamespace())._chart_watermark("g1"))  # type: ignore[arg-type]
        cog = Reports(SimpleNamespace(tiers=Tiers(self.db, enforced=True)))  # type: ignore[arg-type]
        self.assertTrue(await cog._chart_watermark("g1"))

    def test_watermarked_charts_are_valid_pngs(self) -> None:
        from PIL import Image

        rows = [{"PlayerName": f"P{i}", "Value": 100 - i} for i in range(10)]
        trend = [
            {"PlayerName": "P1", "MetricType": "Kills", "WeekStart": f"2026-09-0{d}", "Value": d}
            for d in range(1, 5)
        ]
        renders = {
            "leaderboard": lambda wm: charts.leaderboard_bar_chart("Kills", "2026-10-04", rows, watermark=wm),
            "growth": lambda wm: charts.growth_bar_chart(
                "Kills", [dict(r, GrowthPct=r["Value"] - 95.0) for r in rows], watermark=wm
            ),
            "trend": lambda wm: charts.metric_trend_chart("Kills", trend, watermark=wm),
            "player": lambda wm: charts.player_trend_chart("P1", trend, watermark=wm),
        }
        for name, render in renders.items():
            with self.subTest(name):
                plain = Image.open(render(False))
                marked = Image.open(render(True))
                marked.verify()
                self.assertEqual(marked.format, "PNG")
                # The watermark sits below the chart, so the image grows
                # instead of the text covering the data.
                self.assertGreater(marked.height, plain.height)


if __name__ == "__main__":
    unittest.main()
