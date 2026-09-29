"""Analytical report commands."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.config import resolve_metric
from bot.reporting import charts, formatters
from bot.utils.parsing import chunk_message, parse_week_start

log = logging.getLogger(__name__)

METRIC_CHOICES = [
    app_commands.Choice(name="Versus Points", value="versus"),
    app_commands.Choice(name="Tech Contribution", value="tech"),
    app_commands.Choice(name="HQ Level", value="hq"),
    app_commands.Choice(name="Power", value="power"),
]


class Reports(commands.Cog):
    """On-demand analytical reports."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    report = app_commands.Group(name="report", description="Generate weekly metric reports")

    async def _send_text(
        self,
        interaction: discord.Interaction,
        text: str,
        *,
        file: discord.File | None = None,
    ) -> None:
        # Discord does not render markdown tables; use a code block for alignment.
        wrapped = f"```md\n{text}\n```"
        chunks = list(chunk_message(wrapped, limit=1900))
        first = True
        for chunk in chunks:
            payload = chunk if len(chunk) < 2000 else chunk[:1990] + "\n…"
            if first and file is not None:
                await interaction.followup.send(content=payload, file=file)
            else:
                await interaction.followup.send(content=payload)
            first = False
            file = None

    @report.command(name="week", description="Weekly summary for all metrics")
    @app_commands.describe(week="Week start YYYY-MM-DD / current / last")
    async def report_week(
        self,
        interaction: discord.Interaction,
        week: str | None = None,
    ) -> None:
        await interaction.response.defer()
        week_start = parse_week_start(week)
        rows = await self.bot.db.get_week_metrics(week_start)
        text = formatters.week_summary_text(week_start, rows)
        await self._send_text(interaction, text)

    @report.command(name="player", description="Trend report for one player")
    @app_commands.describe(name="Player name", chart="Attach a PNG trend chart")
    async def report_player(
        self,
        interaction: discord.Interaction,
        name: str,
        chart: bool = True,
    ) -> None:
        await interaction.response.defer()
        rows = await self.bot.db.get_player_metrics(name)
        text = formatters.player_report_text(name, rows)
        png = charts.player_trend_chart(name, rows) if chart and rows else None
        file = discord.File(png, filename=f"{name}_trend.png") if png else None
        await self._send_text(interaction, text, file=file)

    @report.command(name="versus", description="Versus Points leaderboard for a week")
    @app_commands.describe(week="Week start YYYY-MM-DD / current / last")
    async def report_versus(
        self,
        interaction: discord.Interaction,
        week: str | None = None,
    ) -> None:
        await self._metric_leaderboard(interaction, "VersusPoints", week)

    @report.command(name="tech", description="Tech Contribution leaderboard for a week")
    @app_commands.describe(week="Week start YYYY-MM-DD / current / last")
    async def report_tech(
        self,
        interaction: discord.Interaction,
        week: str | None = None,
    ) -> None:
        await self._metric_leaderboard(interaction, "TechContribution", week)

    @report.command(name="trend", description="Multi-week trend for a metric")
    @app_commands.describe(metric="Metric to chart", weeks="Number of recent weeks")
    @app_commands.choices(metric=METRIC_CHOICES)
    async def report_trend(
        self,
        interaction: discord.Interaction,
        metric: app_commands.Choice[str],
        weeks: app_commands.Range[int, 2, 26] = 8,
    ) -> None:
        await interaction.response.defer()
        metric_type = resolve_metric(metric.value)
        rows = await self.bot.db.get_trends(metric_type, weeks=weeks)
        text = formatters.trend_summary_text(metric_type, rows)
        png = charts.metric_trend_chart(metric_type, rows)
        file = discord.File(png, filename=f"{metric_type}_trend.png") if png else None
        await self._send_text(interaction, text, file=file)

    @report.command(name="growth", description="Growth rates for a metric")
    @app_commands.describe(metric="Metric to analyze", weeks="Lookback window in weeks")
    @app_commands.choices(metric=METRIC_CHOICES)
    async def report_growth(
        self,
        interaction: discord.Interaction,
        metric: app_commands.Choice[str],
        weeks: app_commands.Range[int, 2, 26] = 4,
    ) -> None:
        await interaction.response.defer()
        metric_type = resolve_metric(metric.value)
        rows = await self.bot.db.get_growth_rates(metric_type, weeks=weeks)
        text = formatters.growth_report_text(metric_type, weeks, rows)
        png = charts.growth_bar_chart(metric_type, rows)
        file = discord.File(png, filename=f"{metric_type}_growth.png") if png else None
        await self._send_text(interaction, text, file=file)

    @report.command(name="leaderboard", description="Leaderboard for any metric")
    @app_commands.describe(
        metric="Metric to rank",
        week="Week start YYYY-MM-DD / current / last",
        limit="Max players to show",
    )
    @app_commands.choices(metric=METRIC_CHOICES)
    async def report_leaderboard(
        self,
        interaction: discord.Interaction,
        metric: app_commands.Choice[str],
        week: str | None = None,
        limit: app_commands.Range[int, 5, 50] = 25,
    ) -> None:
        metric_type = resolve_metric(metric.value)
        await self._metric_leaderboard(interaction, metric_type, week, limit=limit)

    async def _metric_leaderboard(
        self,
        interaction: discord.Interaction,
        metric_type: str,
        week: str | None,
        limit: int = 25,
    ) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer()
        week_start = parse_week_start(week) if week else None
        rows = await self.bot.db.get_leaderboard(metric_type, week_start=week_start, limit=limit)
        resolved_week = rows[0]["WeekStart"] if rows else (week_start or "n/a")
        text = formatters.leaderboard_text(metric_type, str(resolved_week), rows)
        png = charts.leaderboard_bar_chart(metric_type, str(resolved_week), rows)
        file = discord.File(png, filename=f"{metric_type}_leaderboard.png") if png else None
        await self._send_text(interaction, text, file=file)

    # Prefix fallbacks
    @commands.command(name="reportweek")
    async def report_week_prefix(self, ctx: commands.Context, week: str | None = None) -> None:
        week_start = parse_week_start(week)
        rows = await self.bot.db.get_week_metrics(week_start)
        await ctx.reply(formatters.week_summary_text(week_start, rows)[:1900])

    @commands.command(name="reportplayer")
    async def report_player_prefix(self, ctx: commands.Context, *, name: str) -> None:
        rows = await self.bot.db.get_player_metrics(name)
        text = formatters.player_report_text(name, rows)
        png = charts.player_trend_chart(name, rows)
        if png:
            await ctx.reply(text[:1500], file=discord.File(png, filename="trend.png"))
        else:
            await ctx.reply(text[:1900])


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Reports(bot))
