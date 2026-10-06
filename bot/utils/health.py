"""Hosting health: OCR server status, operator alerts and the uptime heartbeat.

The bot runs on a home Mac, so three things are watched (see
docs/monetization-plan.md §2 item 12):

* An optional heartbeat (``HEARTBEAT_URL``, e.g. healthchecks.io) is pinged
  only while the bot is connected to Discord and its database answers. The
  service alerts the operator when the pings stop: Mac asleep or offline, or
  the bot crashed. The same check touches ``HEALTH_FILE`` for Docker's
  healthcheck.
* The OCR server: a cheap ``GET /models`` probe plus the outcome of real OCR
  calls. Several failures in a row open an incident.
* Backups and reconnects, reported by the backup task and the gateway events.

Alerts are DMs to BOT_OWNER_IDS through :class:`AlertManager`: one alert per
incident, one "recovered" message, and a cooldown per kind so a flapping
server can't spam. Alert text names the failing part and a short reason
(exception type or HTTP status), never screenshots, replies or player data.

Everything takes an injectable clock so tests can drive time.
"""

from __future__ import annotations

import logging
import math
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

import httpx

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
Sender = Callable[[str], Awaitable[None]]

# Minimum time between two messages (alert or recovery) of the same kind. A
# server that recovers and fails again within it gets a silent incident; if
# it's still down when the cooldown ends, the alert goes out then.
ALERT_COOLDOWN = timedelta(minutes=30)
# Failed probes or OCR calls in a row (a success resets the count) before
# the OCR server counts as down.
OCR_FAILURE_THRESHOLD = 3
OCR_PROBE_INTERVAL = timedelta(minutes=2)
HEARTBEAT_INTERVAL = timedelta(minutes=5)
# Offline at least this long (disconnected, Mac asleep, or restarted) before
# coming back is worth a DM.
RECONNECT_ALERT_AFTER = timedelta(minutes=10)
HTTP_TIMEOUT = 15.0
# Touched every minute while healthy; docker-compose.yml's healthcheck reads
# its age, and its age at startup tells how long the bot was down.
HEALTH_FILE = Path(tempfile.gettempdir()) / "lastz-assistant.healthy"

