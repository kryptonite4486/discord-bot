"""Per-server data export as CSV or JSON (/data export, /ops export).

Exports hold the server's WeeklyMetrics rows: player, metric, value, week,
channel and update time. Trivia scores, server settings, entitlements and
usage counters are left out; see docs/monetization-plan.md §2 item 11.

A file over the server's Discord upload limit is zipped. If even the zip
is too big, nothing is sent and the caller asks for a narrower export.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone

import discord

from bot.utils.guild import channel_display_name

# Discord's upload limit for servers without boosts; Guild.filesize_limit
# gives the real one when the guild is known.
DEFAULT_UPLOAD_LIMIT = 10 * 1024 * 1024
FORMATS = ("csv", "json")
COLUMNS = ("player", "metric", "value", "week", "channel_name", "channel_id", "updated_at")


@dataclass(frozen=True)
class ExportFile:
    filename: str
    data: bytes
    rows: int
    zipped: bool = False


def _value(v: float) -> int | float:
    """Whole numbers without a trailing .0 (game stats are almost always whole)."""
    return int(v) if float(v).is_integer() else v


def export_records(rows: list[dict], guild: discord.Guild | None) -> list[dict]:
    """WeeklyMetrics rows as export records, with channel names resolved."""
    names: dict[str, str] = {}
    records = []
    for r in rows:
        cid = str(r["ChannelId"] or "")
        if cid not in names:
            names[cid] = channel_display_name(guild, cid)
        records.append({
            "player": r["PlayerName"],
            "metric": r["MetricType"],
            "value": _value(r["Value"]),
            "week": r["WeekStart"],
            "channel_name": names[cid],
            "channel_id": cid,
            "updated_at": r["UpdatedAt"],
        })
    return records


def render_csv(records: list[dict]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(records)
    # BOM so Excel opens player names with non-Latin characters correctly.
    return buf.getvalue().encode("utf-8-sig")


def render_json(records: list[dict], meta: dict) -> bytes:
    return json.dumps({**meta, "rows": records}, ensure_ascii=False, indent=1).encode("utf-8")


def export_filename(guild_id: str, scope: str, fmt: str, from_week: str | None, to_week: str | None) -> str:
    """e.g. lastz-123-server-2026-01-04_to_2026-10-04.csv"""
    weeks = f"-{from_week or 'start'}_to_{to_week or 'latest'}" if from_week or to_week else ""
    return f"lastz-{guild_id}-{scope}{weeks}.{fmt}"


def fit_upload(name: str, data: bytes, limit: int) -> tuple[str, bytes, bool] | None:
    """(filename, bytes, zipped) that fits ``limit``; None if even a zip doesn't."""
    if len(data) <= limit:
        return name, data, False
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr(name, data)
    zipped = buf.getvalue()
    if len(zipped) > limit:
        return None
    return f"{name}.zip", zipped, True


def upload_limit(guild: discord.Guild | None) -> int:
    return getattr(guild, "filesize_limit", None) or DEFAULT_UPLOAD_LIMIT


async def build_export(
    db,
    guild_id: str,
    guild: discord.Guild | None,
    *,
    fmt: str,
    channel_id: str | None = None,
    from_week: str | None = None,
    to_week: str | None = None,
    limit: int | None = None,
    now: datetime | None = None,
) -> ExportFile | None:
    """Build the export file; None if it's too big to upload even zipped.

    ``channel_id`` None exports the whole server, including unassigned rows.
    ``from_week``/``to_week`` (ISO Sundays, inclusive) are applied as given;
    callers apply the tier history window by raising ``from_week``.
    """
    if fmt not in FORMATS:
        raise ValueError(f"Unknown export format {fmt!r}")
    rows = await db.export_metrics(
        guild_id, channel_id=channel_id, from_week=from_week, to_week=to_week
    )
    records = export_records(rows, guild)
    scope = "server" if channel_id is None else "channel"
    if fmt == "csv":
        data = render_csv(records)
    else:
        meta = {
            "guild_id": guild_id,
            "scope": scope,
            "channel_id": channel_id,
            "from_week": from_week,
            "to_week": to_week,
            "exported_at": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S"),
            "row_count": len(records),
        }
        data = render_json(records, meta)
    label = scope if channel_id is None else f"channel-{channel_id}"
    name = export_filename(guild_id, label, fmt, from_week, to_week)
    fitted = fit_upload(name, data, limit if limit is not None else upload_limit(guild))
    if fitted is None:
        return None
    name, data, zipped = fitted
    return ExportFile(name, data, len(records), zipped)
