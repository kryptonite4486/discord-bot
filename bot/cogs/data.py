"""Server data lifecycle: deletion on request, and retention after removal.

When the bot is removed from a server, that server's metrics are kept for
DATA_RETENTION_DAYS (default 30) and then deleted. Adding the bot back
within that window cancels the deletion, so an accidental kick loses
nothing. While the server has an active subscription (paid, gifted or
trial) its data is kept however long the bot has been gone; the retention
period starts when the bot is removed or the subscription ends, whichever
is later. Server admins can also delete their data immediately with
/data delete, or download it with /data export (bot/utils/export.py).
"""

from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.utils.backup import create_backup
from bot.utils.export import build_export, upload_limit
from bot.utils.guild import (
    channel_display_name,
    channel_id_from_interaction,
    guild_id_from_interaction,
)
from bot.utils.retention import (
    deletion_date,
    fmt_time,
    parse_time,
    plan_reconcile,
    removed_servers,
    utc_now,
)
from bot.utils.parsing import parse_week_start
from bot.utils.tiers import FeatureLocked, hidden_weeks_note, requires_feature

log = logging.getLogger(__name__)

SCOPE_CHOICES = [
    app_commands.Choice(name="This channel", value="channel"),
    app_commands.Choice(name="Entire server", value="server"),
]
FORMAT_CHOICES = [
    app_commands.Choice(name="CSV (spreadsheets)", value="csv"),
    app_commands.Choice(name="JSON", value="json"),
]
_WEEK_HELP = "YYYY-MM-DD (any day of the week), 'current' or 'last'"


def week_range_text(from_week: str | None, to_week: str | None) -> str:
    if from_week and to_week:
        return f" for weeks {from_week} to {to_week}"
    if from_week:
        return f" from week {from_week}"
    if to_week:
        return f" up to week {to_week}"
    return ""


