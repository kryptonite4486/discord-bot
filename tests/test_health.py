"""Tests for hosting monitoring: heartbeat, operator alerts, the OCR probe,
reconnect notices, offsite backup copies and /ops health."""

from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.ocr.pipeline import VisionOCR, extract_metrics_from_image  # noqa: E402
from bot.ocr.vision import VisionOCRError, VisionReplyError, VisionTransientError  # noqa: E402
from bot.utils import health as health_mod  # noqa: E402
from bot.utils.backup import list_backups, sync_offsite  # noqa: E402
from bot.utils.health import (  # noqa: E402
    ALERT_COOLDOWN,
    HEARTBEAT_INTERVAL,
    OCR_PROBE_INTERVAL,
    AlertManager,
    ConnectionWatch,
    Heartbeat,
    HealthState,
    OcrHealth,
    format_health_report,
    probe_ocr,
    set_ocr_tracker,
    short_reason,
    update_ocr_incident,
)

T0 = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
HEARTBEAT_URL = "https://hc-ping.com/secret-uuid"
MODEL = "Qwen2.5-VL-7B-Instruct-4bit"


class FakeClock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


class FakeHttp:
    """httpx client over a MockTransport that records requests."""

    def __init__(self, handler=None) -> None:
        self.requests: list[httpx.Request] = []
        self.handler = handler or (lambda req: httpx.Response(200))

        def record(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self.handler(request)

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(record))

    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]


def models_reply(*ids: str) -> httpx.Response:
    return httpx.Response(200, json={"object": "list", "data": [{"id": i} for i in ids]})