KEY_OCR = "ocr"
KEY_BACKUP = "backup"
KEY_OFFSITE = "offsite"
KEY_RECONNECT = "reconnect"
INCIDENT_LABELS = {
    KEY_OCR: "OCR server",
    KEY_BACKUP: "Daily backup",
    KEY_OFFSITE: "Offsite backup copy",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def fmt_duration(delta: timedelta) -> str:
    """Coarse duration: 45s, 12m, 3h 05m, 2d 4h."""
    secs = max(0, int(delta.total_seconds()))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h {secs % 3600 // 60:02d}m"
    return f"{secs // 86400}d {secs % 86400 // 3600}h"


def fmt_when(at: datetime | None, now: datetime) -> str:
    if at is None:
        return "never"
    return f"{at:%Y-%m-%d %H:%M} UTC ({fmt_duration(now - at)} ago)"


_HTTP_STATUS = re.compile(r"HTTP (\d{3})")


def short_reason(exc: BaseException) -> str:
    """A safe one-line reason: HTTP status or exception type, never a reply body."""
    match = _HTTP_STATUS.search(str(exc))
    if match:
        return f"HTTP {match.group(1)}"
    cause = exc.__cause__
    if cause is not None:
        return f"{type(exc).__name__} ({type(cause).__name__})"
    return type(exc).__name__


# --- Alerts ------------------------------------------------------------------


@dataclass
class Incident:
    key: str
    detail: str
    opened_at: datetime
    alerted: bool = False


class AlertManager:
    """De-duplicated operator alerts with a recovery message and a cooldown."""

    def __init__(
        self,
        send: Sender | None = None,
        *,
        clock: Clock = utc_now,
        cooldown: timedelta = ALERT_COOLDOWN,
    ) -> None:
        self.send = send
        self.clock = clock
        self.cooldown = cooldown
        self.incidents: dict[str, Incident] = {}
        self._last_alert: dict[str, datetime] = {}

    def _cooling(self, key: str, now: datetime) -> bool:
        last = self._last_alert.get(key)
        return last is not None and now - last < self.cooldown

    async def _deliver(self, key: str, text: str, now: datetime) -> None:
        self._last_alert[key] = now
        if self.send is None:
            log.warning("Alert (no operator to DM): %s", text)
            return
        try:
            await self.send(text)
        except Exception:
            # Not retried: the incident still shows in /ops health.
            log.exception("Couldn't deliver operator alert %r", key)

    async def problem(self, key: str, detail: str) -> bool:
        """Report that ``key`` is failing; return whether an alert was sent.

        Call it every time the check fails: the first call opens the incident
        and alerts, later calls only refresh ``detail``. An incident opened
        during the cooldown alerts once the cooldown ends, if still open.
        """
        now = self.clock()
        incident = self.incidents.get(key)
        if incident is None:
            incident = self.incidents[key] = Incident(key, detail, now)
            log.warning("Incident opened (%s): %s", key, detail)
        else:
            incident.detail = detail
        if incident.alerted or self._cooling(key, now):
            return False
        incident.alerted = True
        await self._deliver(key, f"⚠️ {detail}", now)
        return True

    async def resolved(self, key: str, text: str) -> bool:
        """Close ``key``'s incident; send ``text`` only if its alert went out."""
        incident = self.incidents.pop(key, None)
        if incident is None:
            return False
        now = self.clock()
        log.info("Incident resolved (%s) after %s", key, fmt_duration(now - incident.opened_at))
        if not incident.alerted:
            return False
        await self._deliver(
            key, f"✅ {text} (down {fmt_duration(now - incident.opened_at)})", now
        )
        return True

    async def notice(self, key: str, text: str) -> bool:
        """A one-off message (no incident), at most once per cooldown."""
        now = self.clock()
        if self._cooling(key, now):
            return False
        await self._deliver(key, text, now)
        return True


# --- OCR server --------------------------------------------------------------


@dataclass(frozen=True)
class OcrSnapshot:
    consecutive_failures: int
    down: bool
    last_ok_at: datetime | None
    last_error: str | None
    last_error_at: datetime | None
    last_probe_at: datetime | None


class OcrHealth:
    """Counts OCR failures in a row. Thread-safe: OCR runs in worker threads."""

    def __init__(self, *, clock: Clock = utc_now, threshold: int = OCR_FAILURE_THRESHOLD) -> None:
        self.clock = clock
        self.threshold = threshold
        self._lock = threading.Lock()
        self._failures = 0
        self._last_ok_at: datetime | None = None
        self._last_error: str | None = None
        self._last_error_at: datetime | None = None
        self._last_probe_at: datetime | None = None

    def record(self, ok: bool, reason: str | None = None, *, probe: bool = False) -> None:
        now = self.clock()
        with self._lock:
            if probe:
                self._last_probe_at = now
            if ok:
                self._failures = 0
                self._last_ok_at = now
            else:
                self._failures += 1
                self._last_error = reason or "unknown error"
                self._last_error_at = now

    def snapshot(self) -> OcrSnapshot:
        with self._lock:
            return OcrSnapshot(
                consecutive_failures=self._failures,
                down=self._failures >= self.threshold,
                last_ok_at=self._last_ok_at,
                last_error=self._last_error,
                last_error_at=self._last_error_at,
                last_probe_at=self._last_probe_at,
            )


# The bot's tracker; bot.ocr.pipeline reports each OCR call here. A module
# global because OCR runs in worker threads with no reference to the bot.
_ocr_tracker: OcrHealth | None = None


def set_ocr_tracker(tracker: OcrHealth | None) -> None:
    global _ocr_tracker
    _ocr_tracker = tracker


def record_ocr_call(ok: bool, reason: str | None = None) -> None:
    tracker = _ocr_tracker
    if tracker is not None:
        tracker.record(ok, reason)


async def probe_ocr(
    client: httpx.AsyncClient, base_url: str, model: str, api_key: str = ""
) -> str | None:
    """``GET {base_url}/models``: None if ``model`` is listed, else a short reason."""
    url = base_url.rstrip("/") + "/models"
    try:
        resp = await client.get(
            url, headers={"Authorization": f"Bearer {api_key}"}, timeout=HTTP_TIMEOUT
        )
    except httpx.HTTPError as exc:
        return f"unreachable ({type(exc).__name__})"
    if resp.status_code >= 400:
        return f"HTTP {resp.status_code} from /models"
    try:
        ids = {m.get("id") for m in resp.json().get("data", []) if isinstance(m, dict)}
    except (ValueError, AttributeError):
        return "unreadable /models reply"
    if model not in ids:
        return f"model {model} isn't loaded"
    return None


async def update_ocr_incident(alerts: AlertManager, ocr: OcrHealth) -> None:
    snap = ocr.snapshot()
    if snap.down:
        await alerts.problem(
            KEY_OCR,
            f"OCR server is failing: {snap.consecutive_failures} failed checks or "
            f"OCR calls in a row, last error: {snap.last_error}. "
            "Screenshot uploads will fail until it's back.",
        )
    else:
        await alerts.resolved(KEY_OCR, "OCR server is working again.")


# --- Heartbeat and connection ------------------------------------------------


class Heartbeat:
    """Pings an external dead-man's switch while the bot is healthy."""

    def __init__(self, url: str | None, *, clock: Clock = utc_now) -> None:
        self.url = url
        self.clock = clock
        self.last_ping_at: datetime | None = None
        self.last_error: str | None = None
        self.last_skipped: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    async def beat(self, client: httpx.AsyncClient, problem: str | None) -> bool:
        """Ping unless ``problem`` says the bot is unhealthy; return whether it pinged."""
        if not self.url:
            return False
        if problem is not None:
            # Staying silent is the alert: the service notices the missing ping.
            self.last_skipped = problem
            return False
        self.last_skipped = None
        try:
            resp = await client.get(self.url, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            self.last_error = f"HTTP {exc.response.status_code}"
        except httpx.HTTPError as exc:
            # Not str(exc): it can contain the URL, whose path is the secret.
            self.last_error = type(exc).__name__
        else:
            self.last_ping_at = self.clock()
            self.last_error = None
            return True
        log.warning("Heartbeat ping failed: %s", self.last_error)
        return False


class ConnectionWatch:
    """Notices how long the bot was away: disconnected, suspended, or restarted."""

    def __init__(self, *, clock: Clock = utc_now, last_alive: datetime | None = None) -> None:
        self.clock = clock
        # Last moment the bot was known to be connected and running.
        self.last_alive = last_alive
        self.connected = False

    def _gap(self, now: datetime) -> timedelta | None:
        if self.last_alive is None:
            return None
        gap = now - self.last_alive
        return gap if gap >= RECONNECT_ALERT_AFTER else None

    def alive(self) -> timedelta | None:
        """Mark a healthy tick. A long gap since the last one means the process
        was suspended (Mac asleep) without dropping the connection."""
        if not self.connected:
            return None
        now = self.clock()
        gap = self._gap(now)
        self.last_alive = now
        return gap

    def disconnected(self) -> None:
        if self.connected:
            self.last_alive = self.clock()
        self.connected = False

    def reconnected(self) -> timedelta | None:
        """Mark connected; return the time away if long enough to report."""
        now = self.clock()
        gap = self._gap(now)
        self.connected = True
        self.last_alive = now
        return gap


def last_healthy_at(path: Path | None = None) -> datetime | None:
    path = path or HEALTH_FILE
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError:
        return None


def touch_health_file(path: Path | None = None) -> None:
    path = path or HEALTH_FILE
    try:
        path.touch()
    except OSError:
        log.warning("Couldn't touch health file %s", path, exc_info=True)


# --- Everything together -----------------------------------------------------


@dataclass
class BackupStatus:
    last_error: str | None = None
    last_error_at: datetime | None = None
    offsite_error: str | None = None
    offsite_error_at: datetime | None = None


class HealthState:
    """The bot's health, kept on the bot so cog reloads don't reset it."""

    def __init__(
        self,
        *,
        clock: Clock = utc_now,
        heartbeat_url: str | None = None,
        last_alive: datetime | None = None,
    ) -> None:
        self.clock = clock
        self.started_at = clock()
        self.alerts = AlertManager(clock=clock)
        self.ocr = OcrHealth(clock=clock)
        self.heartbeat = Heartbeat(heartbeat_url, clock=clock)
        self.connection = ConnectionWatch(clock=clock, last_alive=last_alive)
        self.backup = BackupStatus()
        self._next_probe: datetime | None = None
        self._next_beat: datetime | None = None

    async def tick(
        self,
        client: httpx.AsyncClient,
        *,
        problem: str | None,
        probe: Callable[[], Awaitable[str | None]],
    ) -> None:
        """One monitor pass (every minute). ``problem`` is None when the bot is
        connected and its database answers; ``probe`` checks the OCR server."""
        now = self.clock()
        if problem is None:
            touch_health_file()
            gap = self.connection.alive()
            if gap is not None:
                await self.report_back_online(gap)
        if self._next_probe is None or now >= self._next_probe:
            self._next_probe = now + OCR_PROBE_INTERVAL
            reason = await probe()
            self.ocr.record(reason is None, reason, probe=True)
        await update_ocr_incident(self.alerts, self.ocr)
        if self.heartbeat.enabled and (self._next_beat is None or now >= self._next_beat):
            self._next_beat = now + HEARTBEAT_INTERVAL
            await self.heartbeat.beat(client, problem)

    async def report_back_online(self, gap: timedelta) -> None:
        await self.alerts.notice(
            KEY_RECONNECT,
            f"🔌 LastZ Assistant is back online after about {fmt_duration(gap)} away "
            "(disconnected, Mac asleep, or restarted).",
        )

    async def backup_failed(self, exc: BaseException) -> None:
        reason = f"{type(exc).__name__}: {exc}"[:200]
        self.backup.last_error, self.backup.last_error_at = reason, self.clock()
        await self.alerts.problem(KEY_BACKUP, f"Daily backup failed: {reason}")

    async def backup_ok(self) -> None:
        self.backup.last_error = None
        await self.alerts.resolved(KEY_BACKUP, "Daily backups are working again.")

    async def offsite_failed(self, exc: BaseException) -> None:
        reason = f"{type(exc).__name__}: {exc}"[:200]
        self.backup.offsite_error, self.backup.offsite_error_at = reason, self.clock()
        await self.alerts.problem(KEY_OFFSITE, f"Offsite backup copy failed: {reason}")

    async def offsite_ok(self) -> None:
        self.backup.offsite_error = None
        await self.alerts.resolved(KEY_OFFSITE, "Offsite backup copies are working again.")


def format_health_report(
    state: HealthState,
    *,
    now: datetime,
    latency: float | None,
    backups_enabled: bool,
    last_backup: datetime | None,
    offsite_enabled: bool,
    last_offsite: datetime | None,
) -> str:
    """Markdown for /ops health. Shows no URLs, keys or player data."""
    lines = [
        "**Bot health**",
        f"Uptime: **{fmt_duration(now - state.started_at)}** "
        f"(since {state.started_at:%Y-%m-%d %H:%M} UTC)",
    ]
    if latency is None or not math.isfinite(latency):
        lines.append("Discord: **not connected**")
    else:
        conn = "connected" if state.connection.connected else "**disconnected**"
        lines.append(f"Discord: {conn}, latency **{latency * 1000:.0f} ms**")

    ocr = state.ocr.snapshot()
    if ocr.down:
        status = f"❌ **failing** ({ocr.consecutive_failures} in a row)"
    elif ocr.consecutive_failures:
        status = f"⚠️ {ocr.consecutive_failures} recent failure(s)"
    elif ocr.last_ok_at is not None:
        status = "✅ OK"
    else:
        status = "not checked yet"
    lines.append(f"OCR server: {status}; last check {fmt_when(ocr.last_probe_at, now)}")
    if ocr.last_error:
        lines.append(f"  Last error: {ocr.last_error}, {fmt_when(ocr.last_error_at, now)}")

    if backups_enabled:
        lines.append(f"Last daily backup: {fmt_when(last_backup, now)}")
        if state.backup.last_error:
            lines.append(
                f"  ❌ Last attempt failed: {state.backup.last_error}, "
                f"{fmt_when(state.backup.last_error_at, now)}"
            )
    else:
        lines.append("Daily backup: off (no backup folder mounted)")
    if offsite_enabled:
        lines.append(f"Offsite copy: {fmt_when(last_offsite, now)}")
        if state.backup.offsite_error:
            lines.append(
                f"  ❌ Last attempt failed: {state.backup.offsite_error}, "
                f"{fmt_when(state.backup.offsite_error_at, now)}"
            )
    else:
        lines.append("Offsite copy: off (`OFFSITE_BACKUP_DIR` not set)")

    hb = state.heartbeat
    if not hb.enabled:
        lines.append("Heartbeat: off (`HEARTBEAT_URL` not set)")
    else:
        line = f"Heartbeat: last ping {fmt_when(hb.last_ping_at, now)}"
        if hb.last_error:
            line += f"; ❌ last ping failed: {hb.last_error}"
        if hb.last_skipped:
            line += f"; skipped: {hb.last_skipped}"
        lines.append(line)

    if state.alerts.incidents:
        lines.append("**Open incidents:**")
        for inc in sorted(state.alerts.incidents.values(), key=lambda i: i.opened_at):
            label = INCIDENT_LABELS.get(inc.key, inc.key)
            alerted = "alerted" if inc.alerted else "alert held by cooldown"
            lines.append(
                f"• {label} since {inc.opened_at:%Y-%m-%d %H:%M} UTC "
                f"({fmt_duration(now - inc.opened_at)}, {alerted})"
            )
    else:
        lines.append("Open incidents: none")
    return "\n".join(lines)[:1900]