class ConfirmDeleteView(discord.ui.View):
    """Delete / Cancel buttons, usable only by the admin who ran the command."""

    def __init__(self, owner_id: int, *, timeout: float = 60.0) -> None:
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.confirmed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the admin who ran the command can confirm it.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Delete permanently", style=discord.ButtonStyle.danger)
    async def delete(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.confirmed = True
        await interaction.response.edit_message(content="Deleting…", view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Cancelled. Nothing was deleted.", view=None)
        self.stop()


class Data(commands.Cog):
    """Data deletion and post-removal retention."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.purge_due.start()

    async def cog_unload(self) -> None:
        self.purge_due.cancel()

    @property
    def retention(self) -> timedelta:
        return timedelta(days=self.bot.settings.data_retention_days)

    # --- Retention after removal -------------------------------------------

    async def schedule(self, guild_id: str, removed_at: datetime) -> None:
        if not await self.bot.db.schedule_purge(guild_id, fmt_time(removed_at)):
            return
        active, ended = await self.bot.db.subscription_status(guild_id, fmt_time(removed_at))
        deletes_at = deletion_date(
            removed_at,
            subscription_active=active,
            subscription_ended=parse_time(ended) if ended else None,
            retention=self.retention,
        )
        if deletes_at is None:
            log.warning(
                "Bot removed from guild %s; its data is kept while its subscription "
                "is active, then for %d more day(s)",
                guild_id,
                self.bot.settings.data_retention_days,
            )
        else:
            log.warning(
                "Bot removed from guild %s; its data will be deleted at %s UTC "
                "unless the bot is added back first",
                guild_id,
                fmt_time(deletes_at),
            )

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await self.schedule(str(guild.id), utc_now())

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        if await self.bot.db.cancel_purge(str(guild.id)):
            log.info("Bot re-added to guild %s; scheduled data deletion cancelled", guild.id)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        member = {str(g.id) for g in self.bot.guilds}
        if not member:
            # An empty guild list is more likely a glitch than every server
            # removing the bot; don't schedule everything for deletion.
            log.warning("No guilds visible at startup; skipping data retention check")
            return
        data = await self.bot.db.guilds_with_data()
        pending = {p["GuildId"] for p in await self.bot.db.pending_purges()}
        to_schedule, to_cancel = plan_reconcile(data, member, pending)
        now = utc_now()
        for guild_id in sorted(to_schedule):
            await self.schedule(guild_id, now)
        for guild_id in sorted(to_cancel):
            await self.bot.db.cancel_purge(guild_id)
            log.info("Bot is in guild %s again; scheduled data deletion cancelled", guild_id)

    @tasks.loop(hours=1)
    async def purge_due(self) -> None:
        now = utc_now()
        due = [
            r
            for r in await removed_servers(self.bot.db, self.retention, now)
            if r.deletes_at is not None and r.deletes_at <= now
        ]
        if not due:
            return
        member = {str(g.id) for g in self.bot.guilds}
        if getattr(self.bot, "backups_enabled", False):
            s = self.bot.settings
            try:
                path = await create_backup(self.bot.db, s.backup_dir, "manual", keep=s.backup_keep)
                log.info("Backup before scheduled data deletion: %s", path.name)
            except Exception:
                log.exception("Backup before scheduled data deletion failed; will retry next hour")
                return
        for r in due:
            if r.guild_id in member:  # re-added and the join event was missed
                await self.bot.db.cancel_purge(r.guild_id)
                continue
            rows = await self.bot.db.purge_guild(r.guild_id)
            log.warning(
                "Deleted %d row(s) for guild %s (bot removed %s UTC; retention ended)",
                rows,
                r.guild_id,
                fmt_time(r.removed_at),
            )

    @purge_due.before_loop
    async def _wait_until_ready(self) -> None:
        await self.bot.wait_until_ready()

    # --- Deletion on request -------------------------------------------------

    data = app_commands.Group(
        name="data",
        description="Manage this server's stored metrics",
        default_permissions=discord.Permissions(administrator=True),
    )

    @data.command(name="delete", description="Permanently delete this channel's or this server's metrics")
    @app_commands.describe(scope="This channel (default) or the entire server")
    @app_commands.choices(scope=SCOPE_CHOICES)
    @app_commands.checks.has_permissions(administrator=True)
    async def delete(
        self,
        interaction: discord.Interaction,
        scope: app_commands.Choice[str] | None = None,
    ) -> None:
        guild_id = guild_id_from_interaction(interaction)
        server = scope is not None and scope.value == "server"
        channel_id = None if server else channel_id_from_interaction(interaction)
        where = (
            "this **entire server** (every channel)"
            if server
            else channel_display_name(interaction.guild, channel_id)
        )
        stats = await self.bot.db.stats(
            guild_id, channel_id=channel_id, include_unassigned=server
        )
        if not stats["rows"]:
            await interaction.response.send_message(
                f"Nothing is stored for {where}.", ephemeral=True
            )
            return

        view = ConfirmDeleteView(interaction.user.id)
        await interaction.response.send_message(
            f"⚠️ This permanently deletes **{stats['rows']}** row(s) for "
            f"**{stats['players']}** player(s) across **{stats['weeks']}** week(s) "
            f"in {where}. It can't be undone from Discord.",
            view=view,
            ephemeral=True,
        )
        if await view.wait():  # timed out
            await interaction.edit_original_response(
                content="Timed out. Nothing was deleted.", view=None
            )
            return
        if not view.confirmed:
            return

        backup_note = ""
        if getattr(self.bot, "backups_enabled", False):
            s = self.bot.settings
            try:
                path = await create_backup(self.bot.db, s.backup_dir, "manual", keep=s.backup_keep)
            except Exception as exc:
                log.exception("Backup before /data delete failed")
                await interaction.edit_original_response(
                    content=f"Deletion cancelled: the safety backup failed (`{exc}`)."
                )
                return
            backup_note = f" A safety backup was taken first (`{path.name}`)."

        rows = await self.bot.db.delete_guild_data(guild_id, channel_id=channel_id)
        log.warning(
            "/data delete by %s (%s): %d row(s) in guild %s channel %s",
            interaction.user,
            interaction.user.id,
            rows,
            guild_id,
            channel_id or "ALL",
        )
        await interaction.edit_original_response(
            content=f"Deleted **{rows}** row(s) for {where}.{backup_note}"
        )

    # --- Export ----------------------------------------------------------------

    @data.command(name="export", description="Download this channel's or this server's metrics as CSV or JSON")
    @app_commands.describe(
        scope="This channel (default) or the entire server",
        format="CSV (default) or JSON",
        from_week=f"Oldest week to include: {_WEEK_HELP}",
        to_week=f"Newest week to include: {_WEEK_HELP}",
    )
    @app_commands.choices(scope=SCOPE_CHOICES, format=FORMAT_CHOICES)
    @app_commands.checks.has_permissions(administrator=True)
    @requires_feature("export")
    async def export(
        self,
        interaction: discord.Interaction,
        scope: app_commands.Choice[str] | None = None,
        format: app_commands.Choice[str] | None = None,
        from_week: str | None = None,
        to_week: str | None = None,
    ) -> None:
        guild_id = guild_id_from_interaction(interaction)
        server = scope is not None and scope.value == "server"
        channel_id = None if server else channel_id_from_interaction(interaction)
        fmt = format.value if format else "csv"
        try:
            start = parse_week_start(from_week) if from_week else None
            end = parse_week_start(to_week) if to_week else None
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if start and end and start > end:
            await interaction.response.send_message(
                "`from_week` is after `to_week`.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        # Exports show the same weeks as reports: older weeks outside the
        # plan's history window are left out and named in a note. Operator
        # exports (/ops export) skip this; see docs/monetization-plan.md.
        note = ""
        tiers = getattr(self.bot, "tiers", None)
        if tiers is not None:
            min_week, status = await tiers.history_window(guild_id)
            if min_week is not None and (start is None or start < min_week):
                hidden = [
                    w
                    for w in await self.bot.db.hidden_weeks(
                        guild_id, min_week, channel_id=channel_id
                    )
                    if (start is None or w >= start) and (end is None or w <= end)
                ]
                note = hidden_weeks_note(hidden, status)
                start = min_week

        where = (
            "this **entire server**"
            if server
            else channel_display_name(interaction.guild, channel_id)
        )
        weeks = week_range_text(start, end)
        result = None
        if start is None or end is None or start <= end:  # the window can pass to_week
            result = await build_export(
                self.bot.db, guild_id, interaction.guild, fmt=fmt,
                channel_id=channel_id, from_week=start, to_week=end,
            )
            if result is None:
                limit_mb = upload_limit(interaction.guild) / (1024 * 1024)
                await interaction.followup.send(
                    f"The export for {where}{weeks} is over this server's "
                    f"{limit_mb:.0f} MB upload limit even zipped. Export one channel "
                    "at a time, or fewer weeks with `from_week` and `to_week`.",
                    ephemeral=True,
                )
                return
        if result is None or not result.rows:
            text = f"Nothing is stored for {where}{weeks}."
            await interaction.followup.send(
                text + (f"\n{note}" if note else ""), ephemeral=True
            )
            return

        rows = result.rows
        log.info(
            "/data export by %s (%s): %d row(s) as %s, guild %s channel %s, weeks %s..%s",
            interaction.user, interaction.user.id, rows, result.filename,
            guild_id, channel_id or "ALL", start or "start", end or "latest",
        )
        zipped = " (zipped to fit Discord's upload limit)" if result.zipped else ""
        text = f"**{rows}** row(s) for {where}{weeks}{zipped}."
        if note:
            text += f"\n{note}"
        await interaction.followup.send(
            text,
            file=discord.File(io.BytesIO(result.data), filename=result.filename),
            ephemeral=True,
        )

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, FeatureLocked):
            return  # the upgrade message was already sent
        if isinstance(error, app_commands.MissingPermissions):
            text = "You need administrator permission for this command."
        else:
            log.exception("Data command error: %s", error)
            text = f"Error: `{error}`"
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Data(bot))
