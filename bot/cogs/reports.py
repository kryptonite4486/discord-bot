"""Analytical report commands."""

from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands

from bot.config import resolve_metric
from bot.reporting import charts, formatters
from bot.utils.guild import (
    channel_id_from_interaction,
    channel_name_map,
    guild_id_from_interaction,
)
from bot.utils.parsing import chunk_fenced_md, parse_week_start
from bot.utils.tiers import TierStatus, ensure_feature, hidden_weeks_note, requires_feature

log = logging.getLogger(__name__)

METRIC_CHOICES = [
    app_commands.Choice(name="Versus Points", value="versus"),
    app_commands.Choice(name="Tech Contribution", value="tech"),
    app_commands.Choice(name="HQ Level", value="hq"),
    app_commands.Choice(name="Power", value="power"),
    app_commands.Choice(name="Arena Power", value="arena"),
    app_commands.Choice(name="Kills", value="kills"),
]

SCOPE_CHOICES = [
    app_commands.Choice(name="Select Channels", value="select"),
    app_commands.Choice(name="All Channels", value="all"),
]

# Optional shortcuts — skip the multi-select UI when already filled.
_CHANNEL_DESCRIBE = (
    "Optional shortcut: data channel (skips picker when using Select Channels)"
)

_SCOPE_DESCRIBE = (
    "Select Channels (picker of datasets) or All Channels with data"
)


@dataclass(frozen=True)
class ReportScope:
    guild_id: str
    """None = all channels in the guild that have data."""
    channel_ids: list[str] | None
    show_channel: bool
    channel_names: dict[str, str]


class DataChannelSelectView(discord.ui.View):
    """Ephemeral multi-select of channels that already have metrics."""

    def __init__(
        self,
        *,
        options: list[discord.SelectOption],
        owner_id: int,
        timeout: float = 120.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.selected: list[str] | None = None
        self._timed_out = False

        max_values = min(len(options), 25)
        select = discord.ui.Select(
            placeholder="Select one or more data channels…",
            min_values=1,
            max_values=max_values,
            options=options,
        )
        select.callback = self._on_select  # type: ignore[method-assign]
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the user who ran the report can pick channels.",
                ephemeral=True,
            )
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction) -> None:
        assert isinstance(self.children[0], discord.ui.Select)
        self.selected = list(self.children[0].values)
        await interaction.response.edit_message(
            content=(
                f"Using **{len(self.selected)}** channel(s). Generating report…"
            ),
            view=None,
        )
        self.stop()

    async def on_timeout(self) -> None:
        self._timed_out = True
        self.selected = None


