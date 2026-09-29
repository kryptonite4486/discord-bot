"""SQLite datastore for WeeklyMetrics."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

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
        channel_id: str | None,
        include_unassigned: bool,
        guild_param_index: int = 1,
    ) -> tuple[str, list[Any]]:
        """
        Build ChannelId filter for Phase 1.

        - channel_id set + include_unassigned: ChannelId IN (channel, '')
        - channel_id set + not include_unassigned: ChannelId = channel
        - channel_id None: no channel filter (guild-wide)
        """
        if channel_id is None:
            return "", []
        if include_unassigned:
            return (
                f" AND (ChannelId = ? OR ChannelId = ?)",
                [channel_id, UNASSIGNED_CHANNEL_ID],
            )
        return " AND ChannelId = ?", [channel_id]

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

    async def get_player_metrics(
        self,
        guild_id: str,
        player_name: str,
        metric_type: str | None = None,
        limit: int | None = None,
        *,
        channel_id: str | None = None,
        include_unassigned: bool = True,
    ) -> list[dict[str, Any]]:
        clauses = ["GuildId = ?", "PlayerName = ? COLLATE NOCASE"]
        params: list[Any] = [guild_id, player_name]
        extra_sql, extra_params = self._channel_scope_sql(
            channel_id=channel_id, include_unassigned=include_unassigned
        )
        # _channel_scope_sql returns AND ... — splice into WHERE
        if metric_type:
            clauses.append("MetricType = ?")
            params.append(metric_type)
        where = " AND ".join(clauses) + extra_sql
        params.extend(extra_params)

        sql = f"""
            SELECT GuildId, ChannelId, WeekStart, PlayerName, MetricType, Value, UpdatedAt
            FROM WeeklyMetrics
            WHERE {where}
            ORDER BY WeekStart DESC, MetricType
        """
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
        include_unassigned: bool = True,
    ) -> list[dict[str, Any]]:
        extra_sql, extra_params = self._channel_scope_sql(
            channel_id=channel_id, include_unassigned=include_unassigned
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
        include_unassigned: bool = True,
    ) -> list[dict[str, Any]]:
        """Return recent weekly values for a metric, optionally for one player."""
        extra_sql, extra_params = self._channel_scope_sql(
            channel_id=channel_id, include_unassigned=include_unassigned
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
        include_unassigned: bool = True,
    ) -> list[dict[str, Any]]:
        trends = await self.get_trends(
            guild_id,
            metric_type,
            weeks=weeks,
            channel_id=channel_id,
            include_unassigned=include_unassigned,
        )
        by_player: dict[str, list[dict[str, Any]]] = {}
        for row in trends:
            by_player.setdefault(row["PlayerName"], []).append(row)

        results: list[dict[str, Any]] = []
        for player, series in by_player.items():
            if len(series) < 2:
                continue
            first, last = series[0], series[-1]
            delta = last["Value"] - first["Value"]
            growth = (delta / first["Value"] * 100.0) if first["Value"] else None
            results.append(
                {
                    "PlayerName": player,
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
        include_unassigned: bool = True,
    ) -> list[dict[str, Any]]:
        extra_sql, extra_params = self._channel_scope_sql(
            channel_id=channel_id, include_unassigned=include_unassigned
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
        include_unassigned: bool = True,
    ) -> list[str]:
        extra_sql, extra_params = self._channel_scope_sql(
            channel_id=channel_id, include_unassigned=include_unassigned
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
        include_unassigned: bool = True,
    ) -> list[str]:
        extra_sql, extra_params = self._channel_scope_sql(
            channel_id=channel_id, include_unassigned=include_unassigned
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
        include_unassigned: bool = True,
    ) -> dict[str, Any]:
        extra_sql, extra_params = self._channel_scope_sql(
            channel_id=channel_id, include_unassigned=include_unassigned
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