class _Async(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.clock = FakeClock()
        self.sent: list[str] = []
        self._tmp = tempfile.TemporaryDirectory()
        # Keep the Docker health file out of the real temp folder.
        patcher = patch.object(health_mod, "HEALTH_FILE", Path(self._tmp.name) / "healthy")
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def send(self, text: str) -> None:
        self.sent.append(text)


class AlertManagerTests(_Async):
    def manager(self) -> AlertManager:
        return AlertManager(self.send, clock=self.clock)

    async def test_one_alert_per_incident_and_one_recovery(self) -> None:
        alerts = self.manager()
        self.assertTrue(await alerts.problem("ocr", "OCR down: HTTP 503"))
        for _ in range(5):
            self.clock.advance(minutes=1)
            self.assertFalse(await alerts.problem("ocr", "OCR down: connection refused"))
        # The incident keeps the latest detail for /ops health.
        self.assertEqual(alerts.incidents["ocr"].detail, "OCR down: connection refused")
        self.clock.advance(minutes=4)
        self.assertTrue(await alerts.resolved("ocr", "OCR is back."))
        self.assertFalse(await alerts.resolved("ocr", "OCR is back."))
        self.assertEqual(self.sent, ["⚠️ OCR down: HTTP 503", "✅ OCR is back. (down 9m)"])
        self.assertEqual(alerts.incidents, {})

    async def test_flapping_inside_cooldown_stays_quiet(self) -> None:
        alerts = self.manager()
        await alerts.problem("ocr", "down")
        self.clock.advance(minutes=2)
        await alerts.resolved("ocr", "up")
        for _ in range(3):  # fails and recovers again within the cooldown
            self.clock.advance(minutes=2)
            self.assertFalse(await alerts.problem("ocr", "down"))
            self.clock.advance(minutes=2)
            self.assertFalse(await alerts.resolved("ocr", "up"))
        self.assertEqual(self.sent, ["⚠️ down", "✅ up (down 2m)"])

    async def test_held_alert_goes_out_when_cooldown_ends(self) -> None:
        alerts = self.manager()
        await alerts.problem("ocr", "down")
        self.clock.advance(minutes=1)
        await alerts.resolved("ocr", "up")
        self.clock.advance(minutes=1)
        self.assertFalse(await alerts.problem("ocr", "down again"))
        self.assertFalse(alerts.incidents["ocr"].alerted)
        self.clock.advance(minutes=27)
        self.assertFalse(await alerts.problem("ocr", "down again"))
        # The recovery message at 1m also counts: the cooldown runs from it.
        self.clock.now = T0 + timedelta(minutes=1) + ALERT_COOLDOWN
        self.assertTrue(await alerts.problem("ocr", "down again"))
        self.assertEqual(self.sent[-1], "⚠️ down again")

    async def test_kinds_have_separate_cooldowns(self) -> None:
        alerts = self.manager()
        await alerts.problem("ocr", "ocr down")
        self.assertTrue(await alerts.problem("backup", "backup failed"))
        self.assertEqual(len(self.sent), 2)

    async def test_notice_respects_cooldown(self) -> None:
        alerts = self.manager()
        self.assertTrue(await alerts.notice("reconnect", "back"))
        self.clock.advance(minutes=29)
        self.assertFalse(await alerts.notice("reconnect", "back"))
        self.clock.advance(minutes=1)
        self.assertTrue(await alerts.notice("reconnect", "back"))
        self.assertEqual(self.sent, ["back", "back"])

    async def test_failed_delivery_is_not_retried(self) -> None:
        alerts = AlertManager(AsyncMock(side_effect=RuntimeError("DMs closed")), clock=self.clock)
        with self.assertLogs("bot.utils.health", "ERROR"):
            self.assertTrue(await alerts.problem("ocr", "down"))
        self.assertFalse(await alerts.problem("ocr", "down"))
        self.assertEqual(alerts.send.await_count, 1)


class OcrHealthTests(_Async):
    async def test_alerts_after_threshold_and_recovers_once(self) -> None:
        ocr = OcrHealth(clock=self.clock)
        alerts = AlertManager(self.send, clock=self.clock)
        for _ in range(2):
            ocr.record(False, "HTTP 503")
            await update_ocr_incident(alerts, ocr)
        self.assertEqual(self.sent, [])  # two failures aren't an outage yet
        ocr.record(False, "HTTP 503")
        await update_ocr_incident(alerts, ocr)
        await update_ocr_incident(alerts, ocr)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("3 failed checks or OCR calls in a row, last error: HTTP 503", self.sent[0])
        self.clock.advance(minutes=6)
        ocr.record(True, probe=True)
        await update_ocr_incident(alerts, ocr)
        await update_ocr_incident(alerts, ocr)
        self.assertEqual(self.sent[1], "✅ OCR server is working again. (down 6m)")
        self.assertEqual(len(self.sent), 2)

    async def test_success_resets_the_count(self) -> None:
        ocr = OcrHealth(clock=self.clock)
        for ok in (False, False, True, False, False):
            ocr.record(ok, "x")
        self.assertFalse(ocr.snapshot().down)
        self.assertEqual(ocr.snapshot().consecutive_failures, 2)

    async def test_probe(self) -> None:
        http = FakeHttp(lambda req: models_reply("other", MODEL))
        self.assertIsNone(await probe_ocr(http.client, "http://omlx:8000/v1/", MODEL, "key"))
        self.assertEqual(http.urls(), ["http://omlx:8000/v1/models"])
        self.assertEqual(http.requests[0].headers["Authorization"], "Bearer key")

        cases = {
            "model x isn't loaded": lambda req: models_reply("other"),
            "HTTP 500 from /models": lambda req: httpx.Response(500, text="boom"),
            "unreadable /models reply": lambda req: httpx.Response(200, text="<html>"),
        }
        for expected, handler in cases.items():
            reason = await probe_ocr(FakeHttp(handler).client, "http://omlx/v1", "x")
            self.assertEqual(reason, expected)

        def refuse(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=req)

        self.assertEqual(
            await probe_ocr(FakeHttp(refuse).client, "http://omlx/v1", MODEL),
            "unreachable (ConnectError)",
        )

    def test_short_reason_never_includes_reply_body(self) -> None:
        exc = VisionOCRError("Vision OCR HTTP 404 from http://x/v1 (model=m): PlayerOne 12345")
        self.assertEqual(short_reason(exc), "HTTP 404")
        try:
            try:
                raise OSError("Connection refused")
            except OSError as cause:
                raise VisionTransientError("Vision OCR request failed contacting x") from cause
        except VisionTransientError as exc2:
            self.assertEqual(short_reason(exc2), "VisionTransientError (OSError)")

    def test_pipeline_reports_ocr_calls(self) -> None:
        ocr = OcrHealth(clock=self.clock)
        set_ocr_tracker(ocr)
        self.addCleanup(set_ocr_tracker, None)
        config = VisionOCR(base_url="http://x/v1", model=MODEL)
        target = "bot.ocr.vision.extract_metrics_via_vision"

        with patch(target, side_effect=VisionOCRError("Vision OCR HTTP 503 from x: Alice 99")):
            for _ in range(3):
                with self.assertRaises(VisionOCRError):
                    extract_metrics_from_image("img.png", kind="versus", ocr=config)
        snap = ocr.snapshot()
        self.assertTrue(snap.down)
        self.assertEqual(snap.last_error, "HTTP 503")

        # An unreadable reply means the server answered: not an outage.
        with patch(target, side_effect=VisionReplyError("bad json")):
            with self.assertRaises(VisionReplyError):
                extract_metrics_from_image("img.png", kind="versus", ocr=config)
        self.assertEqual(ocr.snapshot().consecutive_failures, 0)

        with patch(target, side_effect=VisionOCRError("HTTP 503")):
            with self.assertRaises(VisionOCRError):
                extract_metrics_from_image("img.png", kind="versus", ocr=config)
        with patch(target, return_value="result"):
            self.assertEqual(
                extract_metrics_from_image("img.png", kind="versus", ocr=config), "result"
            )
        self.assertEqual(ocr.snapshot().consecutive_failures, 0)


class HeartbeatTests(_Async):
    async def test_off_without_url(self) -> None:
        http = FakeHttp()
        state = HealthState(clock=self.clock)
        self.assertFalse(state.heartbeat.enabled)
        await state.tick(http.client, problem=None, probe=AsyncMock(return_value=None))
        self.assertEqual(http.requests, [])

    async def test_pings_every_interval_while_healthy(self) -> None:
        http = FakeHttp()
        state = HealthState(clock=self.clock, heartbeat_url=HEARTBEAT_URL)
        probe = AsyncMock(return_value=None)
        for _ in range(11):  # one tick a minute for 10 minutes
            await state.tick(http.client, problem=None, probe=probe)
            self.clock.advance(minutes=1)
        self.assertEqual(http.urls(), [HEARTBEAT_URL] * 3)  # at 0, 5 and 10 minutes
        self.assertEqual(state.heartbeat.last_ping_at, T0 + 2 * HEARTBEAT_INTERVAL)
        # The OCR probe runs every 2 minutes: 0, 2, 4, 6, 8, 10.
        self.assertEqual(probe.await_count, 6)
        self.assertEqual(OCR_PROBE_INTERVAL, timedelta(minutes=2))
        self.assertTrue(health_mod.HEALTH_FILE.exists())

    async def test_no_ping_while_unhealthy(self) -> None:
        http = FakeHttp()
        state = HealthState(clock=self.clock, heartbeat_url=HEARTBEAT_URL)
        await state.tick(
            http.client, problem="not connected to Discord", probe=AsyncMock(return_value=None)
        )
        self.assertEqual(http.requests, [])
        self.assertEqual(state.heartbeat.last_skipped, "not connected to Discord")
        self.assertFalse(health_mod.HEALTH_FILE.exists())  # Docker sees it unhealthy

    async def test_failed_ping_hides_the_url(self) -> None:
        def refuse(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"can't reach {req.url}", request=req)

        hb = Heartbeat(HEARTBEAT_URL, clock=self.clock)
        with self.assertLogs("bot.utils.health", "WARNING"):
            self.assertFalse(await hb.beat(FakeHttp(refuse).client, None))
        self.assertEqual(hb.last_error, "ConnectError")
        with self.assertLogs("bot.utils.health", "WARNING"):
            await hb.beat(FakeHttp(lambda r: httpx.Response(404)).client, None)
        self.assertEqual(hb.last_error, "HTTP 404")
        self.assertTrue(await hb.beat(FakeHttp().client, None))
        self.assertIsNone(hb.last_error)


class ConnectionTests(_Async):
    async def test_reconnect_after_long_disconnect(self) -> None:
        watch = ConnectionWatch(clock=self.clock)
        self.assertIsNone(watch.reconnected())  # first connect, nothing known
        self.clock.advance(minutes=3)
        watch.disconnected()
        self.clock.advance(minutes=2)
        self.assertIsNone(watch.reconnected())  # a short blip isn't reported
        watch.disconnected()
        self.clock.advance(minutes=45)
        self.assertEqual(watch.reconnected(), timedelta(minutes=45))

    async def test_restart_after_downtime_is_reported(self) -> None:
        watch = ConnectionWatch(clock=self.clock, last_alive=T0 - timedelta(hours=2))
        self.assertEqual(watch.reconnected(), timedelta(hours=2))

    async def test_suspended_process_is_noticed_by_the_tick(self) -> None:
        state = HealthState(clock=self.clock)
        state.alerts.send = self.send
        state.connection.reconnected()
        probe = AsyncMock(return_value=None)
        http = FakeHttp()
        await state.tick(http.client, problem=None, probe=probe)
        self.clock.advance(hours=3)  # Mac asleep; the gateway never noticed
        await state.tick(http.client, problem=None, probe=probe)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("back online after about 3h 00m", self.sent[0])


class OffsiteBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.local = root / "backups"
        self.offsite = root / "icloud"
        self.local.mkdir()
        self.offsite.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _daily(self, folder: Path, at: datetime, body: bytes = b"db") -> Path:
        path = folder / f"weekly-{at:%Y%m%d-%H%M%S}-daily.db"
        path.write_bytes(body)
        return path

    def test_copies_newest_daily_once(self) -> None:
        self._daily(self.local, T0 - timedelta(days=1), b"old")
        newest = self._daily(self.local, T0, b"new")
        (self.local / "weekly-20261006-130000-manual.db").write_bytes(b"manual")
        kw = dict(keep=14, max_age=timedelta(days=30), now=T0)
        copied = sync_offsite(self.local, self.offsite, **kw)
        self.assertEqual(copied, self.offsite / newest.name)
        self.assertEqual(copied.read_bytes(), b"new")
        self.assertIsNone(sync_offsite(self.local, self.offsite, **kw))  # already there
        # Only the daily copy, and no temp files for a sync client to pick up.
        self.assertEqual(sorted(p.name for p in self.offsite.iterdir()), [newest.name])

    def test_same_retention_offsite(self) -> None:
        for days in range(40, 0, -1):  # older copies already offsite
            self._daily(self.offsite, T0 - timedelta(days=days))
        newest = self._daily(self.local, T0)
        sync_offsite(self.local, self.offsite, keep=14, max_age=timedelta(days=30), now=T0)
        kept = list_backups(self.offsite, "daily")
        self.assertEqual(len(kept), 14)
        self.assertEqual(kept[-1].name, newest.name)

        # Age limit: with a bigger keep, nothing older than 30 days survives.
        for days in range(40, 14, -1):
            self._daily(self.offsite, T0 - timedelta(days=days))
        sync_offsite(self.local, self.offsite, keep=100, max_age=timedelta(days=30), now=T0)
        oldest = list_backups(self.offsite, "daily")[0]
        self.assertEqual(oldest.name, f"weekly-{T0 - timedelta(days=30):%Y%m%d-%H%M%S}-daily.db")

    def test_missing_folder_is_an_error_not_created(self) -> None:
        self._daily(self.local, T0)
        missing = self.offsite / "not-mounted"
        with self.assertRaises(FileNotFoundError):
            sync_offsite(self.local, missing, keep=14, max_age=timedelta(days=30), now=T0)
        self.assertFalse(missing.exists())


class DailyBackupTaskTests(_Async):
    """The hourly task alerts on backup and offsite failures and on recovery."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        root = Path(self._tmp.name)
        self.backup_dir = root / "backups"
        self.offsite = root / "offsite"
        self.state = HealthState(clock=self.clock)
        self.state.alerts.send = self.send
        db = SimpleNamespace(vacuum_into=AsyncMock(side_effect=self._vacuum))
        settings = SimpleNamespace(
            backup_dir=self.backup_dir,
            backup_keep=14,
            backup_max_age_days=30,
            offsite_backup_dir=self.offsite,
        )
        from bot.cogs.admin import Admin

        self.cog = Admin(SimpleNamespace(settings=settings, db=db, health=self.state))  # type: ignore[arg-type]
        self.fail_backup = False

    async def _vacuum(self, path: Path) -> None:
        if self.fail_backup:
            raise OSError(28, "No space left on device")
        path.write_bytes(b"db")

    async def run_task(self, *, errors: bool) -> None:
        with self.assertLogs("bot.cogs.admin", "ERROR") if errors else nullcontext():
            await self.cog.daily_backup.coro(self.cog)

    async def test_offsite_failure_alerts_then_recovers(self) -> None:
        await self.run_task(errors=True)  # offsite folder isn't mounted yet
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Offsite backup copy failed: FileNotFoundError", self.sent[0])
        self.assertIsNotNone(self.state.backup.offsite_error)

        self.offsite.mkdir()
        self.clock.advance(hours=1)
        await self.run_task(errors=False)  # retried next hour: the copy is made
        self.assertEqual(len(list_backups(self.offsite, "daily")), 1)
        self.assertEqual(self.sent[1], "✅ Offsite backup copies are working again. (down 1h 00m)")
        self.assertIsNone(self.state.backup.offsite_error)

    async def test_backup_failure_alerts_once(self) -> None:
        self.offsite.mkdir()
        self.fail_backup = True
        await self.run_task(errors=True)
        await self.run_task(errors=True)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Daily backup failed: OSError", self.sent[0])
        self.fail_backup = False
        await self.run_task(errors=False)
        self.assertTrue(self.sent[1].startswith("✅ Daily backups are working again."))


class HealthReportTests(_Async):
    def report(self, state: HealthState, **overrides) -> str:
        kwargs = dict(
            now=self.clock(),
            latency=0.085,
            backups_enabled=True,
            last_backup=T0 - timedelta(hours=3),
            offsite_enabled=False,
            last_offsite=None,
        )
        kwargs.update(overrides)
        return format_health_report(state, **kwargs)

    async def test_healthy_report(self) -> None:
        state = HealthState(clock=self.clock)
        state.connection.reconnected()
        state.ocr.record(True, probe=True)
        self.clock.advance(days=2, hours=3)
        text = self.report(state, now=self.clock(), last_backup=self.clock() - timedelta(hours=3))
        self.assertIn("Uptime: **2d 3h** (since 2026-10-06 12:00 UTC)", text)
        self.assertIn("Discord: connected, latency **85 ms**", text)
        self.assertIn("OCR server: ✅ OK; last check 2026-10-06 12:00 UTC (2d 3h ago)", text)
        self.assertIn("Last daily backup: 2026-10-08 12:00 UTC (3h 00m ago)", text)
        self.assertIn("Offsite copy: off (`OFFSITE_BACKUP_DIR` not set)", text)
        self.assertIn("Heartbeat: off (`HEARTBEAT_URL` not set)", text)
        self.assertIn("Open incidents: none", text)

    async def test_problems_and_incidents(self) -> None:
        state = HealthState(clock=self.clock, heartbeat_url=HEARTBEAT_URL)
        state.alerts.send = self.send
        for _ in range(3):
            state.ocr.record(False, "HTTP 503", probe=True)
        await update_ocr_incident(state.alerts, state.ocr)
        await state.offsite_failed(FileNotFoundError("offsite folder /x doesn't exist"))
        state.heartbeat.last_error = "ConnectError"
        self.clock.advance(minutes=10)
        text = self.report(
            state,
            now=self.clock(),
            latency=float("inf"),
            offsite_enabled=True,
            last_offsite=None,
        )
        self.assertIn("Discord: **not connected**", text)
        self.assertIn("OCR server: ❌ **failing** (3 in a row)", text)
        self.assertIn("Last error: HTTP 503, 2026-10-06 12:00 UTC (10m ago)", text)
        self.assertIn("Offsite copy: never", text)
        self.assertIn("❌ Last attempt failed: FileNotFoundError", text)
        self.assertIn("Heartbeat: last ping never; ❌ last ping failed: ConnectError", text)
        self.assertIn("• OCR server since 2026-10-06 12:00 UTC (10m, alerted)", text)
        self.assertIn("• Offsite backup copy since", text)
        self.assertNotIn("hc-ping", text)  # the heartbeat URL is a secret

    async def test_backups_off(self) -> None:
        text = self.report(HealthState(clock=self.clock), backups_enabled=False, last_backup=None)
        self.assertIn("Daily backup: off (no backup folder mounted)", text)


class OpsHealthCommandTests(_Async):
    async def _invoke(self, user_id: int):
        from bot.cogs.ops import NOT_OPERATOR_MESSAGE, Ops

        state = HealthState(clock=self.clock)
        settings = SimpleNamespace(
            bot_owner_ids=frozenset({42}),
            control_guild_id=1,
            backup_dir=Path(self._tmp.name),
            offsite_backup_dir=None,
        )
        bot = SimpleNamespace(settings=settings, health=state, latency=0.05, backups_enabled=True)
        cog = Ops(bot)  # type: ignore[arg-type]
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=user_id),
            guild_id=1,
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        await cog.health.callback(cog, interaction)
        return interaction, NOT_OPERATOR_MESSAGE

    async def test_operator_gets_report(self) -> None:
        interaction, _ = await self._invoke(42)
        text = interaction.followup.send.await_args.args[0]
        self.assertTrue(text.startswith("**Bot health**"))
        self.assertIn("Last daily backup: never", text)

    async def test_handler_rechecks_operator(self) -> None:
        interaction, refusal = await self._invoke(7)
        interaction.response.send_message.assert_awaited_once_with(refusal, ephemeral=True)
        interaction.followup.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