class Reports(commands.Cog):
    """On-demand analytical reports."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    report = app_commands.Group(name="report", description="Generate weekly metric reports")

    def _resolve_scope(
        self,
        interaction: discord.Interaction,
        scope: app_commands.Choice[str] | None,
        *,
        channel: discord.abc.GuildChannel | None = None,
        channel2: discord.abc.GuildChannel | None = None,
        channel3: discord.abc.GuildChannel | None = None,
        selected_ids: list[str] | None = None,
    ) -> ReportScope:
        """
        Resolve which channel dataset(s) to include.

        - select: selected_ids or channel/channel2/channel3 picks
        - all: every channel with data in this guild
        """
        guild_id = guild_id_from_interaction(interaction)
        guild = interaction.guild
        mode = scope.value if scope else "select"

        picks = [c for c in (channel, channel2, channel3) if c is not None]
        for ch in picks:
            if ch.guild is None or str(ch.guild.id) != guild_id:
                raise ValueError(f"Channel {ch.mention} is not in this server.")

        if selected_ids:
            channel_ids = list(dict.fromkeys(str(c) for c in selected_ids))
            names = channel_name_map(guild, channel_ids)
            return ReportScope(
                guild_id, channel_ids, show_channel=len(channel_ids) > 1, channel_names=names
            )

        if picks:
            channel_ids = list(dict.fromkeys(str(c.id) for c in picks))
            names = channel_name_map(guild, channel_ids)
            return ReportScope(
                guild_id,
                channel_ids,
                show_channel=len(channel_ids) > 1 or mode in {"all", "server"},
                channel_names=names,
            )

        if mode in {"all", "server"}:
            return ReportScope(guild_id, None, True, {})

        # select without picks/ids should not reach here — caller shows the picker.
        raise ValueError(
            "Select Channels requires picking at least one data channel."
        )

    async def _data_channel_options(
        self,
        interaction: discord.Interaction,
        guild_id: str,
    ) -> list[discord.SelectOption]:
        breakdown = await self.bot.db.channel_row_counts(guild_id)
        options: list[discord.SelectOption] = []
        for cid, count in breakdown:
            if not cid or count <= 0:
                continue
            label = channel_name_map(interaction.guild, [cid]).get(cid, cid)
            # Select labels max 100 chars; values max 100.
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(cid)[:100],
                    description=f"{count} metric row(s)"[:100],
                )
            )
            if len(options) >= 25:
                break
        return options

    async def _prompt_pick_data_channels(
        self,
        interaction: discord.Interaction,
        guild_id: str,
    ) -> list[str] | None:
        options = await self._data_channel_options(interaction, guild_id)
        if not options:
            await interaction.followup.send(
                "No channels in this server have ingested metrics yet."
            )
            return None

        view = DataChannelSelectView(
            options=options,
            owner_id=interaction.user.id,
        )
        await interaction.followup.send(
            "**Select Channels** — choose which data channels to include "
            f"({len(options)} available):",
            view=view,
            ephemeral=True,
        )
        timed_out = await view.wait()
        if timed_out or not view.selected:
            await interaction.followup.send(
                "Channel selection timed out or was cancelled. Re-run the report.",
                ephemeral=True,
            )
            return None
        return view.selected

    async def _prompt_for_data_scope(
        self,
        interaction: discord.Interaction,
        guild_id: str,
    ) -> None:
        options = await self._data_channel_options(interaction, guild_id)
        lines = [
            f"• {opt.label} (`{opt.value}`) — {opt.description}"
            for opt in options
        ]
        body = (
            "No data source was selected.\n"
            "Re-run and set **scope** to:\n"
            "• **All Channels** — every channel with data, or\n"
            "• **Select Channels** — pick from the list "
            "(or pass channel / channel2 / channel3).\n"
        )
        if lines:
            body += "\nChannels with data:\n" + "\n".join(lines)
        else:
            body += "\n_No channels in this server have ingested data yet._"
        await interaction.followup.send(body)

    async def _resolve_scope_or_prompt(
        self,
        interaction: discord.Interaction,
        scope: app_commands.Choice[str] | None,
        *,
        channel: discord.abc.GuildChannel | None = None,
        channel2: discord.abc.GuildChannel | None = None,
        channel3: discord.abc.GuildChannel | None = None,
    ) -> ReportScope | None:
        """Resolve the report's channels; None if cancelled or not allowed.

        Reports over more than one channel need the multi_channel_reports
        feature (Command plan).
        """
        rs = await self._resolve_scope_or_prompt_any(
            interaction, scope, channel=channel, channel2=channel2, channel3=channel3
        )
        if rs is None:
            return None
        if rs.channel_ids is None or len(rs.channel_ids) > 1:
            if not await ensure_feature(interaction, "multi_channel_reports"):
                return None
        return rs

    async def _resolve_scope_or_prompt_any(
        self,
        interaction: discord.Interaction,
        scope: app_commands.Choice[str] | None,
        *,
        channel: discord.abc.GuildChannel | None = None,
        channel2: discord.abc.GuildChannel | None = None,
        channel3: discord.abc.GuildChannel | None = None,
    ) -> ReportScope | None:
        """
        Resolve scope. Select Channels shows an ephemeral multi-select of
        channels that already have data (unless channel shortcuts were passed).
        """
        guild_id = guild_id_from_interaction(interaction)
        picks = [c for c in (channel, channel2, channel3) if c is not None]
        mode = scope.value if scope else "select"

        # Treat legacy "multiple"/"channel" values as select if a stale client sends them.
        if mode in {"multiple", "channel"}:
            mode = "select"

        if mode in {"all", "server"}:
            try:
                return self._resolve_scope(
                    interaction,
                    scope,
                    channel=channel,
                    channel2=channel2,
                    channel3=channel3,
                )
            except ValueError as exc:
                await interaction.followup.send(str(exc))
                return None

        # Select Channels
        if picks:
            try:
                return self._resolve_scope(
                    interaction,
                    scope,
                    channel=channel,
                    channel2=channel2,
                    channel3=channel3,
                )
            except ValueError as exc:
                await interaction.followup.send(str(exc))
                return None

        selected = await self._prompt_pick_data_channels(interaction, guild_id)
        if selected is None:
            return None
        try:
            return self._resolve_scope(
                interaction,
                scope,
                selected_ids=selected,
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc))
            return None
    async def _history_window(self, guild_id: str) -> tuple[str | None, TierStatus | None]:
        """(min_week, status) from the server's plan; (None, None) = show all."""
        tiers = getattr(self.bot, "tiers", None)
        if tiers is None:
            return None, None
        return await tiers.history_window(guild_id)

    async def _hidden_note(
        self,
        rs: ReportScope,
        window: tuple[str | None, TierStatus | None],
        **filters,
    ) -> str:
        """Footer naming the weeks this report left out, or ''."""
        min_week, status = window
        if min_week is None or status is None:
            return ""
        hidden = await self.bot.db.hidden_weeks(
            rs.guild_id,
            min_week,
            channel_ids=rs.channel_ids,
            include_unassigned=False,
            **filters,
        )
        note = hidden_weeks_note(hidden, status)
        return f"\n\n{note}" if note else ""

    async def _chart_watermark(self, guild_id: str) -> bool:
        """Free-plan watermark on PNG charts (only while tiers are enforced)."""
        tiers = getattr(self.bot, "tiers", None)
        return tiers is not None and await tiers.chart_watermark(guild_id)

    def _enrich_channel_names(
        self,
        interaction: discord.Interaction,
        rows: list[dict],
        scope: ReportScope,
    ) -> dict[str, str]:
        ids = {str(r.get("ChannelId") or "") for r in rows}
        ids.discard("")
        merged = dict(scope.channel_names)
        merged.update(channel_name_map(interaction.guild, ids))
        return merged

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
        for extra in files or []:
            await interaction.followup.send(file=extra)
        if file is not None:
            await interaction.followup.send(file=file)

    @report.command(name="week", description="Weekly summary for all metrics")
    @app_commands.describe(
        week="Week start YYYY-MM-DD / current / last (default: /setup default week)",
        scope=_SCOPE_DESCRIBE,
        channel=_CHANNEL_DESCRIBE,
        channel2="Additional data channel to include",
        channel3="Additional data channel to include",
    )
    @app_commands.choices(scope=SCOPE_CHOICES)
    async def report_week(
        self,
        interaction: discord.Interaction,
        scope: app_commands.Choice[str],
        week: str | None = None,
        channel: discord.TextChannel | None = None,
        channel2: discord.TextChannel | None = None,
        channel3: discord.TextChannel | None = None,
    ) -> None:
        await interaction.response.defer()
        rs = await self._resolve_scope_or_prompt(
            interaction, scope, channel=channel, channel2=channel2, channel3=channel3
        )
        if rs is None:
            return
        if not week:
            week = (await self.bot.db.guild_settings(rs.guild_id))["default_week"]
        week_start = parse_week_start(week)
        window = await self._history_window(rs.guild_id)
        rows = await self.bot.db.get_week_metrics(
            rs.guild_id,
            week_start,
            channel_ids=rs.channel_ids,
            include_unassigned=False,
            min_week=window[0],
        )
        names = self._enrich_channel_names(interaction, rows, rs)
        text = formatters.week_summary_text(
            week_start,
            rows,
            show_channel=rs.show_channel,
            channel_names=names,
        )
        text += await self._hidden_note(rs, window, week_start=week_start)
        await self._send_text(interaction, text)

    @report.command(name="player", description="Trend report for one player")
    @app_commands.describe(
        name="Player name",
        chart="Attach a PNG trend chart",
        scope=_SCOPE_DESCRIBE,
        channel=_CHANNEL_DESCRIBE,
        channel2="Additional data channel to include",
        channel3="Additional data channel to include",
    )
    @app_commands.choices(scope=SCOPE_CHOICES)
    @requires_feature("advanced_reports")
    async def report_player(
        self,
        interaction: discord.Interaction,
        name: str,
        scope: app_commands.Choice[str],
        chart: bool = True,
        channel: discord.TextChannel | None = None,
        channel2: discord.TextChannel | None = None,
        channel3: discord.TextChannel | None = None,
    ) -> None:
        await interaction.response.defer()
        rs = await self._resolve_scope_or_prompt(
            interaction, scope, channel=channel, channel2=channel2, channel3=channel3
        )
        if rs is None:
            return
        window = await self._history_window(rs.guild_id)
        rows = await self.bot.db.get_player_metrics(
            rs.guild_id,
            name,
            channel_ids=rs.channel_ids,
            include_unassigned=False,
            min_week=window[0],
        )
        names = self._enrich_channel_names(interaction, rows, rs)
        text = formatters.player_report_text(
            name,
            rows,
            show_channel=rs.show_channel,
            channel_names=names,
        )
        text += await self._hidden_note(rs, window, player_name=name)
        png = (
            charts.player_trend_chart(
                name, rows, watermark=await self._chart_watermark(rs.guild_id)
            )
            if chart and rows and not rs.show_channel
            else None
        )
        file = discord.File(png, filename=f"{name}_trend.png") if png else None
        await self._send_text(interaction, text, file=file)

    @report.command(name="versus", description="Versus Points leaderboard for a week")
    @app_commands.describe(
        week="Week start YYYY-MM-DD / current / last",
        scope=_SCOPE_DESCRIBE,
        channel=_CHANNEL_DESCRIBE,
        channel2="Additional data channel to include",
        channel3="Additional data channel to include",
    )
    @app_commands.choices(scope=SCOPE_CHOICES)
    async def report_versus(
        self,
        interaction: discord.Interaction,
        scope: app_commands.Choice[str],
        week: str | None = None,
        channel: discord.TextChannel | None = None,
        channel2: discord.TextChannel | None = None,
        channel3: discord.TextChannel | None = None,
    ) -> None:
        await self._metric_leaderboard(
            interaction,
            "VersusPoints",
            scope,
            week=week,
            channel=channel,
            channel2=channel2,
            channel3=channel3,
        )

    @report.command(name="tech", description="Tech Contribution leaderboard for a week")
    @app_commands.describe(
        week="Week start YYYY-MM-DD / current / last",
        scope=_SCOPE_DESCRIBE,
        channel=_CHANNEL_DESCRIBE,
        channel2="Additional data channel to include",
        channel3="Additional data channel to include",
    )
    @app_commands.choices(scope=SCOPE_CHOICES)
    async def report_tech(
        self,
        interaction: discord.Interaction,
        scope: app_commands.Choice[str],
        week: str | None = None,
        channel: discord.TextChannel | None = None,
        channel2: discord.TextChannel | None = None,
        channel3: discord.TextChannel | None = None,
    ) -> None:
        await self._metric_leaderboard(
            interaction,
            "TechContribution",
            scope,
            week=week,
            channel=channel,
            channel2=channel2,
            channel3=channel3,
        )

    @report.command(name="trend", description="Multi-week trend for a metric")
    @app_commands.describe(
        metric="Metric to chart",
        weeks="Number of recent weeks",
        scope=_SCOPE_DESCRIBE,
        channel=_CHANNEL_DESCRIBE,
        channel2="Additional data channel to include",
        channel3="Additional data channel to include",
    )
    @app_commands.choices(metric=METRIC_CHOICES, scope=SCOPE_CHOICES)
    @requires_feature("advanced_reports")
    async def report_trend(
        self,
        interaction: discord.Interaction,
        metric: app_commands.Choice[str],
        scope: app_commands.Choice[str],
        weeks: app_commands.Range[int, 2, 26] = 8,
        channel: discord.TextChannel | None = None,
        channel2: discord.TextChannel | None = None,
        channel3: discord.TextChannel | None = None,
    ) -> None:
        await interaction.response.defer()
        rs = await self._resolve_scope_or_prompt(
            interaction, scope, channel=channel, channel2=channel2, channel3=channel3
        )
        if rs is None:
            return
        metric_type = resolve_metric(metric.value)
        window = await self._history_window(rs.guild_id)
        rows = await self.bot.db.get_trends(
            rs.guild_id,
            metric_type,
            weeks=weeks,
            channel_ids=rs.channel_ids,
            include_unassigned=False,
            min_week=window[0],
        )
        names = self._enrich_channel_names(interaction, rows, rs)
        text = formatters.trend_summary_text(
            metric_type,
            rows,
            show_channel=rs.show_channel,
            channel_names=names,
        )
        text += await self._hidden_note(rs, window, metric_type=metric_type, recent=weeks)
        png = (
            charts.metric_trend_chart(
                metric_type, rows, watermark=await self._chart_watermark(rs.guild_id)
            )
            if not rs.show_channel
            else None
        )
        file = discord.File(png, filename=f"{metric_type}_trend.png") if png else None
        await self._send_text(interaction, text, file=file)

    @report.command(name="growth", description="Growth rates for a metric")
    @app_commands.describe(
        metric="Metric to analyze",
        weeks="Lookback window in weeks",
        scope=_SCOPE_DESCRIBE,
        channel=_CHANNEL_DESCRIBE,
        channel2="Additional data channel to include",
        channel3="Additional data channel to include",
    )
    @app_commands.choices(metric=METRIC_CHOICES, scope=SCOPE_CHOICES)
    @requires_feature("advanced_reports")
    async def report_growth(
        self,
        interaction: discord.Interaction,
        metric: app_commands.Choice[str],
        scope: app_commands.Choice[str],
        weeks: app_commands.Range[int, 2, 26] = 4,
        channel: discord.TextChannel | None = None,
        channel2: discord.TextChannel | None = None,
        channel3: discord.TextChannel | None = None,
    ) -> None:
        await interaction.response.defer()
        rs = await self._resolve_scope_or_prompt(
            interaction, scope, channel=channel, channel2=channel2, channel3=channel3
        )
        if rs is None:
            return
        metric_type = resolve_metric(metric.value)

        try:
            window = await self._history_window(rs.guild_id)
            rows = await self.bot.db.get_growth_rates(
                rs.guild_id,
                metric_type,
                weeks=weeks,
                channel_ids=rs.channel_ids,
                include_unassigned=False,
                min_week=window[0],
            )
            names = self._enrich_channel_names(interaction, rows, rs)
            text = formatters.growth_report_text(
                metric_type,
                weeks,
                rows,
                show_channel=rs.show_channel,
                channel_names=names,
            )
            text += await self._hidden_note(
                rs, window, metric_type=metric_type, recent=weeks
            )

            attachments: list[discord.File] = []
            if rows:
                full_csv = formatters.growth_report_full_csv(
                    metric_type,
                    weeks,
                    rows,
                    show_channel=rs.show_channel,
                    channel_names=names,
                )
                attachments.append(
                    discord.File(
                        io.BytesIO(full_csv.encode("utf-8")),
                        filename=f"{metric_type}_growth_full.csv",
                    )
                )

            png = None
            if not rs.show_channel:
                png = await asyncio.to_thread(
                    charts.growth_bar_chart,
                    metric_type,
                    rows,
                    watermark=await self._chart_watermark(rs.guild_id),
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
        scope=_SCOPE_DESCRIBE,
        channel=_CHANNEL_DESCRIBE,
        channel2="Additional data channel to include",
        channel3="Additional data channel to include",
    )
    @app_commands.choices(metric=METRIC_CHOICES, scope=SCOPE_CHOICES)
    async def report_leaderboard(
        self,
        interaction: discord.Interaction,
        metric: app_commands.Choice[str],
        scope: app_commands.Choice[str],
        week: str | None = None,
        limit: app_commands.Range[int, 5, 500] | None = None,
        channel: discord.TextChannel | None = None,
        channel2: discord.TextChannel | None = None,
        channel3: discord.TextChannel | None = None,
    ) -> None:
        metric_type = resolve_metric(metric.value)
        await self._metric_leaderboard(
            interaction,
            metric_type,
            scope,
            week=week,
            limit=limit,
            channel=channel,
            channel2=channel2,
            channel3=channel3,
        )

    async def _metric_leaderboard(
        self,
        interaction: discord.Interaction,
        metric_type: str,
        scope: app_commands.Choice[str],
        week: str | None = None,
        limit: int | None = None,
        channel: discord.TextChannel | None = None,
        channel2: discord.TextChannel | None = None,
        channel3: discord.TextChannel | None = None,
    ) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer()
        rs = await self._resolve_scope_or_prompt(
            interaction, scope, channel=channel, channel2=channel2, channel3=channel3
        )
        if rs is None:
            return
        week_start = parse_week_start(week) if week else None
        window = await self._history_window(rs.guild_id)
        rows = await self.bot.db.get_leaderboard(
            rs.guild_id,
            metric_type,
            week_start=week_start,
            limit=limit,
            channel_ids=rs.channel_ids,
            include_unassigned=False,
            min_week=window[0],
        )
        names = self._enrich_channel_names(interaction, rows, rs)
        resolved_week = rows[0]["WeekStart"] if rows else (week_start or "n/a")
        text = formatters.leaderboard_text(
            metric_type,
            str(resolved_week),
            rows,
            show_channel=rs.show_channel,
            channel_names=names,
        )
        chart_cap = 40
        png = None
        if not rs.show_channel:
            png = charts.leaderboard_bar_chart(
                metric_type,
                str(resolved_week),
                rows,
                top_n=chart_cap,
                watermark=await self._chart_watermark(rs.guild_id),
            )
            if png and len(rows) > chart_cap:
                text += f"\n\n(Chart shows top {chart_cap} of {len(rows)} players.)"
        # No week given: the latest week with data, which may itself be hidden.
        text += await self._hidden_note(
            rs,
            window,
            metric_type=metric_type,
            **({"week_start": week_start} if week_start else {"recent": 1}),
        )
        file = (
            discord.File(png, filename=f"{metric_type}_leaderboard.png") if png else None
        )
        await self._send_text(interaction, text, file=file)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Reports(bot))
