"""Tests for database backups and the persistent-storage startup check."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

# Allow `python tests/test_backup.py` from repo root
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.db import Database  # noqa: E402
from bot.utils import storage  # noqa: E402
from bot.utils.health import HealthState  # noqa: E402
from bot.utils.backup import (  # noqa: E402
    MANUAL_KEEP,
    create_backup,
    last_backup_time,
    list_backups,
    prune,
    prune_older_than,
)

T0 = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)


class BackupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.backup_dir = root / "backups"
        self.db = Database(root / "live.db")
        await self.db.connect()
        await self.db.upsert_metrics(
            "g1", [("2026-09-27", "EnemyHelicopter", "Kills", 1_078_263)], channel_id="c1"
        )

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self._tmp.cleanup()

    async def test_backup_is_complete_readable_copy(self) -> None:
        # The write above may still sit in the WAL; the backup must include it.
        path = await create_backup(self.db, self.backup_dir, "manual", keep=14, now=T0)
        self.assertEqual(path.name, "weekly-20261004-120000-manual.db")
        with sqlite3.connect(path) as conn:
            rows = conn.execute(
                "SELECT PlayerName, MetricType, Value FROM WeeklyMetrics"
            ).fetchall()
        self.assertEqual(rows, [("EnemyHelicopter", "Kills", 1_078_263.0)])
        # No temp files left behind for iCloud to sync.
        self.assertEqual([p.name for p in self.backup_dir.iterdir()], [path.name])

    async def test_daily_retention_ignores_manual_backups(self) -> None:
        manual = await create_backup(self.db, self.backup_dir, "manual", keep=3, now=T0)
        for day in range(5):
            await create_backup(
                self.db, self.backup_dir, "daily", keep=3, now=T0 + timedelta(days=day)
            )
        daily = list_backups(self.backup_dir, "daily")
        self.assertEqual(len(daily), 3)
        self.assertTrue(daily[-1].name.startswith("weekly-20261008"))
        self.assertTrue(manual.exists())
        self.assertEqual(
            last_backup_time(self.backup_dir, "daily"), T0 + timedelta(days=4)
        )

    async def test_manual_retention(self) -> None:
        for n in range(MANUAL_KEEP + 2):
            await create_backup(
                self.db, self.backup_dir, "manual", keep=14, now=T0 + timedelta(minutes=n)
            )
        self.assertEqual(len(list_backups(self.backup_dir, "manual")), MANUAL_KEEP)

    def test_prune_leaves_unrelated_files(self) -> None:
        self.backup_dir.mkdir()
        (self.backup_dir / "notes.txt").write_text("keep me")
        (self.backup_dir / "weekly-20261001-000000-daily.db").write_bytes(b"")
        prune(self.backup_dir, "daily", keep=0)
        self.assertEqual(
            [p.name for p in self.backup_dir.iterdir()], ["notes.txt"]
        )

    def _touch(self, *names: str) -> None:
        self.backup_dir.mkdir(exist_ok=True)
        for name in names:
            (self.backup_dir / name).write_bytes(b"")

    def _names(self) -> list[str]:
        return sorted(p.name for p in self.backup_dir.iterdir())

    def test_age_limit_applies_to_every_kind(self) -> None:
        self._touch(
            "weekly-20260801-000000-manual.db",  # 64 days old
            "weekly-20260903-000000-manual.db",  # 31 days old
            "weekly-20260905-000000-manual.db",  # 29 days old
            "weekly-20260902-120000-daily.db",  # 32 days old
            "weekly-20261003-000000-daily.db",
            "notes.txt",
        )
        removed = prune_older_than(self.backup_dir, timedelta(days=30), now=T0)
        self.assertEqual(len(removed), 3)
        self.assertEqual(
            self._names(),
            ["notes.txt", "weekly-20260905-000000-manual.db", "weekly-20261003-000000-daily.db"],
        )

    def test_age_limit_keeps_newest_backup(self) -> None:
        # Nothing new for months (bot off, disk full): keep the last copy.
        self._touch("weekly-20260101-000000-daily.db", "weekly-20260201-000000-manual.db")
        prune_older_than(self.backup_dir, timedelta(days=30), now=T0)
        self.assertEqual(self._names(), ["weekly-20260201-000000-manual.db"])

    def test_age_limit_with_no_folder(self) -> None:
        self.assertEqual(prune_older_than(self.backup_dir, timedelta(days=30), now=T0), [])

    async def test_hourly_task_prunes_even_when_no_backup_is_due(self) -> None:
        from types import SimpleNamespace

        from bot.cogs.admin import Admin

        recent = datetime.now(timezone.utc) - timedelta(hours=2)
        self._touch(
            "weekly-20200101-000000-manual.db",
            f"weekly-{recent:%Y%m%d-%H%M%S}-daily.db",
        )
        settings = SimpleNamespace(
            backup_dir=self.backup_dir, backup_keep=14, backup_max_age_days=30, offsite_backup_dir=None
        )
        bot = SimpleNamespace(settings=settings, db=self.db, health=HealthState())
        cog = Admin(bot)  # type: ignore[arg-type]
        await cog.daily_backup.coro(cog)
        # No new daily backup (the last one is 2 hours old); the old manual one is gone.
        self.assertEqual(self._names(), [f"weekly-{recent:%Y%m%d-%H%M%S}-daily.db"])

    def test_no_backups_yet(self) -> None:
        self.assertIsNone(last_backup_time(self.backup_dir, "daily"))


MOUNTINFO = """\
600 500 0:52 / / rw,relatime - overlay overlay rw
601 600 0:55 / /proc rw - proc proc rw
610 600 0:80 /Users/matt/DiscordBot/data /app/data rw - fakeowner /run/host_mark/Users rw
611 600 0:80 /Users/matt/Documents/DiscordBot\\040Backup /app/back\\040ups rw - fakeowner x rw
612 600 0:80 /Users/matt/Café /app/café rw - fakeowner x rw
"""


class StorageCheckTests(unittest.TestCase):
    def test_mount_points_decode_escapes(self) -> None:
        points = storage.mount_points(MOUNTINFO)
        self.assertIn("/app/data", points)
        self.assertIn("/app/back ups", points)
        self.assertIn("/app/café", points)

    def test_is_on_mount(self) -> None:
        points = storage.mount_points(MOUNTINFO)
        self.assertTrue(storage.is_on_mount(Path("/app/data"), points))
        self.assertTrue(storage.is_on_mount(Path("/app/data/sub"), points))
        self.assertFalse(storage.is_on_mount(Path("/app/other"), points))

    def _check(self, mountinfo: str, *, allow: bool = False) -> bool:
        with tempfile.TemporaryDirectory() as tmp:
            info = Path(tmp) / "mountinfo"
            info.write_text(mountinfo)
            dockerenv = Path(tmp) / ".dockerenv"
            dockerenv.touch()
            with patch.object(storage, "MOUNTINFO", info), patch.object(
                storage, "DOCKERENV", dockerenv
            ):
                return storage.check_persistent_paths(
                    Path("/app/data/weekly.db"),
                    Path("/app/backups"),
                    allow_unmounted=allow,
                )

    def test_unmounted_data_refuses_to_start(self) -> None:
        bare = "600 500 0:52 / / rw,relatime - overlay overlay rw\n"
        with self.assertRaises(RuntimeError):
            self._check(bare)
        with self.assertLogs("bot.utils.storage", "ERROR"):
            self.assertFalse(self._check(bare, allow=True))

    def test_unmounted_backups_disable_backups(self) -> None:
        data_only = MOUNTINFO.splitlines()[0] + "\n" + MOUNTINFO.splitlines()[2]
        with self.assertLogs("bot.utils.storage", "ERROR"):
            self.assertFalse(self._check(data_only))

    def test_both_mounted(self) -> None:
        mounted = MOUNTINFO.replace("/app/back\\040ups", "/app/backups")
        self.assertTrue(self._check(mounted))

    def test_outside_container_skips_check(self) -> None:
        with patch.object(storage, "DOCKERENV", Path("/definitely/not/here")):
            self.assertTrue(
                storage.check_persistent_paths(
                    Path("/tmp/x.db"), Path("/tmp/b"), allow_unmounted=False
                )
            )


if __name__ == "__main__":
    unittest.main()
