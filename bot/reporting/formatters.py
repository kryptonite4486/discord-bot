"""Markdown and text formatters for report output."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from bot.utils import parsing as parsing_utils


def _channel_label(
    channel_id: str | None,
    channel_names: dict[str, str] | None = None,
) -> str:
    if not channel_id:
        return "(unassigned)"
    cid = str(channel_id)
    if channel_names and cid in channel_names:
        return channel_names[cid]
    return cid


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
    channel_names: dict[str, str] | None = None,
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
                    _channel_label(r.get("ChannelId"), channel_names),
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
    channel_names: dict[str, str] | None = None,
) -> str:
    if not rows:
        return f"No metrics found for player **{player}**."

    by_metric: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_metric[row["MetricType"]].append(row)

    def _rank_cell(r: dict[str, Any]) -> str:
        rank = r.get("Rank")
        pop = r.get("Population")
        if rank is None or pop is None:
            return "—"
        return f"{int(rank)}/{int(pop)}"

    parts = [f"## Player Report — **{player}**", ""]
    for metric, items in sorted(by_metric.items()):
        chronological = sorted(items, key=lambda r: (r["WeekStart"], r.get("ChannelId") or ""))
        if show_channel:
            table_rows = [
                [
                    r["WeekStart"],
                    _channel_label(r.get("ChannelId"), channel_names),
                    _rank_cell(r),
                    parsing_utils.format_value(metric, r["Value"]),
                ]
                for r in chronological[-12:]
            ]
            parts.append(f"### {metric}")
            parts.append(
                markdown_table(["Week", "Channel", "Rank", "Value"], table_rows)
            )
        else:
            shown = chronological[-12:]
            table_rows = [
                [
                    r["WeekStart"],
                    _rank_cell(r),
                    parsing_utils.format_value(metric, r["Value"]),
                ]
                for r in shown
            ]
            parts.append(f"### {metric}")
            parts.append(markdown_table(["Week", "Rank", "Value"], table_rows))
            if len(shown) >= 2:
                if metric in {"TechContribution", "VersusPoints"}:
                    avg = sum(r["Value"] for r in shown) / len(shown)
                    parts.append(
                        f"Average ({shown[0]['WeekStart']} → "
                        f"{shown[-1]['WeekStart']}, {len(shown)} weeks): "
                        f"**{parsing_utils.format_value(metric, avg)}** "
                        f"({avg:,.1f})"
                    )
                else:
                    delta = shown[-1]["Value"] - shown[0]["Value"]
                    parts.append(
                        f"Change ({shown[0]['WeekStart']} → "
                        f"{shown[-1]['WeekStart']}): "
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
    channel_names: dict[str, str] | None = None,
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
            cells.append(_channel_label(r.get("ChannelId"), channel_names))
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
    channel_names: dict[str, str] | None = None,
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
                headers,
                _growth_table_rows(
                    metric, rows, show_channel=show_channel, channel_names=channel_names
                ),
            )
        )
        return "\n".join(parts).strip()

    top = rows[:top_n]
    bottom = rows[-bottom_n:]
    parts.append(f"### Top {len(top)} (highest growth)")
    parts.append(
        markdown_table(
            headers,
            _growth_table_rows(
                metric, top, show_channel=show_channel, channel_names=channel_names
            ),
        )
    )
    parts.append("")
    parts.append(f"### Bottom {len(bottom)} (lowest growth)")
    parts.append(
        markdown_table(
            headers,
            _growth_table_rows(
                metric, bottom, show_channel=show_channel, channel_names=channel_names
            ),
        )
    )
    if len(rows) < top_n + bottom_n:
        parts.append("")
        parts.append(
            f"(N={len(rows)} < {top_n + bottom_n}: top and bottom lists overlap.)"
        )
    parts.append("")
    parts.append("Full ranked list attached as a CSV file.")
    return "\n".join(parts).strip()


def _csv_cell(value: Any) -> str:
    """Serialize a CSV field with no commas (delimiter-only commas in the file)."""
    text = str(value if value is not None else "")
    return (
        text.replace(",", "")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()
    )


def growth_report_full_csv(
    metric: str,
    weeks: int,
    rows: list[dict[str, Any]],
    *,
    show_channel: bool = False,
    channel_names: dict[str, str] | None = None,
) -> str:
    """Complete growth ranking for download as a .csv file (comma delimiter only)."""
    if not rows:
        return "Rank,Player,First,Last,Delta,GrowthPct\n"

    headers = ["Rank", "Player"]
    if show_channel:
        headers.append("Channel")
    headers.extend(["First", "Last", "Delta", "GrowthPct", "FirstWeek", "LastWeek"])

    lines = [",".join(headers)]
    for i, r in enumerate(rows, start=1):
        growth = (
            f"{r['GrowthPct']:+.1f}%"
            if r["GrowthPct"] is not None
            else "n/a"
        )
        cells = [
            _csv_cell(i),
            _csv_cell(r["PlayerName"]),
        ]
        if show_channel:
            cells.append(
                _csv_cell(_channel_label(r.get("ChannelId"), channel_names))
            )
        cells.extend(
            [
                _csv_cell(parsing_utils.format_value(metric, r["FirstValue"])),
                _csv_cell(parsing_utils.format_value(metric, r["LastValue"])),
                _csv_cell(parsing_utils.format_value(metric, r["Delta"])),
                _csv_cell(growth),
                _csv_cell(r.get("FirstWeek", "")),
                _csv_cell(r.get("LastWeek", "")),
            ]
        )
        lines.append(",".join(cells))
    return "\n".join(lines) + "\n"


def leaderboard_text(
    metric: str,
    week: str,
    rows: list[dict[str, Any]],
    *,
    show_channel: bool = False,
    channel_names: dict[str, str] | None = None,
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
                _channel_label(r.get("ChannelId"), channel_names),
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
    channel_names: dict[str, str] | None = None,
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
            f"**{player}** [{_channel_label(ch_id, channel_names)}]"
            if show_channel
            else f"**{player}**"
        )
        parts.append(f"{label}: {spark}")
    return "\n".join(parts)
