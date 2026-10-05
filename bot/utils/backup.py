"""SQLite backups that are safe to take while the bot is running.

Backups use ``VACUUM INTO``, which writes a consistent, compacted copy through
SQLite itself (a plain file copy of a WAL-mode database can miss recent writes
or be torn mid-write). Each backup is written to a temporary name and renamed
into place, so sync tools such as iCloud never pick up a partial file.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from bot.db import Database

log = logging.getLogger(__name__)

BackupKind = Literal["daily", "manual"]

# Manual (/admin backup) copies are kept separately so on-demand backups never
# push scheduled ones out of the retention window.
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


def last_backup_time(backup_dir: Path, kind: BackupKind) -> datetime | None:
    backups = list_backups(backup_dir, kind)
    if not backups:
        return None
    m = _NAME_RE.match(backups[-1].name)
    assert m is not None
    return datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)


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
