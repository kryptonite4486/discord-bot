"""Analytical report commands."""

from __future__ import annotations

import asyncio
import io
import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.config import resolve_metric
from bot.reporting import charts, formatters
from bot.utils.guild import (
    channel_id_from_context,
    channel_id_from_interaction,
    guild_id_from_context,
    guild_id_from_interaction,
)
from bot.utils.parsing import chunk_fenced_md, parse_week_start

log = logging.getLogger(__name__)

METRIC_CHOICES = [
    app_commands.Choice(name="Versus Points", value="versus"),
    app_commands.Choice(name="Tech Contribution", value="tech"),
    app_commands.Choice(name="HQ Level", value="hq"),
    app_commands.Choice(name="Power", value="power"),
]

SCOPE_CHOICES = [
    app_commands.Choice(name="This channel", value="channel"),
    app_commands.Choice(name="Entire server", value="server"),
]


class Reports(commands.Cog):
    """On-demand analytical reports."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    report = app_commands.Group(name="report", description="Generate weekly metric reports")

    @staticmethod
    def _resolve_scope(
        interaction: discord.Interaction,
        scope: app_commands.Choice[str] | None,
    ) -> tuple[str, str | None, bool]:
        """
        Return (guild_id, channel_id|None, show_channel).

        channel scope: filter to current channel only (Phase 2 strict).
        server scope: all channels in the guild; show ChannelId breakout.
        """
        guild_id = guild_id_from_interaction(interaction)
        mode = scope.value if scope else "channel"
        if mode == "server":
            return guild_id, None, True
        return guild_id, channel_id_from_interaction(interaction), False

    async def _send_text(
        self,
        interaction: discord.Interaction,
        text: str,
        *,
        file: discord.File | None = None,
        files: list[discord.File] | None = None,
    ) -> None:
        # Discord does not render markdown tables; use a code block for alignment.
        # Each chunk is a complete ```md … ``` fence so mid-report splits stay valid.
        chunks = chunk_fenced_md(text, limit=1900)
        for chunk in chunks:
            payload = chunk if len(chunk) <= 2000 else chunk[:1990] + "\n```"
            await interaction.followup.send(content=payload)
        # Optional downloads (e.g. full growth .md), then chart last if provided.
        for extra in files or []:
            await interaction.followup.send(file=extra)
        if file is not None:
            await interaction.followup.send(file=file)

    @report.command(name="week", description="Weekly summary for all metrics")
    @app_commands.describe(
        week="Week start YYYY-MM-DD / current / last",
        scope="This channel (default) or entire server",
    )
    @app_commands.choices(scope=SCOPE_CHOICES)
    async def report_week(
        self,
        interaction: discord.Interaction,
        week: str | None = None,
        scope: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer()
        guild_id, channel_id, show_channel = self._resolve_scope(interaction, scope)
        week_start = parse_week_start(week)
        rows = await self.bot.db.get_week_metrics(
            guild_id,
            week_start,
            channel_id=channel_id,
            include_unassigned=False,
        )
        text = formatters.week_summary_text(
            week_start, rows, show_channel=show_channel
        )
        await self._send_text(interaction, text)

    @report.command(name="player", description="Trend report for one player")
    @app_commands.describe(
        name="Player name",
        chart="Attach a PNG trend chart",
        scope="This channel (default) or entire server",
    )
    @app_commands.choices(scope=SCOPE_CHOICES)
    async def report_player(
        self,
        interaction: discord.Interaction,
        name: str,
        chart: bool = True,
        scope: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer()
        guild_id, channel_id, show_channel = self._resolve_scope(interaction, scope)
        rows = await self.bot.db.get_player_metrics(
            guild_id,
            name,
            channel_id=channel_id,
            include_unassigned=False,
        )
        text = formatters.player_report_text(
            name, rows, show_channel=show_channel
        )
        png = (
            charts.player_trend_chart(name, rows)
            if chart and rows and not show_channel
            else None
        )
        file = discord.File(png, filename=f"{name}_trend.png") if png else None
        await self._send_text(interaction, text, file=file)

    @report.command(name="versus", description="Versus Points leaderboard for a week")
    @app_commands.describe(
        week="Week start YYYY-MM-DD / current / last",
        scope="This channel (default) or entire server",
    )
    @app_commands.choices(scope=SCOPE_CHOICES)
    async def report_versus(
        self,
        interaction: discord.Interaction,
        week: str | None = None,
        scope: app_commands.Choice[str] | None = None,
    ) -> None:
        await self._metric_leaderboard(interaction, "VersusPoints", week, scope=scope)

    @report.command(name="tech", description="Tech Contribution leaderboard for a week")
    @app_commands.describe(
        week="Week start YYYY-MM-DD / current / last",
        scope="This channel (default) or entire server",
    )
    @app_commands.choices(scope=SCOPE_CHOICES)
    async def report_tech(
        self,
        interaction: discord.Interaction,
        week: str | None = None,
        scope: app_commands.Choice[str] | None = None,
    ) -> None:
        await self._metric_leaderboard(
            interaction, "TechContribution", week, scope=scope
        )

    @report.command(name="trend", description="Multi-week trend for a metric")
    @app_commands.describe(
        metric="Metric to chart",
        weeks="Number of recent weeks",
        scope="This channel (default) or entire server",
    )
    @app_commands.choices(metric=METRIC_CHOICES, scope=SCOPE_CHOICES)
    async def report_trend(
        self,
        interaction: discord.Interaction,
        metric: app_commands.Choice[str],
        weeks: app_commands.Range[int, 2, 26] = 8,
        scope: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer()
        guild_id, channel_id, show_channel = self._resolve_scope(interaction, scope)
        metric_type = resolve_metric(metric.value)
        rows = await self.bot.db.get_trends(
            guild_id,
            metric_type,
            weeks=weeks,
            channel_id=channel_id,
            include_unassigned=False,
        )
        text = formatters.trend_summary_text(
            metric_type, rows, show_channel=show_channel
        )
        png = charts.metric_trend_chart(metric_type, rows) if not show_channel else None
        file = discord.File(png, filename=f"{metric_type}_trend.png") if png else None
        await self._send_text(interaction, text, file=file)

    @report.command(name="growth", description="Growth rates for a metric")
    @app_commands.describe(
        metric="Metric to analyze",
        weeks="Lookback window in weeks",
        scope="This channel (default) or entire server",
    )
    @app_commands.choices(metric=METRIC_CHOICES, scope=SCOPE_CHOICES)
    async def report_growth(
        self,
        interaction: discord.Interaction,
        metric: app_commands.Choice[str],
        weeks: app_commands.Range[int, 2, 26] = 4,
        scope: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer()
        guild_id, channel_id, show_channel = self._resolve_scope(interaction, scope)
        metric_type = resolve_metric(metric.value)

        try:
            rows = await self.bot.db.get_growth_rates(
                guild_id,
                metric_type,
                weeks=weeks,
                channel_id=channel_id,
                include_unassigned=False,
            )
            text = formatters.growth_report_text(
                metric_type, weeks, rows, show_channel=show_channel
            )

            attachments: list[discord.File] = []
            if rows:
                full_md = formatters.growth_report_full_markdown(
                    metric_type, weeks, rows, show_channel=show_channel
                )
                attachments.append(
                    discord.File(
                        io.BytesIO(full_md.encode("utf-8")),
                        filename=f"{metric_type}_growth_full.md",
                    )
                )

            # Matplotlib can block the event loop on first use / large fonts.
            png = None
            if not show_channel:
                png = await asyncio.to_thread(
                    charts.growth_bar_chart, metric_type, rows
                )
            chart = (
                discord.File(png, filename=f"{metric_type}_growth.png")
                if png
                else None
            )
            await self._send_text(
                interaction, text, file=chart, files=attachments
            )
        except AttributeError as exc:
            log.exception("Growth report missing helper — reload formatters")
            await interaction.followup.send(
                "Growth report helpers are out of date. Run "
                "`/admin reload reports` (or restart the bot) and try again.\n"
                f"Details: `{exc}`"
            )
        except Exception as exc:
            log.exception("Growth report failed")
            await interaction.followup.send(f"Growth report failed: `{exc}`")

    @report.command(name="leaderboard", description="Leaderboard for any metric")
    @app_commands.describe(
        metric="Metric to rank",
        week="Week start YYYY-MM-DD / current / last",
        limit="Optional max players (default: all for the week)",
        scope="This channel (default) or entire server",
    )
    @app_commands.choices(metric=METRIC_CHOICES, scope=SCOPE_CHOICES)
    async def report_leaderboard(
        self,
        interaction: discord.Interaction,
        metric: app_commands.Choice[str],
        week: str | None = None,
        limit: app_commands.Range[int, 5, 500] | None = None,
        scope: app_commands.Choice[str] | None = None,
    ) -> None:
        metric_type = resolve_metric(metric.value)
        await self._metric_leaderboard(
            interaction, metric_type, week, limit=limit, scope=scope
        )

    async def _metric_leaderboard(
        self,
        interaction: discord.Interaction,
        metric_type: str,
        week: str | None,
        limit: int | None = None,
        scope: app_commands.Choice[str] | None = None,
    ) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer()
        guild_id, channel_id, show_channel = self._resolve_scope(interaction, scope)
        week_start = parse_week_start(week) if week else None
        rows = await self.bot.db.get_leaderboard(
            guild_id,
            metric_type,
            week_start=week_start,
            limit=limit,
            channel_id=channel_id,
            include_unassigned=False,
        )
        resolved_week = rows[0]["WeekStart"] if rows else (week_start or "n/a")
        text = formatters.leaderboard_text(
            metric_type, str(resolved_week), rows, show_channel=show_channel
        )
        chart_cap = 40
        png = None
        if not show_channel:
            png = charts.leaderboard_bar_chart(
                metric_type, str(resolved_week), rows, top_n=chart_cap
            )
            if png and len(rows) > chart_cap:
                text += f"\n\n(Chart shows top {chart_cap} of {len(rows)} players.)"
        file = discord.File(png, filename=f"{metric_type}_leaderboard.png") if png else None
        await self._send_text(interaction, text, file=file)

    # Prefix fallbacks (channel scope only)
    @commands.command(name="reportweek")
    async def report_week_prefix(self, ctx: commands.Context, week: str | None = None) -> None:
        guild_id = guild_id_from_context(ctx)
        channel_id = channel_id_from_context(ctx)
        week_start = parse_week_start(week)
        rows = await self.bot.db.get_week_metrics(
            guild_id,
            week_start,
            channel_id=channel_id,
            include_unassigned=False,
        )
        await ctx.reply(formatters.week_summary_text(week_start, rows)[:1900])

    @commands.command(name="reportplayer")
    async def report_player_prefix(self, ctx: commands.Context, *, name: str) -> None:
        guild_id = guild_id_from_context(ctx)
        channel_id = channel_id_from_context(ctx)
        rows = await self.bot.db.get_player_metrics(
            guild_id,
            name,
            channel_id=channel_id,
            include_unassigned=False,
        )
        text = formatters.player_report_text(name, rows)
        png = charts.player_trend_chart(name, rows)
        if png:
            await ctx.reply(text[:1500], file=discord.File(png, filename="trend.png"))
        else:
            await ctx.reply(text[:1900])


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Reports(bot))
