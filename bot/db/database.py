"""SQLite datastore for WeeklyMetrics."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable, Sequence

import aiosqlite

from bot.config import METRIC_TYPES

log = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS WeeklyMetrics (
    GuildId     TEXT    NOT NULL,
    WeekStart   TEXT    NOT NULL,
    PlayerName  TEXT    NOT NULL,
    MetricType  TEXT    NOT NULL,
    Value       REAL    NOT NULL,
    UpdatedAt   TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (GuildId, WeekStart, PlayerName, MetricType)
);

CREATE INDEX IF NOT EXISTS idx_weekly_player
    ON WeeklyMetrics (GuildId, PlayerName, MetricType, WeekStart);

CREATE INDEX IF NOT EXISTS idx_weekly_week
    ON WeeklyMetrics (GuildId, WeekStart, MetricType);
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
        if not cols:
            return
        if "GuildId" in cols:
            return

        guild_id = (self.legacy_guild_id or "").strip() or LEGACY_GUILD_FALLBACK
        if not (self.legacy_guild_id or "").strip():
            log.warning(
                "Migrating WeeklyMetrics without LEGACY_GUILD_ID; "
                "assigning existing rows to GuildId=%r. "
                "Set LEGACY_GUILD_ID to your Discord server snowflake "
                "before startup to preserve ownership of historical data.",
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

    async def upsert_metric(
        self,
        guild_id: str,
        week_start: str,
        player_name: str,
        metric_type: str,
        value: float,
    ) -> None:
        if metric_type not in METRIC_TYPES:
            raise ValueError(f"Invalid MetricType: {metric_type}")
        player_name = player_name.strip()
        if not player_name:
            raise ValueError("PlayerName cannot be empty")
        if not guild_id:
            raise ValueError("GuildId cannot be empty")

        await self.conn.execute(
            """
            INSERT INTO WeeklyMetrics
                (GuildId, WeekStart, PlayerName, MetricType, Value, UpdatedAt)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(GuildId, WeekStart, PlayerName, MetricType) DO UPDATE SET
                Value = excluded.Value,
                UpdatedAt = datetime('now')
            """,
            (guild_id, week_start, player_name, metric_type, float(value)),
        )
        await self.conn.commit()

    async def upsert_metrics(
        self,
        guild_id: str,
        rows: Iterable[tuple[str, str, str, float]],
    ) -> int:
        """Bulk upsert (week, player, metric_type, value) for one guild. Returns row count."""
        if not guild_id:
            raise ValueError("GuildId cannot be empty")
        payload = [
            (guild_id, week, player.strip(), metric, float(value))
            for week, player, metric, value in rows
            if player and player.strip() and metric in METRIC_TYPES
        ]
        if not payload:
            return 0

        await self.conn.executemany(
            """
            INSERT INTO WeeklyMetrics
                (GuildId, WeekStart, PlayerName, MetricType, Value, UpdatedAt)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(GuildId, WeekStart, PlayerName, MetricType) DO UPDATE SET
                Value = excluded.Value,
                UpdatedAt = datetime('now')
            """,
            payload,
        )
        await self.conn.commit()
        return len(payload)

    async def get_player_metrics(
        self,
        guild_id: str,
        player_name: str,
        metric_type: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["GuildId = ?", "PlayerName = ? COLLATE NOCASE"]
        params: list[Any] = [guild_id, player_name]
        if metric_type:
            clauses.append("MetricType = ?")
            params.append(metric_type)

        sql = f"""
            SELECT GuildId, WeekStart, PlayerName, MetricType, Value, UpdatedAt
            FROM WeeklyMetrics
            WHERE {' AND '.join(clauses)}
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
    ) -> list[dict[str, Any]]:
        if metric_type:
            sql = """
                SELECT GuildId, WeekStart, PlayerName, MetricType, Value, UpdatedAt
                FROM WeeklyMetrics
                WHERE GuildId = ? AND WeekStart = ? AND MetricType = ?
                ORDER BY Value DESC, PlayerName
            """
            params: Sequence[Any] = (guild_id, week_start, metric_type)
        else:
            sql = """
                SELECT GuildId, WeekStart, PlayerName, MetricType, Value, UpdatedAt
                FROM WeeklyMetrics
                WHERE GuildId = ? AND WeekStart = ?
                ORDER BY MetricType, Value DESC, PlayerName
            """
            params = (guild_id, week_start)

        async with self.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_trends(
        self,
        guild_id: str,
        metric_type: str,
        weeks: int = 8,
        player_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent weekly values for a metric, optionally for one player."""
        week_filter_sql = """
            SELECT DISTINCT WeekStart
            FROM WeeklyMetrics
            WHERE GuildId = ? AND MetricType = ?
            ORDER BY WeekStart DESC
            LIMIT ?
        """
        async with self.conn.execute(
            week_filter_sql, (guild_id, metric_type, weeks)
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

        sql = f"""
            SELECT GuildId, WeekStart, PlayerName, MetricType, Value
            FROM WeeklyMetrics
            WHERE {' AND '.join(clauses)}
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
    ) -> list[dict[str, Any]]:
        """
        Compute growth from earliest to latest value within the last N weeks.

        Returns list of dicts:
          PlayerName, FirstWeek, LastWeek, FirstValue, LastValue, Delta, GrowthPct
        """
        trends = await self.get_trends(guild_id, metric_type, weeks=weeks)
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
    ) -> list[dict[str, Any]]:
        if week_start is None:
            async with self.conn.execute(
                """
                SELECT WeekStart FROM WeeklyMetrics
                WHERE GuildId = ? AND MetricType = ?
                ORDER BY WeekStart DESC LIMIT 1
                """,
                (guild_id, metric_type),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return []
            week_start = row["WeekStart"]

        sql = """
            SELECT GuildId, WeekStart, PlayerName, MetricType, Value
            FROM WeeklyMetrics
            WHERE GuildId = ? AND WeekStart = ? AND MetricType = ?
            ORDER BY Value DESC, PlayerName
        """
        params: list[Any] = [guild_id, week_start, metric_type]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        async with self.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def list_weeks(self, guild_id: str, limit: int = 52) -> list[str]:
        async with self.conn.execute(
            """
            SELECT DISTINCT WeekStart FROM WeeklyMetrics
            WHERE GuildId = ?
            ORDER BY WeekStart DESC LIMIT ?
            """,
            (guild_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [r["WeekStart"] for r in rows]

    async def list_players(self, guild_id: str) -> list[str]:
        async with self.conn.execute(
            """
            SELECT DISTINCT PlayerName FROM WeeklyMetrics
            WHERE GuildId = ?
            ORDER BY PlayerName COLLATE NOCASE
            """,
            (guild_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [r["PlayerName"] for r in rows]

    async def stats(self, guild_id: str) -> dict[str, Any]:
        async with self.conn.execute(
            "SELECT COUNT(*) AS c FROM WeeklyMetrics WHERE GuildId = ?",
            (guild_id,),
        ) as cursor:
            total = (await cursor.fetchone())["c"]
        async with self.conn.execute(
            """
            SELECT COUNT(DISTINCT PlayerName) AS c FROM WeeklyMetrics
            WHERE GuildId = ?
            """,
            (guild_id,),
        ) as cursor:
            players = (await cursor.fetchone())["c"]
        async with self.conn.execute(
            """
            SELECT COUNT(DISTINCT WeekStart) AS c FROM WeeklyMetrics
            WHERE GuildId = ?
            """,
            (guild_id,),
        ) as cursor:
            weeks = (await cursor.fetchone())["c"]
        async with self.conn.execute(
            """
            SELECT MetricType, COUNT(*) AS c
            FROM WeeklyMetrics
            WHERE GuildId = ?
            GROUP BY MetricType
            """,
            (guild_id,),
        ) as cursor:
            by_metric = {r["MetricType"]: r["c"] for r in await cursor.fetchall()}
        return {
            "rows": total,
            "players": players,
            "weeks": weeks,
            "by_metric": by_metric,
            "path": str(self.path),
            "guild_id": guild_id,
        }
