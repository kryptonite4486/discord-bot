"""Markdown and text formatters for report output."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from bot.utils.parsing import format_value


def markdown_table(headers: list[str], rows: Iterable[list[str]]) -> str:
    header = "| " + " | ".join(headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join([header, sep, *body])


def week_summary_text(week: str, rows: list[dict[str, Any]]) -> str:
    if not rows:
        return f"No metrics found for week **{week}**."

    by_metric: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_metric[row["MetricType"]].append(row)

    parts = [f"## Weekly Summary — `{week}`", ""]
    for metric, items in sorted(by_metric.items()):
        parts.append(f"### {metric} ({len(items)} players)")
        table_rows = [
            [
                str(i),
                r["PlayerName"],
                format_value(metric, r["Value"]),
            ]
            for i, r in enumerate(items[:15], start=1)
        ]
        parts.append(markdown_table(["#", "Player", "Value"], table_rows))
        if len(items) > 15:
            parts.append(f"_…and {len(items) - 15} more_")
        parts.append("")
    return "\n".join(parts).strip()


def player_report_text(player: str, rows: list[dict[str, Any]]) -> str:
    if not rows:
        return f"No metrics found for player **{player}**."

    by_metric: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_metric[row["MetricType"]].append(row)

    parts = [f"## Player Report — **{player}**", ""]
    for metric, items in sorted(by_metric.items()):
        # rows come DESC by week; reverse for chronological table
        chronological = list(reversed(items))
        table_rows = [
            [r["WeekStart"], format_value(metric, r["Value"])]
            for r in chronological[-12:]
        ]
        parts.append(f"### {metric}")
        parts.append(markdown_table(["Week", "Value"], table_rows))
        if len(chronological) >= 2:
            delta = chronological[-1]["Value"] - chronological[0]["Value"]
            parts.append(
                f"Change ({chronological[0]['WeekStart']} → "
                f"{chronological[-1]['WeekStart']}): "
                f"**{format_value(metric, delta)}** "
                f"({'+' if delta >= 0 else ''}{delta:,.1f})"
            )
        parts.append("")
    return "\n".join(parts).strip()


def growth_report_text(
    metric: str,
    weeks: int,
    rows: list[dict[str, Any]],
) -> str:
    if not rows:
        return f"No growth data for **{metric}** over the last {weeks} weeks."

    parts = [f"## Growth Report — {metric} ({weeks} weeks)", ""]
    table_rows = []
    for r in rows[:25]:
        growth = (
            f"{r['GrowthPct']:+.1f}%"
            if r["GrowthPct"] is not None
            else "n/a"
        )
        table_rows.append(
            [
                r["PlayerName"],
                format_value(metric, r["FirstValue"]),
                format_value(metric, r["LastValue"]),
                format_value(metric, r["Delta"]),
                growth,
            ]
        )
    parts.append(
        markdown_table(
            ["Player", "First", "Last", "Δ", "Growth"],
            table_rows,
        )
    )
    return "\n".join(parts)


def leaderboard_text(
    metric: str,
    week: str,
    rows: list[dict[str, Any]],
) -> str:
    if not rows:
        return f"No leaderboard data for **{metric}** (week {week})."

    parts = [f"## Leaderboard — {metric} (`{week}`)", ""]
    table_rows = [
        [str(i), r["PlayerName"], format_value(metric, r["Value"])]
        for i, r in enumerate(rows, start=1)
    ]
    parts.append(markdown_table(["#", "Player", "Value"], table_rows))
    return "\n".join(parts)


def trend_summary_text(
    metric: str,
    rows: list[dict[str, Any]],
    top_n: int = 10,
) -> str:
    if not rows:
        return f"No trend data for **{metric}**."

    by_player: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_player[row["PlayerName"]].append(row)

    # Rank by latest value
    ranked = sorted(
        by_player.items(),
        key=lambda kv: kv[1][-1]["Value"] if kv[1] else 0,
        reverse=True,
    )[:top_n]

    parts = [f"## Trend — {metric} (top {len(ranked)})", ""]
    for player, series in ranked:
        spark = " → ".join(format_value(metric, r["Value"]) for r in series)
        parts.append(f"**{player}**: {spark}")
    return "\n".join(parts)
