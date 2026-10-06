"""SQLite backups that are safe to take while the bot is running.

Backups use ``VACUUM INTO``, which writes a consistent, compacted copy through
SQLite itself (a plain file copy of a WAL-mode database can miss recent writes
or be torn mid-write). Each backup is written to a temporary name and renamed
into place, so sync tools such as iCloud never pick up a partial file.

An optional second folder (``OFFSITE_BACKUP_DIR``: a mounted iCloud Drive,
Dropbox or NAS folder) gets a copy of each daily backup and the same
retention, so deleted data leaves the offsite copies on the same schedule.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from bot.db import Database

log = logging.getLogger(__name__)

BackupKind = Literal["daily", "manual"]

# Manual copies (/ops backup and the safety copies taken before renames and
# deletions) are counted separately so on-demand backups never push scheduled
# ones out of the retention window. Every kind is also limited by age
# (BACKUP_MAX_AGE_DAYS, see prune_older_than).
MANUAL_KEEP = 10

_NAME_RE = re.compile(r"^weekly-(\d{8}-\d{6})-(daily|manual)\.db$")


def backup_name(kind: BackupKind, now: datetime) -> str:
    return f"weekly-{now:%Y%m%d-%H%M%S}-{kind}.db"


def list_backups(backup_dir: Path, kind: BackupKind) -> list[Path]:
    """Backups of ``kind``, oldest first (names sort chronologically)."""
    if not backup_dir.is_dir():
        return []
    found = [
        p
        for p in backup_dir.iterdir()
        if (m := _NAME_RE.match(p.name)) and m.group(2) == kind
    ]
    return sorted(found, key=lambda p: p.name)


def prune(backup_dir: Path, kind: BackupKind, keep: int) -> list[Path]:
    """Delete all but the newest ``keep`` backups of ``kind``; return removed paths."""
    backups = list_backups(backup_dir, kind)
    stale = backups[:-keep] if keep > 0 else backups
    for path in stale:
        path.unlink(missing_ok=True)
        log.info("Pruned old backup %s", path.name)
    return stale


def _taken_at(path: Path) -> datetime:
    m = _NAME_RE.match(path.name)
    assert m is not None
    return datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)


def prune_older_than(
    backup_dir: Path, max_age: timedelta, *, now: datetime | None = None
) -> list[Path]:
    """Delete backups of every kind older than ``max_age``; return removed paths.

    The newest backup is always kept, even if it's too old, so that a stretch
    without new backups (bot offline, disk full) never leaves no backup at all.
    """
    now = now or datetime.now(timezone.utc)
    backups = sorted(
        (p for kind in ("daily", "manual") for p in list_backups(backup_dir, kind)),
        key=_taken_at,
    )
    stale = [p for p in backups[:-1] if now - _taken_at(p) > max_age]
    for path in stale:
        path.unlink(missing_ok=True)
        log.info("Pruned backup older than %d day(s): %s", max_age.days, path.name)
    return stale


def last_backup_time(backup_dir: Path, kind: BackupKind) -> datetime | None:
    backups = list_backups(backup_dir, kind)
    return _taken_at(backups[-1]) if backups else None


async def create_backup(
    db: Database,
    backup_dir: Path,
    kind: BackupKind,
    *,
    keep: int,
    now: datetime | None = None,
) -> Path:
    """Write a consistent copy of ``db`` into ``backup_dir`` and apply retention."""
    now = now or datetime.now(timezone.utc)
    backup_dir.mkdir(parents=True, exist_ok=True)
    final = backup_dir / backup_name(kind, now)
    # Dot-prefixed temp name: ignored by list_backups and hidden in Finder.
    tmp = backup_dir / f".{final.name}.partial"
    tmp.unlink(missing_ok=True)
    try:
        await db.vacuum_into(tmp)
        os.replace(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
    log.info("Database backup written: %s (%d bytes)", final, final.stat().st_size)
    prune(backup_dir, kind, keep if kind == "daily" else MANUAL_KEEP)
    return final


def sync_offsite(
    backup_dir: Path,
    offsite_dir: Path,
    *,
    keep: int,
    max_age: timedelta,
    now: datetime | None = None,
) -> Path | None:
    """Copy the newest daily backup to ``offsite_dir`` and apply retention there.

    Returns the new copy, or None if the offsite folder already had it. Run
    hourly, so a copy that failed (folder unmounted, disk full) is retried.
    The folder must already exist: creating it would quietly write the
    "offsite" copy to the local disk when a network or cloud drive isn't
    mounted.
    """
    if not offsite_dir.is_dir():
        raise FileNotFoundError(
            f"offsite backup folder {offsite_dir} doesn't exist (not mounted?)"
        )
    copied = None
    dailies = list_backups(backup_dir, "daily")
    if dailies and not (offsite_dir / dailies[-1].name).exists():
        source = dailies[-1]
        final = offsite_dir / source.name
        tmp = offsite_dir / f".{source.name}.partial"
        try:
            shutil.copyfile(source, tmp)
            os.replace(tmp, final)
        finally:
            tmp.unlink(missing_ok=True)
        log.info("Offsite backup copy written: %s", final)
        copied = final
    prune(offsite_dir, "daily", keep)
    prune_older_than(offsite_dir, max_age, now=now)
    return copied
