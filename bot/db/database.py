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
    WeekStart   TEXT    NOT NULL,
    PlayerName  TEXT    NOT NULL,
    MetricType  TEXT    NOT NULL,
    Value       REAL    NOT NULL,
    UpdatedAt   TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (WeekStart, PlayerName, MetricType)
);

CREATE INDEX IF NOT EXISTS idx_weekly_player
    ON WeeklyMetrics (PlayerName, MetricType, WeekStart);

CREATE INDEX IF NOT EXISTS idx_weekly_week
    ON WeeklyMetrics (WeekStart, MetricType);
"""


class Database:
    """Async SQLite helper around the WeeklyMetrics fact table."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")
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

    async def upsert_metric(
        self,
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

        await self.conn.execute(
            """
            INSERT INTO WeeklyMetrics (WeekStart, PlayerName, MetricType, Value, UpdatedAt)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(WeekStart, PlayerName, MetricType) DO UPDATE SET
                Value = excluded.Value,
                UpdatedAt = datetime('now')
            """,
            (week_start, player_name, metric_type, float(value)),
        )
        await self.conn.commit()

    async def upsert_metrics(
        self,
        rows: Iterable[tuple[str, str, str, float]],
    ) -> int:
        """Bulk upsert (week, player, metric_type, value). Returns row count."""
        payload = [
            (week, player.strip(), metric, float(value))
            for week, player, metric, value in rows
            if player and player.strip() and metric in METRIC_TYPES
        ]
        if not payload:
            return 0

        await self.conn.executemany(
            """
            INSERT INTO WeeklyMetrics (WeekStart, PlayerName, MetricType, Value, UpdatedAt)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(WeekStart, PlayerName, MetricType) DO UPDATE SET
                Value = excluded.Value,
                UpdatedAt = datetime('now')
            """,
            payload,
        )
        await self.conn.commit()
        return len(payload)

    async def get_player_metrics(
        self,
        player_name: str,
        metric_type: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["PlayerName = ? COLLATE NOCASE"]
        params: list[Any] = [player_name]
        if metric_type:
            clauses.append("MetricType = ?")
            params.append(metric_type)

        sql = f"""
            SELECT WeekStart, PlayerName, MetricType, Value, UpdatedAt
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
        week_start: str,
        metric_type: str | None = None,
    ) -> list[dict[str, Any]]:
        if metric_type:
            sql = """
                SELECT WeekStart, PlayerName, MetricType, Value, UpdatedAt
                FROM WeeklyMetrics
                WHERE WeekStart = ? AND MetricType = ?
                ORDER BY Value DESC, PlayerName
            """
            params: Sequence[Any] = (week_start, metric_type)
        else:
            sql = """
                SELECT WeekStart, PlayerName, MetricType, Value, UpdatedAt
                FROM WeeklyMetrics
                WHERE WeekStart = ?
                ORDER BY MetricType, Value DESC, PlayerName
            """
            params = (week_start,)

        async with self.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_trends(
        self,
        metric_type: str,
        weeks: int = 8,
        player_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent weekly values for a metric, optionally for one player."""
        week_filter_sql = """
            SELECT DISTINCT WeekStart
            FROM WeeklyMetrics
            WHERE MetricType = ?
            ORDER BY WeekStart DESC
            LIMIT ?
        """
        async with self.conn.execute(week_filter_sql, (metric_type, weeks)) as cursor:
            week_rows = await cursor.fetchall()
        week_list = [r["WeekStart"] for r in week_rows]
        if not week_list:
            return []

        placeholders = ",".join("?" * len(week_list))
        clauses = [f"WeekStart IN ({placeholders})", "MetricType = ?"]
        params: list[Any] = [*week_list, metric_type]
        if player_name:
            clauses.append("PlayerName = ? COLLATE NOCASE")
            params.append(player_name)

        sql = f"""
            SELECT WeekStart, PlayerName, MetricType, Value
            FROM WeeklyMetrics
            WHERE {' AND '.join(clauses)}
            ORDER BY WeekStart ASC, PlayerName
        """
        async with self.conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_growth_rates(
        self,
        metric_type: str,
        weeks: int = 4,
    ) -> list[dict[str, Any]]:
        """
        Compute growth from earliest to latest value within the last N weeks.

        Returns list of dicts:
          PlayerName, FirstWeek, LastWeek, FirstValue, LastValue, Delta, GrowthPct
        """
        trends = await self.get_trends(metric_type, weeks=weeks)
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
        metric_type: str,
        week_start: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        if week_start is None:
            async with self.conn.execute(
                """
                SELECT WeekStart FROM WeeklyMetrics
                WHERE MetricType = ?
                ORDER BY WeekStart DESC LIMIT 1
                """,
                (metric_type,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return []
            week_start = row["WeekStart"]

        async with self.conn.execute(
            """
            SELECT WeekStart, PlayerName, MetricType, Value
            FROM WeeklyMetrics
            WHERE WeekStart = ? AND MetricType = ?
            ORDER BY Value DESC, PlayerName
            LIMIT ?
            """,
            (week_start, metric_type, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def list_weeks(self, limit: int = 52) -> list[str]:
        async with self.conn.execute(
            """
            SELECT DISTINCT WeekStart FROM WeeklyMetrics
            ORDER BY WeekStart DESC LIMIT ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [r["WeekStart"] for r in rows]

    async def list_players(self) -> list[str]:
        async with self.conn.execute(
            """
            SELECT DISTINCT PlayerName FROM WeeklyMetrics
            ORDER BY PlayerName COLLATE NOCASE
            """
        ) as cursor:
            rows = await cursor.fetchall()
        return [r["PlayerName"] for r in rows]

    async def stats(self) -> dict[str, Any]:
        async with self.conn.execute(
            "SELECT COUNT(*) AS c FROM WeeklyMetrics"
        ) as cursor:
            total = (await cursor.fetchone())["c"]
        async with self.conn.execute(
            "SELECT COUNT(DISTINCT PlayerName) AS c FROM WeeklyMetrics"
        ) as cursor:
            players = (await cursor.fetchone())["c"]
        async with self.conn.execute(
            "SELECT COUNT(DISTINCT WeekStart) AS c FROM WeeklyMetrics"
        ) as cursor:
            weeks = (await cursor.fetchone())["c"]
        async with self.conn.execute(
            """
            SELECT MetricType, COUNT(*) AS c
            FROM WeeklyMetrics GROUP BY MetricType
            """
        ) as cursor:
            by_metric = {r["MetricType"]: r["c"] for r in await cursor.fetchall()}
        return {
            "rows": total,
            "players": players,
            "weeks": weeks,
            "by_metric": by_metric,
            "path": str(self.path),
        }
