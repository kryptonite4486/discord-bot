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
