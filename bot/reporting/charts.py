"""Matplotlib chart generation for reports."""

from __future__ import annotations

import io
from collections import defaultdict
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def _fig_to_png(fig: plt.Figure) -> io.BytesIO:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=140, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


def player_trend_chart(
    player: str,
    rows: list[dict[str, Any]],
) -> io.BytesIO | None:
    """Multi-metric trend chart for one player."""
    if not rows:
        return None

    by_metric: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_metric[row["MetricType"]].append(row)

    metrics = sorted(by_metric.keys())
    fig, axes = plt.subplots(len(metrics), 1, figsize=(9, 3.2 * len(metrics)), squeeze=False)

    for ax, metric in zip(axes[:, 0], metrics):
        series = sorted(by_metric[metric], key=lambda r: r["WeekStart"])
        weeks = [r["WeekStart"] for r in series]
        values = [r["Value"] for r in series]
        ax.plot(weeks, values, marker="o", linewidth=2)
        ax.set_title(f"{player} — {metric}")
        ax.set_ylabel(metric)
        ax.tick_params(axis="x", rotation=30)
        ax.grid(True, alpha=0.3)

    fig.tight_layout()
    return _fig_to_png(fig)


def metric_trend_chart(
    metric: str,
    rows: list[dict[str, Any]],
    top_n: int = 8,
) -> io.BytesIO | None:
    """Overlay trend lines for top N players by latest value."""
    if not rows:
        return None

    by_player: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_player[row["PlayerName"]].append(row)

    ranked = sorted(
        by_player.items(),
        key=lambda kv: kv[1][-1]["Value"] if kv[1] else 0,
        reverse=True,
    )[:top_n]

    fig, ax = plt.subplots(figsize=(10, 5))
    for player, series in ranked:
        series = sorted(series, key=lambda r: r["WeekStart"])
        ax.plot(
            [r["WeekStart"] for r in series],
            [r["Value"] for r in series],
            marker="o",
            label=player,
        )

    ax.set_title(f"{metric} — Top {len(ranked)} Trends")
    ax.set_ylabel(metric)
    ax.tick_params(axis="x", rotation=30)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", fontsize=8)
    fig.tight_layout()
    return _fig_to_png(fig)


def leaderboard_bar_chart(
    metric: str,
    week: str,
    rows: list[dict[str, Any]],
    top_n: int = 40,
) -> io.BytesIO | None:
    if not rows:
        return None

    subset = rows[:top_n]
    names = [r["PlayerName"] for r in subset][::-1]
    values = [r["Value"] for r in subset][::-1]

    fig, ax = plt.subplots(figsize=(9, max(3.5, 0.35 * len(names) + 1)))
    ax.barh(names, values, color="#3b82f6")
    title = f"{metric} Leaderboard — {week}"
    if len(rows) > top_n:
        title += f" (top {top_n} of {len(rows)})"
    ax.set_title(title)
    ax.set_xlabel(metric)
    ax.grid(True, axis="x", alpha=0.3)
    fig.tight_layout()
    return _fig_to_png(fig)


def growth_bar_chart(
    metric: str,
    rows: list[dict[str, Any]],
    top_n: int = 15,
) -> io.BytesIO | None:
    if not rows:
        return None

    subset = rows[:top_n]
    names = [r["PlayerName"] for r in subset][::-1]
    growth = [(r["GrowthPct"] or 0.0) for r in subset][::-1]
    colors = ["#16a34a" if g >= 0 else "#dc2626" for g in growth]

    fig, ax = plt.subplots(figsize=(9, max(3.5, 0.35 * len(names) + 1)))
    ax.barh(names, growth, color=colors)
    ax.set_title(f"{metric} Growth %")
    ax.set_xlabel("Growth %")
    ax.axvline(0, color="#444", linewidth=0.8)
    ax.grid(True, axis="x", alpha=0.3)
    fig.tight_layout()
    return _fig_to_png(fig)
