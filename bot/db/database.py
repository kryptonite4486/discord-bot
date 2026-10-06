"""SQLite datastore for WeeklyMetrics."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Iterable, Sequence

import aiosqlite

from bot.config import METRIC_TYPES

log = logging.getLogger(__name__)

# Empty string = unassigned channel (Phase 1 pre-migration).
UNASSIGNED_CHANNEL_ID = ""

# UsageLedger measures. ocr_images counts every image sent to the vision
# model (failed ones too: they still cost GPU time).
USAGE_KINDS = (
    "ocr_batches",
    "ocr_images",
    "ocr_failed",
    "ocr_seconds",
    "ocr_wait_seconds",
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS WeeklyMetrics (
    GuildId     TEXT    NOT NULL,
    ChannelId   TEXT    NOT NULL DEFAULT '',
    WeekStart   TEXT    NOT NULL,
    PlayerName  TEXT    NOT NULL,
    MetricType  TEXT    NOT NULL,
    Value       REAL    NOT NULL,
    UpdatedAt   TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (GuildId, ChannelId, WeekStart, PlayerName, MetricType)
);

CREATE INDEX IF NOT EXISTS idx_weekly_player
    ON WeeklyMetrics (GuildId, ChannelId, PlayerName, MetricType, WeekStart);

CREATE INDEX IF NOT EXISTS idx_weekly_week
    ON WeeklyMetrics (GuildId, ChannelId, WeekStart, MetricType);

CREATE INDEX IF NOT EXISTS idx_weekly_unassigned
    ON WeeklyMetrics (GuildId, ChannelId);

-- Servers that removed the bot. The deletion date isn't stored: it depends
-- on subscriptions, which can change (see bot/cogs/data.py). Times are UTC
-- 'YYYY-MM-DD HH:MM:SS'.
CREATE TABLE IF NOT EXISTS PendingPurge (
    GuildId   TEXT PRIMARY KEY,
    RemovedAt TEXT NOT NULL
);

-- Paid subscriptions, gifts and trials per server (docs/monetization-plan.md
-- section 5). Active: not revoked, started, and EndsAt empty or in the future.
CREATE TABLE IF NOT EXISTS GuildEntitlement (
    Id          INTEGER PRIMARY KEY,
    GuildId     TEXT NOT NULL,
    Tier        TEXT NOT NULL,  -- 'mid' | 'full'
    Source      TEXT NOT NULL,  -- 'discord' | 'stripe' | 'gift' | 'trial' | 'code'
    ExternalId  TEXT,
    StartsAt    TEXT NOT NULL,
    EndsAt      TEXT,           -- NULL = no expiry
    GrantedBy   TEXT,
    Reason      TEXT,
    RevokedAt   TEXT,
    CreatedAt   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ent_guild
    ON GuildEntitlement (GuildId, RevokedAt, EndsAt);

-- Who granted, revoked or extended which entitlement, and why.
CREATE TABLE IF NOT EXISTS EntitlementAudit (
    Id      INTEGER PRIMARY KEY,
    At      TEXT NOT NULL DEFAULT (datetime('now')),
    ActorId TEXT,
    Action  TEXT NOT NULL,  -- grant | revoke | extend
    GuildId TEXT NOT NULL,
    Detail  TEXT
);

-- Per-server usage, one row per UTC day and measure (see USAGE_KINDS).
CREATE TABLE IF NOT EXISTS UsageLedger (
    GuildId TEXT NOT NULL,
    Day     TEXT NOT NULL,  -- UTC YYYY-MM-DD
    Kind    TEXT NOT NULL,
    Amount  REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (GuildId, Day, Kind)
);

-- Trivia totals per player per server, split by match mode ('server' |
-- 'global'). A player's cross-server points count for the server they
-- played from. GuildName is kept for the cross-server leaderboard.
CREATE TABLE IF NOT EXISTS TriviaScore (
    GuildId     TEXT    NOT NULL,
    UserId      TEXT    NOT NULL,
    Mode        TEXT    NOT NULL,
    DisplayName TEXT    NOT NULL,
    GuildName   TEXT    NOT NULL DEFAULT '',
    Points      INTEGER NOT NULL DEFAULT 0,
    Correct     INTEGER NOT NULL DEFAULT 0,
    Answered    INTEGER NOT NULL DEFAULT 0,
    Games       INTEGER NOT NULL DEFAULT 0,
    Wins        INTEGER NOT NULL DEFAULT 0,
    UpdatedAt   TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (GuildId, UserId, Mode)
);

CREATE INDEX IF NOT EXISTS idx_trivia_mode ON TriviaScore (Mode, Points);

-- Per-server trivia options. No row = default (cross-server play allowed).
CREATE TABLE IF NOT EXISTS TriviaSettings (
    GuildId     TEXT PRIMARY KEY,
    AllowGlobal INTEGER NOT NULL DEFAULT 1
);

-- Per-server options set with /setup. No row = defaults. TriviaChannelId:
-- the only channel trivia runs in (and where cross-server invitations go);
-- NULL = trivia anywhere.
CREATE TABLE IF NOT EXISTS GuildSettings (
    GuildId         TEXT PRIMARY KEY,
    TriviaChannelId TEXT,
    UpdatedAt       TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

LEGACY_GUILD_FALLBACK = "legacy"


class Database:
    """Async SQLite helper around the WeeklyMetrics fact table."""

    def __init__(
        self,
        path: Path | str,
        legacy_guild_id: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.legacy_guild_id = legacy_guild_id
        self._conn: aiosqlite.Connection | None = None
        # The bot shares one connection; a commit from any coroutine commits all
        # pending statements. Writers hold this lock across execute + commit so
        # a multi-statement change (rename_player) can't be committed half-done.
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")
        await self._migrate_add_guild_id()
        await self._migrate_add_channel_id()
        await self._conn.executescript(SCHEMA_SQL)
        await self._migrate_trivia_announce_channel()
        await self._conn.commit()
        log.info("Database ready at %s", self.path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            log.info("Database connection closed")

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    async def _table_columns(self, table: str) -> set[str]:
        async with self.conn.execute(f"PRAGMA table_info({table})") as cursor:
            rows = await cursor.fetchall()
        return {r["name"] for r in rows}

    async def _migrate_add_guild_id(self) -> None:
        """Rebuild WeeklyMetrics with GuildId if upgrading a pre-partition DB."""
        cols = await self._table_columns("WeeklyMetrics")
        if not cols or "GuildId" in cols:
            return

        guild_id = (self.legacy_guild_id or "").strip() or LEGACY_GUILD_FALLBACK
        if not (self.legacy_guild_id or "").strip():
            log.warning(
                "Migrating WeeklyMetrics without LEGACY_GUILD_ID; "
                "assigning existing rows to GuildId=%r.",
                LEGACY_GUILD_FALLBACK,
            )
        else:
            log.info(
                "Migrating WeeklyMetrics: assigning existing rows to GuildId=%s",
                guild_id,
            )

        await self.conn.execute(
            """
            CREATE TABLE WeeklyMetrics_new (
                GuildId     TEXT    NOT NULL,
                WeekStart   TEXT    NOT NULL,
                PlayerName  TEXT    NOT NULL,
                MetricType  TEXT    NOT NULL,
                Value       REAL    NOT NULL,
                UpdatedAt   TEXT    NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (GuildId, WeekStart, PlayerName, MetricType)
            )
            """
        )
        await self.conn.execute(
            """
            INSERT INTO WeeklyMetrics_new
                (GuildId, WeekStart, PlayerName, MetricType, Value, UpdatedAt)
            SELECT ?, WeekStart, PlayerName, MetricType, Value, UpdatedAt
            FROM WeeklyMetrics
            """,
            (guild_id,),
        )
        await self.conn.execute("DROP TABLE WeeklyMetrics")
        await self.conn.execute(
            "ALTER TABLE WeeklyMetrics_new RENAME TO WeeklyMetrics"
        )
        await self.conn.execute("DROP INDEX IF EXISTS idx_weekly_player")
        await self.conn.execute("DROP INDEX IF EXISTS idx_weekly_week")
        await self.conn.commit()
        log.info("WeeklyMetrics GuildId migration complete")

    async def _migrate_add_channel_id(self) -> None:
        """Add ChannelId (default '') for Phase 1 optional channel tagging."""
        cols = await self._table_columns("WeeklyMetrics")
        if not cols:
            return
        if "ChannelId" in cols:
            return
        if "GuildId" not in cols:
            return

        log.info(
            "Migrating WeeklyMetrics: adding ChannelId DEFAULT '' "
            "(unassigned until /admin assign-channel)"
        )
        await self.conn.execute(
            """
            CREATE TABLE WeeklyMetrics_new (
                GuildId     TEXT    NOT NULL,
                ChannelId   TEXT    NOT NULL DEFAULT '',
                WeekStart   TEXT    NOT NULL,
                PlayerName  TEXT    NOT NULL,
                MetricType  TEXT    NOT NULL,
                Value       REAL    NOT NULL,
                UpdatedAt   TEXT    NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (GuildId, ChannelId, WeekStart, PlayerName, MetricType)
            )
            """
        )
        await self.conn.execute(
            """
            INSERT INTO WeeklyMetrics_new
                (GuildId, ChannelId, WeekStart, PlayerName, MetricType, Value, UpdatedAt)
            SELECT GuildId, '', WeekStart, PlayerName, MetricType, Value, UpdatedAt
            FROM WeeklyMetrics
            """
        )
        await self.conn.execute("DROP TABLE WeeklyMetrics")
        await self.conn.execute(
            "ALTER TABLE WeeklyMetrics_new RENAME TO WeeklyMetrics"
        )
        await self.conn.execute("DROP INDEX IF EXISTS idx_weekly_player")
        await self.conn.execute("DROP INDEX IF EXISTS idx_weekly_week")
        await self.conn.commit()
        log.info("WeeklyMetrics ChannelId migration complete")

    async def _migrate_trivia_announce_channel(self) -> None:
        """Move TriviaSettings.AnnounceChannelId into GuildSettings.TriviaChannelId.

        The first trivia build had a separate invitation channel; /setup's
        trivia channel now does that job.
        """
        if "AnnounceChannelId" not in await self._table_columns("TriviaSettings"):
            return
        cur = await self.conn.execute(
            """
            INSERT INTO GuildSettings (GuildId, TriviaChannelId)
            SELECT GuildId, AnnounceChannelId FROM TriviaSettings
            WHERE COALESCE(AnnounceChannelId, '') != ''
            ON CONFLICT(GuildId) DO NOTHING
            """
        )
        await self.conn.execute("ALTER TABLE TriviaSettings DROP COLUMN AnnounceChannelId")
        log.info(
            "Moved %d trivia announcement channel(s) to GuildSettings.TriviaChannelId",
            cur.rowcount,
        )

    @staticmethod
    def _channel_scope_sql(
        *,
        channel_id: str | None = None,
        channel_ids: Sequence[str] | None = None,
        include_unassigned: bool = False,
    ) -> tuple[str, list[Any]]:
        """
        Build ChannelId filter.

        - channel_ids set: ChannelId IN (...)
        - channel_id set + include_unassigned: ChannelId IN (channel, '')
        - channel_id set: ChannelId = channel
        - neither: no channel filter (guild-wide)
        """
        ids: list[str] | None = None
        if channel_ids is not None:
            ids = [str(c) for c in channel_ids]
        elif channel_id is not None:
            ids = [str(channel_id)]

        if ids is None:
            return "", []

        if include_unassigned and UNASSIGNED_CHANNEL_ID not in ids:
            ids = [*ids, UNASSIGNED_CHANNEL_ID]

        if not ids:
            return " AND 0", []

        if len(ids) == 1:
            return " AND ChannelId = ?", ids
        placeholders = ",".join("?" * len(ids))
        return f" AND ChannelId IN ({placeholders})", ids

    async def upsert_metric(
        self,
        guild_id: str,
        week_start: str,
        player_name: str,
        metric_type: str,
        value: float,
        *,
        channel_id: str,
    ) -> None:
        if metric_type not in METRIC_TYPES:
            raise ValueError(f"Invalid MetricType: {metric_type}")
        player_name = player_name.strip()
        if not player_name:
            raise ValueError("PlayerName cannot be empty")
        if not guild_id:
            raise ValueError("GuildId cannot be empty")
        channel_id = str(channel_id or UNASSIGNED_CHANNEL_ID)

        async with self._write_lock:
            await self.conn.execute(
                """
                INSERT INTO WeeklyMetrics
                    (GuildId, ChannelId, WeekStart, PlayerName, MetricType, Value, UpdatedAt)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(GuildId, ChannelId, WeekStart, PlayerName, MetricType)
                DO UPDATE SET
                    Value = excluded.Value,
                    UpdatedAt = datetime('now')
                """,
                (
                    guild_id,
                    channel_id,
                    week_start,
                    player_name,
                    metric_type,
                    float(value),
                ),
            )
            await self.conn.commit()

    async def upsert_metrics(
        self,
        guild_id: str,
        rows: Iterable[tuple[str, str, str, float]],
        *,
        channel_id: str,
    ) -> int:
        """Bulk upsert (week, player, metric_type, value) for one guild+channel."""
        if not guild_id:
            raise ValueError("GuildId cannot be empty")
        channel_id = str(channel_id or UNASSIGNED_CHANNEL_ID)
        payload = [
            (guild_id, channel_id, week, player.strip(), metric, float(value))
            for week, player, metric, value in rows
            if player and player.strip() and metric in METRIC_TYPES
        ]
        if not payload:
            return 0

        async with self._write_lock:
            await self.conn.executemany(
                """
                INSERT INTO WeeklyMetrics
                    (GuildId, ChannelId, WeekStart, PlayerName, MetricType, Value, UpdatedAt)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(GuildId, ChannelId, WeekStart, PlayerName, MetricType)
                DO UPDATE SET
                    Value = excluded.Value,
                    UpdatedAt = datetime('now')
                """,
                payload,
            )
            await self.conn.commit()
            return len(payload)

    async def player_name_counts(self, guild_id: str, channel_id: str) -> dict[str, int]:
        """Stored player names in one guild+channel with their row counts."""
        async with self.conn.execute(
            """
            SELECT PlayerName, count(*) AS n FROM WeeklyMetrics
            WHERE GuildId = ? AND ChannelId = ?
            GROUP BY PlayerName
            """,
            (guild_id, str(channel_id)),
        ) as cursor:
            return {row["PlayerName"]: row["n"] async for row in cursor}

    async def player_name_summary(
        self, guild_id: str, *, channel_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Per channel+player: row count and first/last week (one channel or whole guild)."""
        extra_sql, extra_params = self._channel_scope_sql(channel_id=channel_id)
        async with self.conn.execute(
            f"""
            SELECT ChannelId, PlayerName, count(*) AS Rows,
                   min(WeekStart) AS FirstWeek, max(WeekStart) AS LastWeek
            FROM WeeklyMetrics
            WHERE GuildId = ?{extra_sql}
            GROUP BY ChannelId, PlayerName
            """,
            (guild_id, *extra_params),
        ) as cursor:
            return [dict(r) async for r in cursor]

    async def name_conflicts(
        self,
        guild_id: str,
        names: Sequence[str],
        *,
        channel_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """(channel, week, metric) slots where more than one of ``names`` has a value."""
        if len(names) < 2:
            return []
        extra_sql, extra_params = self._channel_scope_sql(channel_id=channel_id)
        placeholders = ",".join("?" * len(names))
        async with self.conn.execute(
            f"""
            SELECT ChannelId, WeekStart, MetricType,
                   group_concat(PlayerName || '=' || Value, ', ') AS ValuesByName
            FROM WeeklyMetrics
            WHERE GuildId = ? AND PlayerName IN ({placeholders}){extra_sql}
            GROUP BY ChannelId, WeekStart, MetricType
            HAVING count(*) > 1
            ORDER BY WeekStart, ChannelId, MetricType
            """,
            (guild_id, *names, *extra_params),
        ) as cursor:
            return [dict(r) async for r in cursor]

    async def rename_player(
        self,
        guild_id: str,
        from_name: str,
        to_name: str,
        *,
        channel_id: str | None = None,
        on_conflict: str = "stop",
    ) -> dict[str, Any]:
        """
        Move every row for ``from_name`` to ``to_name`` (one channel or whole guild).

        When both names have a value for the same channel/week/metric:
        ``on_conflict="stop"`` changes nothing and returns the conflicts,
        ``"keep_target"`` drops ``from_name``'s value, ``"keep_source"`` replaces
        ``to_name``'s value. Returns {"moved", "replaced", "dropped", "conflicts"}.
        """
        if on_conflict not in {"stop", "keep_target", "keep_source"}:
            raise ValueError(f"Invalid on_conflict: {on_conflict}")
        from_name, to_name = from_name.strip(), to_name.strip()
        if not from_name or not to_name:
            raise ValueError("Player names cannot be empty")
        if from_name == to_name:
            raise ValueError("Old and new names are the same")

        async with self._write_lock:
            conflicts = await self.name_conflicts(
                guild_id, [from_name, to_name], channel_id=channel_id
            )
            result: dict[str, Any] = {
                "moved": 0,
                "replaced": 0,
                "dropped": 0,
                "conflicts": conflicts,
            }
            if conflicts and on_conflict == "stop":
                return result

            extra_sql, extra_params = self._channel_scope_sql(channel_id=channel_id)
            # Rows of ``loser`` that share a channel/week/metric with the other name.
            clash_sql = f"""
                DELETE FROM WeeklyMetrics
                WHERE GuildId = ? AND PlayerName = ?{extra_sql}
                  AND EXISTS (
                    SELECT 1 FROM WeeklyMetrics o
                    WHERE o.GuildId = WeeklyMetrics.GuildId
                      AND o.ChannelId = WeeklyMetrics.ChannelId
                      AND o.WeekStart = WeeklyMetrics.WeekStart
                      AND o.MetricType = WeeklyMetrics.MetricType
                      AND o.PlayerName = ?
                  )
            """
            try:
                if conflicts:
                    loser, winner = (
                        (from_name, to_name)
                        if on_conflict == "keep_target"
                        else (to_name, from_name)
                    )
                    cur = await self.conn.execute(
                        clash_sql, (guild_id, loser, *extra_params, winner)
                    )
                    key = "dropped" if on_conflict == "keep_target" else "replaced"
                    result[key] = cur.rowcount
                cur = await self.conn.execute(
                    f"""
                    UPDATE WeeklyMetrics SET PlayerName = ?, UpdatedAt = datetime('now')
                    WHERE GuildId = ? AND PlayerName = ?{extra_sql}
                    """,
                    (to_name, guild_id, from_name, *extra_params),
                )
                result["moved"] = cur.rowcount
                await self.conn.commit()
            except Exception:
                await self.conn.rollback()
                raise
        log.info(
            "Renamed player %r -> %r guild=%s channel=%s: %s",
            from_name,
            to_name,
            guild_id,
            channel_id or "*",
            {k: v for k, v in result.items() if k != "conflicts"},
        )
        return result

    async def vacuum_into(self, path: Path) -> None:
        """Write a consistent, compacted copy of the database to ``path``."""
        await self.conn.execute("VACUUM INTO ?", (str(path),))

    async def get_latest_values(
        self,
        guild_id: str,
        metric_type: str,
        players: Iterable[str],
        *,
        channel_id: str,
        week_start: str,
        include_week: bool,
    ) -> dict[str, float]:
        """
        Most recent value per player (keyed by lowercased name) for one
        guild+channel, from weeks before ``week_start`` (or on/before it when
        ``include_week``).
        """
        names = sorted({p.strip().lower() for p in players if p and p.strip()})
        if not names:
            return {}
        placeholders = ",".join("?" for _ in names)
        op = "<=" if include_week else "<"
        sql = f"""
            SELECT PlayerName, Value
            FROM WeeklyMetrics
            WHERE GuildId = ? AND ChannelId = ? AND MetricType = ?
              AND WeekStart {op} ? AND lower(PlayerName) IN ({placeholders})
            ORDER BY WeekStart
        """
        params = [guild_id, str(channel_id), metric_type, week_start, *names]
        latest: dict[str, float] = {}
        async with self.conn.execute(sql, params) as cursor:
            async for row in cursor:
                latest[row["PlayerName"].lower()] = float(row["Value"])
        return latest

    async def assign_channel(
        self,
        guild_id: str,
        channel_id: str,
        *,
        only_unassigned: bool = True,
    ) -> int:
        """
        Set ChannelId for rows in a guild.

        Phase 1 backfill: only_unassigned=True updates ChannelId='' rows.
        Returns number of rows updated.
        """
        if not guild_id or not channel_id:
            raise ValueError("guild_id and channel_id are required")
        channel_id = str(channel_id)
        async with self._write_lock:
            if only_unassigned:
                cursor = await self.conn.execute(
                    """
                    UPDATE WeeklyMetrics
                    SET ChannelId = ?, UpdatedAt = datetime('now')
                    WHERE GuildId = ? AND ChannelId = ?
                    """,
                    (channel_id, guild_id, UNASSIGNED_CHANNEL_ID),
                )
            else:
                cursor = await self.conn.execute(
                    """
                    UPDATE WeeklyMetrics
                    SET ChannelId = ?, UpdatedAt = datetime('now')
                    WHERE GuildId = ?
                    """,
                    (channel_id, guild_id),
                )
            await self.conn.commit()
            return cursor.rowcount

    async def count_unassigned(self, guild_id: str) -> int:
        async with self.conn.execute(
            """
            SELECT COUNT(*) AS c FROM WeeklyMetrics
            WHERE GuildId = ? AND ChannelId = ?
            """,
            (guild_id, UNASSIGNED_CHANNEL_ID),
        ) as cursor:
            return int((await cursor.fetchone())["c"])

    async def count_all_unassigned(self) -> int:
        async with self.conn.execute(
            """
            SELECT COUNT(*) AS c FROM WeeklyMetrics
            WHERE ChannelId = ?
            """,
            (UNASSIGNED_CHANNEL_ID,),
        ) as cursor:
            return int((await cursor.fetchone())["c"])

    async def get_player_metrics(
        self,
        guild_id: str,
        player_name: str,
        metric_type: str | None = None,
        limit: int | None = None,
        *,
        channel_id: str | None = None,
        channel_ids: Sequence[str] | None = None,
        include_unassigned: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Return a player's rows with Rank and Population within their data channel.

        Rank is 1-based among all players in the same GuildId + ChannelId +
        WeekStart + MetricType (ordered by Value descending). Population is
        that group's size.
        """
        extra_sql, extra_params = self._channel_scope_sql(
            channel_id=channel_id,
            channel_ids=channel_ids,
            include_unassigned=include_unassigned,
        )
        player_clauses = ["PlayerName = ? COLLATE NOCASE"]
        player_params: list[Any] = [player_name]
        if metric_type:
            player_clauses.append("MetricType = ?")
            player_params.append(metric_type)
        player_where = " AND ".join(player_clauses) + extra_sql
        player_params.extend(extra_params)

        sql = f"""
            WITH ranked AS (
                SELECT
                    GuildId,
                    ChannelId,
                    WeekStart,
                    PlayerName,
                    MetricType,
                    Value,
                    UpdatedAt,
                    RANK() OVER (
                        PARTITION BY GuildId, ChannelId, WeekStart, MetricType
                        ORDER BY Value DESC, PlayerName COLLATE NOCASE
                    ) AS Rank,
                    COUNT(*) OVER (
                        PARTITION BY GuildId, ChannelId, WeekStart, MetricType
                    ) AS Population
                FROM WeeklyMetrics
                WHERE GuildId = ?
            )
            SELECT
                GuildId, ChannelId, WeekStart, PlayerName, MetricType, Value,
                UpdatedAt, Rank, Population
            FROM ranked
            WHERE {player_where}
            ORDER BY WeekStart DESC, MetricType
        """
        params: list[Any] = [guild_id, *player_params]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)

        async with self.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_week_metrics(
        self,
        guild_id: str,
        week_start: str,
        metric_type: str | None = None,
        *,
        channel_id: str | None = None,
        channel_ids: Sequence[str] | None = None,
        include_unassigned: bool = False,
    ) -> list[dict[str, Any]]:
        extra_sql, extra_params = self._channel_scope_sql(
            channel_id=channel_id,
            channel_ids=channel_ids,
            include_unassigned=include_unassigned,
        )
        if metric_type:
            sql = f"""
                SELECT GuildId, ChannelId, WeekStart, PlayerName, MetricType, Value, UpdatedAt
                FROM WeeklyMetrics
                WHERE GuildId = ? AND WeekStart = ? AND MetricType = ?{extra_sql}
                ORDER BY Value DESC, PlayerName
            """
            params: list[Any] = [guild_id, week_start, metric_type, *extra_params]
        else:
            sql = f"""
                SELECT GuildId, ChannelId, WeekStart, PlayerName, MetricType, Value, UpdatedAt
                FROM WeeklyMetrics
                WHERE GuildId = ? AND WeekStart = ?{extra_sql}
                ORDER BY MetricType, Value DESC, PlayerName
            """
            params = [guild_id, week_start, *extra_params]

        async with self.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_trends(
        self,
        guild_id: str,
        metric_type: str,
        weeks: int = 8,
        player_name: str | None = None,
        *,
        channel_id: str | None = None,
        channel_ids: Sequence[str] | None = None,
        include_unassigned: bool = False,
    ) -> list[dict[str, Any]]:
        """Return recent weekly values for a metric, optionally for one player."""
        extra_sql, extra_params = self._channel_scope_sql(
            channel_id=channel_id,
            channel_ids=channel_ids,
            include_unassigned=include_unassigned,
        )
        week_filter_sql = f"""
            SELECT DISTINCT WeekStart
            FROM WeeklyMetrics
            WHERE GuildId = ? AND MetricType = ?{extra_sql}
            ORDER BY WeekStart DESC
            LIMIT ?
        """
        async with self.conn.execute(
            week_filter_sql, (guild_id, metric_type, *extra_params, weeks)
        ) as cursor:
            week_rows = await cursor.fetchall()
        week_list = [r["WeekStart"] for r in week_rows]
        if not week_list:
            return []

        placeholders = ",".join("?" * len(week_list))
        clauses = [
            "GuildId = ?",
            f"WeekStart IN ({placeholders})",
            "MetricType = ?",
        ]
        params: list[Any] = [guild_id, *week_list, metric_type]
        if player_name:
            clauses.append("PlayerName = ? COLLATE NOCASE")
            params.append(player_name)
        # Channel scope binds after all clause params (extra_sql starts with AND).
        params.extend(extra_params)

        sql = f"""
            SELECT GuildId, ChannelId, WeekStart, PlayerName, MetricType, Value
            FROM WeeklyMetrics
            WHERE {' AND '.join(clauses)}{extra_sql}
            ORDER BY WeekStart ASC, PlayerName
        """
        async with self.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_growth_rates(
        self,
        guild_id: str,
        metric_type: str,
        weeks: int = 4,
        *,
        channel_id: str | None = None,
        channel_ids: Sequence[str] | None = None,
        include_unassigned: bool = False,
    ) -> list[dict[str, Any]]:
        trends = await self.get_trends(
            guild_id,
            metric_type,
            weeks=weeks,
            channel_id=channel_id,
            channel_ids=channel_ids,
            include_unassigned=include_unassigned,
        )
        by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in trends:
            key = (row["PlayerName"], str(row.get("ChannelId") or ""))
            by_key.setdefault(key, []).append(row)

        results: list[dict[str, Any]] = []
        for (player, ch_id), series in by_key.items():
            if len(series) < 2:
                continue
            first, last = series[0], series[-1]
            delta = last["Value"] - first["Value"]
            growth = (delta / first["Value"] * 100.0) if first["Value"] else None
            results.append(
                {
                    "PlayerName": player,
                    "ChannelId": ch_id,
                    "FirstWeek": first["WeekStart"],
                    "LastWeek": last["WeekStart"],
                    "FirstValue": first["Value"],
                    "LastValue": last["Value"],
                    "Delta": delta,
                    "GrowthPct": growth,
                }
            )

        results.sort(
            key=lambda r: (r["GrowthPct"] is None, -(r["GrowthPct"] or 0), -r["Delta"])
        )
        return results

    async def get_leaderboard(
        self,
        guild_id: str,
        metric_type: str,
        week_start: str | None = None,
        limit: int | None = None,
        *,
        channel_id: str | None = None,
        channel_ids: Sequence[str] | None = None,
        include_unassigned: bool = False,
    ) -> list[dict[str, Any]]:
        extra_sql, extra_params = self._channel_scope_sql(
            channel_id=channel_id,
            channel_ids=channel_ids,
            include_unassigned=include_unassigned,
        )
        if week_start is None:
            async with self.conn.execute(
                f"""
                SELECT WeekStart FROM WeeklyMetrics
                WHERE GuildId = ? AND MetricType = ?{extra_sql}
                ORDER BY WeekStart DESC LIMIT 1
                """,
                (guild_id, metric_type, *extra_params),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return []
            week_start = row["WeekStart"]

        sql = f"""
            SELECT GuildId, ChannelId, WeekStart, PlayerName, MetricType, Value
            FROM WeeklyMetrics
            WHERE GuildId = ? AND WeekStart = ? AND MetricType = ?{extra_sql}
            ORDER BY Value DESC, PlayerName
        """
        params: list[Any] = [guild_id, week_start, metric_type, *extra_params]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        async with self.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def list_weeks(
        self,
        guild_id: str,
        limit: int = 52,
        *,
        channel_id: str | None = None,
        channel_ids: Sequence[str] | None = None,
        include_unassigned: bool = False,
    ) -> list[str]:
        extra_sql, extra_params = self._channel_scope_sql(
            channel_id=channel_id,
            channel_ids=channel_ids,
            include_unassigned=include_unassigned,
        )
        async with self.conn.execute(
            f"""
            SELECT DISTINCT WeekStart FROM WeeklyMetrics
            WHERE GuildId = ?{extra_sql}
            ORDER BY WeekStart DESC LIMIT ?
            """,
            (guild_id, *extra_params, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [r["WeekStart"] for r in rows]

    async def list_players(
        self,
        guild_id: str,
        *,
        channel_id: str | None = None,
        channel_ids: Sequence[str] | None = None,
        include_unassigned: bool = False,
    ) -> list[str]:
        extra_sql, extra_params = self._channel_scope_sql(
            channel_id=channel_id,
            channel_ids=channel_ids,
            include_unassigned=include_unassigned,
        )
        async with self.conn.execute(
            f"""
            SELECT DISTINCT PlayerName FROM WeeklyMetrics
            WHERE GuildId = ?{extra_sql}
            ORDER BY PlayerName COLLATE NOCASE
            """,
            (guild_id, *extra_params),
        ) as cursor:
            rows = await cursor.fetchall()
        return [r["PlayerName"] for r in rows]

    async def stats(
        self,
        guild_id: str,
        *,
        channel_id: str | None = None,
        channel_ids: Sequence[str] | None = None,
        include_unassigned: bool = False,
    ) -> dict[str, Any]:
        extra_sql, extra_params = self._channel_scope_sql(
            channel_id=channel_id,
            channel_ids=channel_ids,
            include_unassigned=include_unassigned,
        )
        async with self.conn.execute(
            f"SELECT COUNT(*) AS c FROM WeeklyMetrics WHERE GuildId = ?{extra_sql}",
            (guild_id, *extra_params),
        ) as cursor:
            total = (await cursor.fetchone())["c"]
        async with self.conn.execute(
            f"""
            SELECT COUNT(DISTINCT PlayerName) AS c FROM WeeklyMetrics
            WHERE GuildId = ?{extra_sql}
            """,
            (guild_id, *extra_params),
        ) as cursor:
            players = (await cursor.fetchone())["c"]
        async with self.conn.execute(
            f"""
            SELECT COUNT(DISTINCT WeekStart) AS c FROM WeeklyMetrics
            WHERE GuildId = ?{extra_sql}
            """,
            (guild_id, *extra_params),
        ) as cursor:
            weeks = (await cursor.fetchone())["c"]
        async with self.conn.execute(
            f"""
            SELECT MetricType, COUNT(*) AS c
            FROM WeeklyMetrics
            WHERE GuildId = ?{extra_sql}
            GROUP BY MetricType
            """,
            (guild_id, *extra_params),
        ) as cursor:
            by_metric = {r["MetricType"]: r["c"] for r in await cursor.fetchall()}
        unassigned = await self.count_unassigned(guild_id)
        return {
            "rows": total,
            "players": players,
            "weeks": weeks,
            "by_metric": by_metric,
            "path": str(self.path),
            "guild_id": guild_id,
            "channel_id": channel_id,
            "unassigned_rows": unassigned,
        }

    async def channel_row_counts(self, guild_id: str) -> list[tuple[str, int]]:
        """Return (ChannelId, row_count) for every channel bucket in a guild."""
        async with self.conn.execute(
            """
            SELECT ChannelId, COUNT(*) AS c
            FROM WeeklyMetrics
            WHERE GuildId = ?
            GROUP BY ChannelId
            ORDER BY c DESC, ChannelId
            """,
            (guild_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [(str(r["ChannelId"]), int(r["c"])) for r in rows]

    async def add_usage(
        self, guild_id: str, day: str, amounts: dict[str, float]
    ) -> None:
        """Add to a server's usage counters for one UTC day."""
        unknown = set(amounts) - set(USAGE_KINDS)
        if unknown:
            raise ValueError(f"Invalid usage kind(s): {sorted(unknown)}")
        rows = [(guild_id, day, k, float(v)) for k, v in amounts.items() if v]
        if not rows:
            return
        async with self._write_lock:
            await self.conn.executemany(
                """
                INSERT INTO UsageLedger (GuildId, Day, Kind, Amount)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(GuildId, Day, Kind)
                DO UPDATE SET Amount = Amount + excluded.Amount
                """,
                rows,
            )
            await self.conn.commit()

    async def usage_by_guild(self, since_day: str) -> dict[str, dict[str, float]]:
        """Totals per server and measure from ``since_day`` (inclusive)."""
        async with self.conn.execute(
            """
            SELECT GuildId, Kind, SUM(Amount) AS total
            FROM UsageLedger
            WHERE Day >= ?
            GROUP BY GuildId, Kind
            """,
            (since_day,),
        ) as cursor:
            rows = await cursor.fetchall()
        out: dict[str, dict[str, float]] = {}
        for r in rows:
            out.setdefault(str(r["GuildId"]), {})[str(r["Kind"])] = float(r["total"])
        return out

    async def usage_by_day(self, since_day: str, kind: str) -> list[tuple[str, float]]:
        """One measure summed across servers per day, oldest first."""
        async with self.conn.execute(
            """
            SELECT Day, SUM(Amount) AS total
            FROM UsageLedger
            WHERE Day >= ? AND Kind = ?
            GROUP BY Day
            ORDER BY Day
            """,
            (since_day, kind),
        ) as cursor:
            rows = await cursor.fetchall()
        return [(str(r["Day"]), float(r["total"])) for r in rows]

    async def delete_guild_data(
        self, guild_id: str, *, channel_id: str | None = None
    ) -> int:
        """Delete a server's metrics (one channel, or all of them); return rows removed.

        Usage counters are kept: they hold only counts per day, no player data.
        """
        if not guild_id:
            raise ValueError("GuildId cannot be empty")
        sql = "DELETE FROM WeeklyMetrics WHERE GuildId = ?"
        params: tuple[str, ...] = (guild_id,)
        if channel_id is not None:
            sql += " AND ChannelId = ?"
            params = (guild_id, channel_id)
        async with self._write_lock:
            cursor = await self.conn.execute(sql, params)
            await self.conn.commit()
        return cursor.rowcount

    async def guilds_with_data(self) -> set[str]:
        async with self.conn.execute(
            """
            SELECT GuildId FROM WeeklyMetrics UNION SELECT GuildId FROM TriviaScore
            UNION SELECT GuildId FROM TriviaSettings UNION SELECT GuildId FROM GuildSettings
            """
        ) as cursor:
            return {str(r["GuildId"]) for r in await cursor.fetchall()}

    async def schedule_purge(self, guild_id: str, removed_at: str) -> bool:
        """Record that the bot was removed; False if already recorded.

        An existing record is kept, so a restart can't push the date back.
        """
        async with self._write_lock:
            cursor = await self.conn.execute(
                """
                INSERT INTO PendingPurge (GuildId, RemovedAt)
                VALUES (?, ?)
                ON CONFLICT(GuildId) DO NOTHING
                """,
                (guild_id, removed_at),
            )
            await self.conn.commit()
        return cursor.rowcount > 0

    async def cancel_purge(self, guild_id: str) -> bool:
        """Forget a removal (the bot is back); True if one was recorded."""
        async with self._write_lock:
            cursor = await self.conn.execute(
                "DELETE FROM PendingPurge WHERE GuildId = ?", (guild_id,)
            )
            await self.conn.commit()
        return cursor.rowcount > 0

    async def pending_purges(self) -> list[dict[str, str]]:
        """Servers that removed the bot, earliest removal first."""
        async with self.conn.execute(
            "SELECT GuildId, RemovedAt FROM PendingPurge ORDER BY RemovedAt"
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def subscription_status(self, guild_id: str, now: str) -> tuple[bool, str | None]:
        """(has an active subscription, when the last one ended).

        Any entitlement counts: paid, gifted or trial. The end of an inactive
        one is the earlier of EndsAt and RevokedAt (never later than ``now``).
        """
        async with self.conn.execute(
            """
            SELECT
              MAX(RevokedAt IS NULL AND StartsAt <= :now
                  AND (EndsAt IS NULL OR EndsAt > :now)) AS active,
              MAX(MIN(COALESCE(EndsAt, :now), COALESCE(RevokedAt, :now), :now))
                  AS ended
            FROM GuildEntitlement
            WHERE GuildId = :guild AND StartsAt <= :now
            """,
            {"guild": guild_id, "now": now},
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or row["active"] is None:
            return False, None
        return bool(row["active"]), row["ended"]

    async def purge_guild(self, guild_id: str) -> int:
        """Delete a server's metrics and trivia data and clear its schedule.

        Returns metric rows removed.
        """
        async with self._write_lock:
            cursor = await self.conn.execute(
                "DELETE FROM WeeklyMetrics WHERE GuildId = ?", (guild_id,)
            )
            for table in ("TriviaScore", "TriviaSettings", "GuildSettings"):
                await self.conn.execute(
                    f"DELETE FROM {table} WHERE GuildId = ?", (guild_id,)
                )
            await self.conn.execute(
                "DELETE FROM PendingPurge WHERE GuildId = ?", (guild_id,)
            )
            await self.conn.commit()
        return cursor.rowcount

    async def usage_total(self, guild_id: str, since_day: str, kind: str) -> float:
        """One server's total for one usage measure from ``since_day`` (inclusive)."""
        async with self.conn.execute(
            "SELECT COALESCE(SUM(Amount), 0) AS total FROM UsageLedger "
            "WHERE GuildId = ? AND Day >= ? AND Kind = ?",
            (guild_id, since_day, kind),
        ) as cursor:
            row = await cursor.fetchone()
        return float(row["total"])

    # --- Entitlements (subscriptions, gifts, trials) ------------------------

    _ACTIVE_SQL = (
        "RevokedAt IS NULL AND StartsAt <= :now AND (EndsAt IS NULL OR EndsAt > :now)"
    )

    async def active_entitlements(self, guild_id: str, now: str) -> list[dict[str, Any]]:
        async with self.conn.execute(
            f"SELECT * FROM GuildEntitlement WHERE GuildId = :guild AND {self._ACTIVE_SQL} "
            "ORDER BY Id",
            {"guild": guild_id, "now": now},
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def entitlements_for(self, guild_id: str) -> list[dict[str, Any]]:
        """Every entitlement a server has had, newest first."""
        async with self.conn.execute(
            "SELECT * FROM GuildEntitlement WHERE GuildId = ? ORDER BY Id DESC",
            (guild_id,),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def all_active_entitlements(self, now: str) -> list[dict[str, Any]]:
        """Active entitlements across all servers, soonest-ending first."""
        async with self.conn.execute(
            f"SELECT * FROM GuildEntitlement WHERE {self._ACTIVE_SQL} "
            "ORDER BY EndsAt IS NULL, EndsAt, GuildId",
            {"now": now},
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def get_entitlement(self, entitlement_id: int) -> dict[str, Any] | None:
        async with self.conn.execute(
            "SELECT * FROM GuildEntitlement WHERE Id = ?", (entitlement_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def add_entitlement(
        self,
        guild_id: str,
        tier: str,
        source: str,
        *,
        starts_at: str,
        ends_at: str | None,
        granted_by: str | None,
        reason: str | None,
        external_id: str | None = None,
    ) -> int:
        """Insert an entitlement and its audit entry; return its Id."""
        async with self._write_lock:
            cursor = await self.conn.execute(
                """
                INSERT INTO GuildEntitlement
                    (GuildId, Tier, Source, ExternalId, StartsAt, EndsAt, GrantedBy, Reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (guild_id, tier, source, external_id, starts_at, ends_at, granted_by, reason),
            )
            new_id = int(cursor.lastrowid)
            await self._audit(
                granted_by, "grant", guild_id,
                f"#{new_id} {tier} {source} until {ends_at or 'no end'}: {reason or ''}".strip(),
            )
            await self.conn.commit()
        return new_id

    async def revoke_entitlement(
        self, entitlement_id: int, *, at: str, actor_id: str | None, reason: str | None
    ) -> bool:
        """Mark an entitlement revoked (never deleted); False if already revoked."""
        async with self._write_lock:
            cursor = await self.conn.execute(
                "UPDATE GuildEntitlement SET RevokedAt = ? WHERE Id = ? AND RevokedAt IS NULL",
                (at, entitlement_id),
            )
            if cursor.rowcount == 0:
                return False
            guild_id = await self._entitlement_guild(entitlement_id)
            await self._audit(actor_id, "revoke", guild_id, f"#{entitlement_id}: {reason or ''}".strip())
            await self.conn.commit()
        return True

    async def set_entitlement_end(
        self, entitlement_id: int, ends_at: str | None, *, actor_id: str | None
    ) -> bool:
        """Change when an entitlement ends (None: no end); False if not found."""
        async with self._write_lock:
            cursor = await self.conn.execute(
                "UPDATE GuildEntitlement SET EndsAt = ? WHERE Id = ?",
                (ends_at, entitlement_id),
            )
            if cursor.rowcount == 0:
                return False
            guild_id = await self._entitlement_guild(entitlement_id)
            await self._audit(
                actor_id, "extend", guild_id, f"#{entitlement_id} until {ends_at or 'no end'}"
            )
            await self.conn.commit()
        return True

    async def entitlement_audit(self, guild_id: str, limit: int = 10) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM EntitlementAudit WHERE GuildId = ? ORDER BY Id DESC LIMIT ?",
            (guild_id, limit),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def _entitlement_guild(self, entitlement_id: int) -> str:
        async with self.conn.execute(
            "SELECT GuildId FROM GuildEntitlement WHERE Id = ?", (entitlement_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return str(row["GuildId"]) if row else ""

    async def _audit(self, actor_id: str | None, action: str, guild_id: str, detail: str) -> None:
        # Caller holds the write lock and commits.
        await self.conn.execute(
            "INSERT INTO EntitlementAudit (ActorId, Action, GuildId, Detail) VALUES (?, ?, ?, ?)",
            (actor_id, action, guild_id, detail),
        )

    # --- Server settings (/setup) -------------------------------------------

    async def guild_settings(self, guild_id: str) -> dict[str, Any]:
        """{"trivia_channel_id": str | None} (defaults if unset)."""
        async with self.conn.execute(
            "SELECT TriviaChannelId FROM GuildSettings WHERE GuildId = ?", (guild_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return {"trivia_channel_id": (row["TriviaChannelId"] or None) if row else None}

    async def set_trivia_channel(self, guild_id: str, channel_id: str | None) -> None:
        async with self._write_lock:
            await self.conn.execute(
                """
                INSERT INTO GuildSettings (GuildId, TriviaChannelId, UpdatedAt)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(GuildId) DO UPDATE SET
                    TriviaChannelId = excluded.TriviaChannelId,
                    UpdatedAt = datetime('now')
                """,
                (guild_id, channel_id),
            )
            await self.conn.commit()

    # --- Trivia --------------------------------------------------------------

    async def trivia_settings(self, guild_id: str) -> dict[str, Any]:
        """{"allow_global": bool} (default if unset)."""
        async with self.conn.execute(
            "SELECT AllowGlobal FROM TriviaSettings WHERE GuildId = ?", (guild_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return {"allow_global": bool(row["AllowGlobal"]) if row else True}

    async def set_trivia_settings(self, guild_id: str, *, allow_global: bool) -> None:
        async with self._write_lock:
            await self.conn.execute(
                """
                INSERT INTO TriviaSettings (GuildId, AllowGlobal) VALUES (?, ?)
                ON CONFLICT(GuildId) DO UPDATE SET AllowGlobal = excluded.AllowGlobal
                """,
                (guild_id, int(allow_global)),
            )
            await self.conn.commit()

    async def trivia_announce_channels(self) -> list[tuple[str, str]]:
        """(guild_id, trivia channel) of servers that get cross-server match invitations."""
        async with self.conn.execute(
            """
            SELECT g.GuildId, g.TriviaChannelId FROM GuildSettings g
            LEFT JOIN TriviaSettings t ON t.GuildId = g.GuildId
            WHERE COALESCE(g.TriviaChannelId, '') != '' AND COALESCE(t.AllowGlobal, 1) = 1
            """
        ) as cursor:
            return [(str(r["GuildId"]), str(r["TriviaChannelId"])) async for r in cursor]

    async def record_trivia_match(self, mode: str, players: Sequence[dict[str, Any]]) -> None:
        """Add one finished match to each player's totals.

        Each entry: guild_id, user_id, name, guild_name, points, correct,
        answered, won (bool).
        """
        if mode not in {"server", "global"}:
            raise ValueError(f"Invalid trivia mode: {mode}")
        payload = [
            (
                str(p["guild_id"]), str(p["user_id"]), mode, p["name"], p["guild_name"],
                int(p["points"]), int(p["correct"]), int(p["answered"]), int(bool(p["won"])),
            )
            for p in players
        ]
        if not payload:
            return
        async with self._write_lock:
            await self.conn.executemany(
                """
                INSERT INTO TriviaScore
                    (GuildId, UserId, Mode, DisplayName, GuildName,
                     Points, Correct, Answered, Games, Wins, UpdatedAt)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, datetime('now'))
                ON CONFLICT(GuildId, UserId, Mode) DO UPDATE SET
                    DisplayName = excluded.DisplayName,
                    GuildName = excluded.GuildName,
                    Points = Points + excluded.Points,
                    Correct = Correct + excluded.Correct,
                    Answered = Answered + excluded.Answered,
                    Games = Games + 1,
                    Wins = Wins + excluded.Wins,
                    UpdatedAt = datetime('now')
                """,
                payload,
            )
            await self.conn.commit()

    async def trivia_leaderboard(
        self, *, guild_id: str | None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Top players: one server's (all modes), or cross-server (``guild_id=None``).

        The cross-server board ranks points from cross-server matches, one row
        per player per server, and leaves out servers that turned cross-server
        play off.
        """
        if guild_id is not None:
            sql = """
                SELECT s.UserId,
                       (SELECT x.DisplayName FROM TriviaScore x
                        WHERE x.GuildId = s.GuildId AND x.UserId = s.UserId
                        ORDER BY x.UpdatedAt DESC LIMIT 1) AS DisplayName,
                       '' AS GuildName,
                       sum(Points) AS Points, sum(Correct) AS Correct,
                       sum(Answered) AS Answered, sum(Games) AS Games, sum(Wins) AS Wins
                FROM TriviaScore s WHERE s.GuildId = ?
                GROUP BY s.GuildId, s.UserId
                ORDER BY Points DESC, Correct DESC LIMIT ?
            """
            params: tuple[Any, ...] = (guild_id, limit)
        else:
            sql = """
                SELECT s.UserId, s.DisplayName, s.GuildName, s.Points, s.Correct,
                       s.Answered, s.Games, s.Wins
                FROM TriviaScore s
                LEFT JOIN TriviaSettings t ON t.GuildId = s.GuildId
                WHERE s.Mode = 'global' AND COALESCE(t.AllowGlobal, 1) = 1
                ORDER BY s.Points DESC, s.Correct DESC LIMIT ?
            """
            params = (limit,)
        async with self.conn.execute(sql, params) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def delete_trivia_scores(self, guild_id: str) -> int:
        """Delete a server's trivia scores (settings are kept); return rows removed."""
        async with self._write_lock:
            cursor = await self.conn.execute(
                "DELETE FROM TriviaScore WHERE GuildId = ?", (guild_id,)
            )
            await self.conn.commit()
        return cursor.rowcount
