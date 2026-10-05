"""Make sure persistent paths are host mounts when running in a container.

Without a mount, the database (or backups) would be written inside the
container's own filesystem and silently lost when the container is replaced.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

MOUNTINFO = Path("/proc/self/mountinfo")
DOCKERENV = Path("/.dockerenv")
# The kernel escapes space, tab, newline and backslash as \NNN octal.
_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")


def in_container() -> bool:
    return DOCKERENV.exists()


def mount_points(mountinfo: str) -> set[str]:
    """Mount points from /proc/self/mountinfo (field 5, octal escapes decoded)."""
    points: set[str] = set()
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) >= 5:
            points.add(_OCTAL_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), fields[4]))
    return points


def is_on_mount(path: Path, points: set[str]) -> bool:
    """True when ``path`` is at or below a mount point other than ``/``."""
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        if str(candidate) == "/":
            return False
        if str(candidate) in points:
            return True
    return False


def check_persistent_paths(
    database_path: Path,
    backup_dir: Path | None,
    *,
    allow_unmounted: bool,
) -> bool:
    """
    Raise if the database directory isn't a host mount (inside a container).

    Returns whether backups can be written persistently. Does nothing outside a
    container, where paths are already on the host.
    """
    if not in_container() or not MOUNTINFO.exists():
        return backup_dir is not None
    points = mount_points(MOUNTINFO.read_text())

    data_dir = database_path.parent
    if not is_on_mount(data_dir, points):
        message = (
            f"Database directory {data_dir} is not a mounted volume, so data would "
            "be lost when the container is replaced. Set BOT_DATA_DIR in .env "
            "(see docker-compose.yml)."
        )
        if not allow_unmounted:
            raise RuntimeError(message + " Set ALLOW_UNMOUNTED_DATA=1 to override.")
        log.error("%s Continuing because ALLOW_UNMOUNTED_DATA=1.", message)

    if backup_dir is None:
        return False
    if not is_on_mount(backup_dir, points):
        log.error(
            "Backup directory %s is not a mounted volume; backups are disabled. "
            "Set BOT_BACKUP_DIR in .env (see docker-compose.yml).",
            backup_dir,
        )
        return False
    return True
