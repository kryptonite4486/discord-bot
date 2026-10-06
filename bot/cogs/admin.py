"""Admin slash commands (server-scoped) and the hourly backup task."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.utils.backup import create_backup, last_backup_time, prune_older_than
from bot.utils.names import group_variants
from bot.utils.parsing import chunk_message
from bot.utils.tiers import FeatureLocked, requires_feature
from bot.utils.guild import (
    channel_id_from_interaction,
    guild_id_from_interaction,
)

log = logging.getLogger(__name__)

SCOPE_CHOICES = [
    app_commands.Choice(name="This channel", value="channel"),
    app_commands.Choice(name="Entire server", value="server"),
]

BACKUP_INTERVAL = timedelta(hours=24)

CONFLICT_CHOICES = [
    app_commands.Choice(name="Stop and list conflicts (default)", value="stop"),
    app_commands.Choice(name="Keep the new name's value", value="keep_target"),
    app_commands.Choice(name="Use the old name's value", value="keep_source"),
]
# Follow-up messages per report; Discord rate-limits long bursts.
MAX_REPORT_MESSAGES = 6


def format_variant_report(
    groups: list[list[dict]],
    conflicts: list[list[dict]],
    *,
    server_scope: bool,
) -> str:
    """Numbered list of name variants with row counts, weeks and conflicts."""
    where = "this server" if server_scope else "this channel"
    if not groups:
        return f"No duplicate player names found in {where}."
    lines = [
        f"**{len(groups)} player name(s) with variant spellings in {where}** "
        "(most rows first; the first is the suggested spelling):",
        "",
    ]
    for n, (group, clashes) in enumerate(zip(groups, conflicts), start=1):
        spellings = " · ".join(
            f"`{r['PlayerName']}` ({r['Rows']} rows, {r['FirstWeek']}→{r['LastWeek']})"
            for r in group
        )
        channel = f"<#{group[0]['ChannelId']}> " if server_scope else ""
        lines.append(f"**{n}.** {channel}{spellings}")
        if clashes:
            shown = "; ".join(
                f"{c['WeekStart']} {c['MetricType']}: {c['ValuesByName']}" for c in clashes[:3]
            )
            more = f" (+{len(clashes) - 3} more)" if len(clashes) > 3 else ""
            lines.append(f"   ⚠️ {len(clashes)} conflict(s): {shown}{more}")
    lines += [
        "",
        "Fix one with `/admin rename-player old_name:<misread> new_name:<correct>`. "
        "Add `scope:Entire server` to fix every channel at once. A backup is taken "
        "first; conflicts stop the rename unless you pick how to resolve them.",
    ]
    return "\n".join(lines)


class Admin(commands.Cog):
    """Server administration: stats and player-name cleanup, plus the daily backup."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        if getattr(self.bot, "backups_enabled", False):
            self.daily_backup.start()

    async def cog_unload(self) -> None:
        self.daily_backup.cancel()

    # Checks hourly rather than sleeping 24h, so a restart or a sleeping Mac
    # delays the daily backup by at most an hour instead of skipping it.
    # Each check also removes backups past BACKUP_MAX_AGE_DAYS, so deleted
    # data doesn't linger in old copies.
    @tasks.loop(hours=1)
    async def daily_backup(self) -> None:
        settings = self.bot.settings
        last = last_backup_time(settings.backup_dir, "daily")
        if last is None or datetime.now(timezone.utc) - last >= BACKUP_INTERVAL:
            try:
                await create_backup(
                    self.bot.db, settings.backup_dir, "daily", keep=settings.backup_keep
                )
            except Exception:
                log.exception("Scheduled database backup failed")
        try:
            prune_older_than(
                settings.backup_dir, timedelta(days=settings.backup_max_age_days)
            )
        except Exception:
            log.exception("Pruning old backups failed")

    # Hidden from members without Administrator; each command checks too, in
    # case a server widens access under Server Settings → Integrations.
    admin = app_commands.Group(
        name="admin",
        description="Server administration",
        default_permissions=discord.Permissions(administrator=True),
    )

    async def _player_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        names = await self.bot.db.list_players(guild_id_from_interaction(interaction))
        needle = current.casefold()
        matches = [n for n in names if needle in n.casefold()][:25]
        return [app_commands.Choice(name=n[:100], value=n[:100]) for n in matches]

    @admin.command(
        name="duplicates",
        description="List player names stored under several spellings (OCR misreads)",
    )
    @app_commands.describe(scope="This channel (default) or entire server")
    @app_commands.choices(scope=SCOPE_CHOICES)
    @app_commands.checks.has_permissions(administrator=True)
    @requires_feature("name_tools")
    async def duplicates(
        self,
        interaction: discord.Interaction,
        scope: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = guild_id_from_interaction(interaction)
        server_scope = bool(scope and scope.value == "server")
        channel_id = None if server_scope else channel_id_from_interaction(interaction)
        summary = await self.bot.db.player_name_summary(guild_id, channel_id=channel_id)
        groups = group_variants(summary)
        conflicts = [
            await self.bot.db.name_conflicts(
                guild_id,
                [r["PlayerName"] for r in group],
                channel_id=group[0]["ChannelId"],
            )
            for group in groups
        ]
        report = format_variant_report(groups, conflicts, server_scope=server_scope)
        chunks = list(chunk_message(report))
        for chunk in chunks[:MAX_REPORT_MESSAGES]:
            await interaction.followup.send(chunk, ephemeral=True)
        if len(chunks) > MAX_REPORT_MESSAGES:
            await interaction.followup.send(
                f"_…{len(chunks) - MAX_REPORT_MESSAGES} more message(s) not shown; "
                "run it per channel to see the rest._",
                ephemeral=True,
            )

    @admin.command(
        name="rename-player",
        description="Move a player's rows to another spelling (fix OCR misreads)",
    )
    @app_commands.describe(
        old_name="Spelling to replace (e.g. the misread one)",
        new_name="Correct spelling (existing player or a new name)",
        scope="This channel (default) or entire server",
        on_conflict="When both names have a value for the same week and metric",
    )
    @app_commands.choices(scope=SCOPE_CHOICES, on_conflict=CONFLICT_CHOICES)
    @app_commands.checks.has_permissions(administrator=True)
    @requires_feature("name_tools")
    async def rename_player(
        self,
        interaction: discord.Interaction,
        old_name: str,
        new_name: str,
        scope: app_commands.Choice[str] | None = None,
        on_conflict: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = guild_id_from_interaction(interaction)
        server_scope = bool(scope and scope.value == "server")
        channel_id = None if server_scope else channel_id_from_interaction(interaction)
        where = "this server" if server_scope else "this channel"
        mode = on_conflict.value if on_conflict else "stop"

        summary = await self.bot.db.player_name_summary(guild_id, channel_id=channel_id)
        if not any(r["PlayerName"] == old_name for r in summary):
            await interaction.followup.send(
                f"No rows for `{old_name}` in {where}. Names are case-sensitive; "
                "pick one from the suggestions or `/admin duplicates`.",
                ephemeral=True,
            )
            return

        backup_note = "⚠️ No backup taken (backups are disabled)."
        if getattr(self.bot, "backups_enabled", False):
            settings = self.bot.settings
            try:
                path = await create_backup(
                    self.bot.db, settings.backup_dir, "manual", keep=settings.backup_keep
                )
            except Exception as exc:
                log.exception("Backup before rename failed")
                await interaction.followup.send(
                    f"Rename cancelled: the safety backup failed (`{exc}`).",
                    ephemeral=True,
                )
                return
            backup_note = f"Backup saved first: `{path.name}`."

        try:
            result = await self.bot.db.rename_player(
                guild_id, old_name, new_name, channel_id=channel_id, on_conflict=mode
            )
        except ValueError as exc:
            await interaction.followup.send(f"Rename failed: {exc}", ephemeral=True)
            return

        conflicts = result["conflicts"]
        if conflicts and mode == "stop":
            shown = "\n".join(
                f"• {c['WeekStart']} {c['MetricType']}"
                + (f" <#{c['ChannelId']}>" if server_scope else "")
                + f": {c['ValuesByName']}"
                for c in conflicts[:10]
            )
            more = f"\n…and {len(conflicts) - 10} more" if len(conflicts) > 10 else ""
            await interaction.followup.send(
                f"Nothing changed: `{old_name}` and `{new_name}` both have values for "
                f"{len(conflicts)} week/metric slot(s) in {where}:\n{shown}{more}\n"
                "Run again with `on_conflict` set to keep one of them.",
                ephemeral=True,
            )
            return

        log.info(
            "rename-player %r -> %r scope=%s by %s: %s",
            old_name,
            new_name,
            "server" if server_scope else channel_id,
            interaction.user,
            {k: v for k, v in result.items() if k != "conflicts"},
        )
        details = [f"moved **{result['moved']}** row(s)"]
        if result["dropped"]:
            details.append(f"dropped {result['dropped']} conflicting `{old_name}` value(s)")
        if result["replaced"]:
            details.append(f"replaced {result['replaced']} `{new_name}` value(s)")
        await interaction.followup.send(
            f"Renamed `{old_name}` → `{new_name}` in {where}: {', '.join(details)}.\n"
            f"{backup_note}",
            ephemeral=True,
        )

    @rename_player.autocomplete("old_name")
    async def _old_name_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._player_autocomplete(interaction, current)

    @rename_player.autocomplete("new_name")
    async def _new_name_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._player_autocomplete(interaction, current)

    @admin.command(name="stats", description="Show datastore statistics for this channel")
    @app_commands.describe(
        scope="This channel (default) or entire server",
    )
    @app_commands.choices(scope=SCOPE_CHOICES)
    @app_commands.checks.has_permissions(administrator=True)
    async def stats(
        self,
        interaction: discord.Interaction,
        scope: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = guild_id_from_interaction(interaction)
        channel_id = channel_id_from_interaction(interaction)
        mode = scope.value if scope else "channel"
        server_scope = mode == "server"
        filter_channel_id = None if server_scope else channel_id

        channel = interaction.channel
        channel_label = (
            f"{channel.mention} (`{channel_id}`)"
            if isinstance(channel, discord.abc.GuildChannel)
            else f"`{channel_id}`"
        )
        stats = await self.bot.db.stats(
            guild_id,
            channel_id=filter_channel_id,
            include_unassigned=False,
        )
        by_metric = "\n".join(
            f"• {k}: {v}" for k, v in sorted(stats["by_metric"].items())
        ) or "_none_"
        title = (
            "Datastore Stats (entire server)"
            if server_scope
            else "Datastore Stats (this channel)"
        )
        embed = discord.Embed(title=title, color=discord.Color.blurple())
        embed.add_field(name="Guild", value=f"`{guild_id}`", inline=False)
        if server_scope:
            embed.add_field(
                name="Scope",
                value="All channels in this server",
                inline=False,
            )
            embed.add_field(
                name="Command channel",
                value=channel_label,
                inline=False,
            )
            # Per-channel row counts for the guild.
            breakdown = await self.bot.db.channel_row_counts(guild_id)
            if breakdown:
                lines = [
                    f"• `{cid or '(unassigned)'}`: {count}"
                    for cid, count in breakdown
                ]
                embed.add_field(
                    name="Rows by ChannelId",
                    value="\n".join(lines)[:1024],
                    inline=False,
                )
        else:
            embed.add_field(name="Channel", value=channel_label, inline=False)
        embed.add_field(name="Rows", value=str(stats["rows"]))
        embed.add_field(name="Players", value=str(stats["players"]))
        embed.add_field(name="Weeks", value=str(stats["weeks"]))
        if server_scope:
            embed.add_field(
                name="Unassigned channel rows",
                value=str(stats["unassigned_rows"]),
                inline=False,
            )
        embed.add_field(name="By Metric", value=by_metric, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, FeatureLocked):
            return  # the upgrade message was already sent
        if isinstance(error, app_commands.errors.MissingPermissions):
            msg = "You need administrator permission for this command."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return
        log.exception("Admin command error: %s", error)
        text = f"Error: `{error}`"
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot))
