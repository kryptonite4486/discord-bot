"""Markdown and text formatters for report output."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from bot.utils import parsing as parsing_utils


def _channel_label(channel_id: str | None) -> str:
    if not channel_id:
        return "(unassigned)"
    return str(channel_id)


def markdown_table(headers: list[str], rows: Iterable[list[str]]) -> str:
    header = "| " + " | ".join(headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join([header, sep, *body])


def week_summary_text(
    week: str,
    rows: list[dict[str, Any]],
    *,
    show_channel: bool = False,
) -> str:
    if not rows:
        return f"No metrics found for week **{week}**."

    by_metric: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_metric[row["MetricType"]].append(row)

    # Avoid backticks in titles: reports are wrapped in ```md fences,
    # and nested ticks break Discord's code-block parsing.
    scope_note = " (all channels)" if show_channel else ""
    parts = [f"## Weekly Summary — {week}{scope_note}", ""]
    for metric, items in sorted(by_metric.items()):
        parts.append(f"### {metric} ({len(items)} rows)")
        if show_channel:
            table_rows = [
                [
                    str(i),
                    r["PlayerName"],
                    _channel_label(r.get("ChannelId")),
                    parsing_utils.format_value(metric, r["Value"]),
                ]
                for i, r in enumerate(items, start=1)
            ]
            parts.append(
                markdown_table(["#", "Player", "Channel", "Value"], table_rows)
            )
        else:
            table_rows = [
                [
                    str(i),
                    r["PlayerName"],
                    parsing_utils.format_value(metric, r["Value"]),
                ]
                for i, r in enumerate(items, start=1)
            ]
            parts.append(markdown_table(["#", "Player", "Value"], table_rows))
        parts.append("")
    return "\n".join(parts).strip()


def player_report_text(
    player: str,
    rows: list[dict[str, Any]],
    *,
    show_channel: bool = False,
) -> str:
    if not rows:
        return f"No metrics found for player **{player}**."

    by_metric: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_metric[row["MetricType"]].append(row)

    parts = [f"## Player Report — **{player}**", ""]
    for metric, items in sorted(by_metric.items()):
        # rows come DESC by week; reverse for chronological table
        chronological = list(reversed(items))
        if show_channel:
            table_rows = [
                [
                    r["WeekStart"],
                    _channel_label(r.get("ChannelId")),
                    parsing_utils.format_value(metric, r["Value"]),
                ]
                for r in chronological[-12:]
            ]
            parts.append(f"### {metric}")
            parts.append(markdown_table(["Week", "Channel", "Value"], table_rows))
        else:
            table_rows = [
                [r["WeekStart"], parsing_utils.format_value(metric, r["Value"])]
                for r in chronological[-12:]
            ]
            parts.append(f"### {metric}")
            parts.append(markdown_table(["Week", "Value"], table_rows))
        if len(chronological) >= 2 and not show_channel:
            delta = chronological[-1]["Value"] - chronological[0]["Value"]
            parts.append(
                f"Change ({chronological[0]['WeekStart']} → "
                f"{chronological[-1]['WeekStart']}): "
                f"**{parsing_utils.format_value(metric, delta)}** "
                f"({'+' if delta >= 0 else ''}{delta:,.1f})"
            )
        parts.append("")
    return "\n".join(parts).strip()


def _growth_table_rows(
    metric: str,
    rows: list[dict[str, Any]],
    *,
    show_channel: bool = False,
) -> list[list[str]]:
    table_rows: list[list[str]] = []
    for r in rows:
        growth = (
            f"{r['GrowthPct']:+.1f}%"
            if r["GrowthPct"] is not None
            else "n/a"
        )
        cells = [r["PlayerName"]]
        if show_channel:
            cells.append(_channel_label(r.get("ChannelId")))
        cells.extend(
            [
                parsing_utils.format_value(metric, r["FirstValue"]),
                parsing_utils.format_value(metric, r["LastValue"]),
                parsing_utils.format_value(metric, r["Delta"]),
                growth,
            ]
        )
        table_rows.append(cells)
    return table_rows


def growth_report_text(
    metric: str,
    weeks: int,
    rows: list[dict[str, Any]],
    *,
    top_n: int = 15,
    bottom_n: int = 15,
    show_channel: bool = False,
) -> str:
    """Discord summary: top N and bottom N by growth (rows sorted desc)."""
    if not rows:
        return f"No growth data for {metric} over the last {weeks} weeks."

    headers = (
        ["Player", "Channel", "First", "Last", "Δ", "Growth"]
        if show_channel
        else ["Player", "First", "Last", "Δ", "Growth"]
    )
    scope_note = " (all channels)" if show_channel else ""
    parts = [
        f"## Growth Report — {metric} ({weeks} weeks){scope_note}",
        f"Series with enough history: {len(rows)}",
        "",
    ]

    if len(rows) <= top_n:
        parts.append(f"### All ({len(rows)})")
        parts.append(
            markdown_table(
                headers, _growth_table_rows(metric, rows, show_channel=show_channel)
            )
        )
        return "\n".join(parts).strip()

    top = rows[:top_n]
    bottom = rows[-bottom_n:]
    parts.append(f"### Top {len(top)} (highest growth)")
    parts.append(
        markdown_table(
            headers, _growth_table_rows(metric, top, show_channel=show_channel)
        )
    )
    parts.append("")
    parts.append(f"### Bottom {len(bottom)} (lowest growth)")
    parts.append(
        markdown_table(
            headers, _growth_table_rows(metric, bottom, show_channel=show_channel)
        )
    )
    if len(rows) < top_n + bottom_n:
        parts.append("")
        parts.append(
            f"(N={len(rows)} < {top_n + bottom_n}: top and bottom lists overlap.)"
        )
    parts.append("")
    parts.append("Full ranked list attached as a markdown file.")
    return "\n".join(parts).strip()


def growth_report_full_markdown(
    metric: str,
    weeks: int,
    rows: list[dict[str, Any]],
    *,
    show_channel: bool = False,
) -> str:
    """Complete growth ranking for download as a .md file."""
    if not rows:
        return f"# Growth Report — {metric} ({weeks} weeks)\n\nNo data.\n"

    headers = (
        ["#", "Player", "Channel", "First", "Last", "Δ", "Growth"]
        if show_channel
        else ["#", "Player", "First", "Last", "Δ", "Growth"]
    )
    table_rows: list[list[str]] = []
    for i, r in enumerate(rows, start=1):
        growth = (
            f"{r['GrowthPct']:+.1f}%"
            if r["GrowthPct"] is not None
            else "n/a"
        )
        cells = [str(i), r["PlayerName"]]
        if show_channel:
            cells.append(_channel_label(r.get("ChannelId")))
        cells.extend(
            [
                parsing_utils.format_value(metric, r["FirstValue"]),
                parsing_utils.format_value(metric, r["LastValue"]),
                parsing_utils.format_value(metric, r["Delta"]),
                growth,
            ]
        )
        table_rows.append(cells)

    first_week = rows[0].get("FirstWeek", "?")
    last_week = rows[0].get("LastWeek", "?")
    parts = [
        f"# Growth Report — {metric}",
        f"Lookback: {weeks} weeks ({first_week} → {last_week})",
        f"Series: {len(rows)}",
        "",
        markdown_table(headers, table_rows),
        "",
    ]
    return "\n".join(parts)


def leaderboard_text(
    metric: str,
    week: str,
    rows: list[dict[str, Any]],
    *,
    show_channel: bool = False,
) -> str:
    if not rows:
        return f"No leaderboard data for **{metric}** (week {week})."

    scope_note = " (all channels)" if show_channel else ""
    parts = [f"## Leaderboard — {metric} ({week}){scope_note}", ""]
    if show_channel:
        table_rows = [
            [
                str(i),
                r["PlayerName"],
                _channel_label(r.get("ChannelId")),
                parsing_utils.format_value(metric, r["Value"]),
            ]
            for i, r in enumerate(rows, start=1)
        ]
        parts.append(markdown_table(["#", "Player", "Channel", "Value"], table_rows))
    else:
        table_rows = [
            [str(i), r["PlayerName"], parsing_utils.format_value(metric, r["Value"])]
            for i, r in enumerate(rows, start=1)
        ]
        parts.append(markdown_table(["#", "Player", "Value"], table_rows))
    return "\n".join(parts)


def trend_summary_text(
    metric: str,
    rows: list[dict[str, Any]],
    top_n: int = 10,
    *,
    show_channel: bool = False,
) -> str:
    if not rows:
        return f"No trend data for **{metric}**."

    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (
            row["PlayerName"],
            str(row.get("ChannelId") or "") if show_channel else "",
        )
        by_key[key].append(row)

    # Rank by latest value
    ranked = sorted(
        by_key.items(),
        key=lambda kv: kv[1][-1]["Value"] if kv[1] else 0,
        reverse=True,
    )[:top_n]

    scope_note = " (all channels)" if show_channel else ""
    parts = [f"## Trend — {metric} (top {len(ranked)}){scope_note}", ""]
    for (player, ch_id), series in ranked:
        spark = " → ".join(parsing_utils.format_value(metric, r["Value"]) for r in series)
        label = (
            f"**{player}** [{_channel_label(ch_id)}]"
            if show_channel
            else f"**{player}**"
        )
        parts.append(f"{label}: {spark}")
    return "\n".join(parts)
