"""Operator-only commands: reload, sync, backup, queue, purges, usage.

These act on the whole bot, not one server, so they are limited to the
users in BOT_OWNER_IDS. The /ops slash group is registered only in
CONTROL_GUILD_ID, so it never appears in customer servers, and every call
is checked again for both the server and the user.
"""

from __future__ import annotations

import importlib
import logging
import sys
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from bot.utils.backup import create_backup
from bot.utils.command_sync import sync_commands, sync_control_guild
from bot.utils.fair_queue import GuildQueueState
from bot.utils.retention import RemovedServer, removed_servers

log = logging.getLogger(__name__)

NOT_OPERATOR_MESSAGE = "Only the bot operator can use this command."
# Keeps /ops usage and /ops queue under Discord's 2,000-character limit.
MAX_USAGE_ROWS = 20

# Cog reload alone does not refresh already-imported helpers. Reload deps in
# dependency order (leaves first) so formatters pick up a fresh format_value.
_RELOAD_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "bot.cogs.reports": (
        "bot.db.database",
        "bot.utils.parsing",
        "bot.utils.guild",
        "bot.reporting.formatters",
        "bot.reporting.charts",
        "bot.reporting",
    ),
    "bot.cogs.ingest": (
        "bot.db.database",
        "bot.utils.parsing",
        "bot.ocr.vision",
        "bot.ocr.pipeline",
        "bot.ocr",
    ),
    "bot.cogs.help_cmd": (
        "bot.utils.parsing",
    ),
    "bot.cogs.admin": (
        "bot.db.database",
        "bot.utils.guild",
        "bot.utils.backup",
        "bot.utils.names",
        "bot.utils.parsing",
    ),
    "bot.cogs.data": (
        "bot.db.database",
        "bot.utils.backup",
        "bot.utils.guild",
        "bot.utils.retention",
    ),
    "bot.cogs.ops": (
        "bot.utils.backup",
        "bot.utils.command_sync",
        "bot.utils.retention",
    ),
}


def _rebind_database(bot: commands.Bot) -> None:
    """Point the live Database instance at the reloaded class.

    importlib.reload updates the module, but bot.db remains an instance of the
    previous class — method lookup would keep serving stale SQL otherwise.
    """
    mod = sys.modules.get("bot.db.database")
    db = getattr(bot, "db", None)
    if mod is None or db is None or not hasattr(mod, "Database"):
        return
    db.__class__ = mod.Database
    pkg = sys.modules.get("bot.db")
    if pkg is not None:
        pkg.Database = mod.Database


def _reload_dependencies(extension: str, bot: commands.Bot | None = None) -> list[str]:
    refreshed: list[str] = []
    for name in _RELOAD_DEPENDENCIES.get(extension, ()):
        mod = sys.modules.get(name)
        if mod is None:
            continue
        importlib.reload(mod)
        refreshed.append(name)
        if name == "bot.db.database" and bot is not None:
            _rebind_database(bot)
    return refreshed


async def reload_cog(bot: commands.Bot, cog: str) -> str:
    module = cog if cog.startswith("bot.cogs.") else f"bot.cogs.{cog}"
    deps = _reload_dependencies(module, bot)
    try:
        await bot.reload_extension(module)
    except commands.ExtensionNotLoaded:
        await bot.load_extension(module)
    extra = f" (also reloaded: {', '.join(deps)})" if deps else ""
    return f"Reloaded `{module}`{extra}."


async def run_command_sync(bot: commands.Bot) -> str:
    """Register commands and clean duplicate copies in every server."""
    s = bot.settings
    result = await sync_commands(
        bot.tree,
        list(bot.guilds),
        dev_guild_id=s.dev_guild_id,
        control_guild_id=s.control_guild_id,
    )
    return result.summary()


def format_usage_report(
    days: int,
    by_guild: dict[str, dict[str, float]],
    images_by_day: list[tuple[str, float]],
    names: dict[str, str],
) -> str:
    """Markdown summary of OCR usage per server over the last ``days`` days."""
    if not by_guild:
        return f"No OCR usage recorded in the last {days} day(s)."

    def row(label: str, u: dict[str, float]) -> str:
        images = int(u.get("ocr_images", 0))
        batches = int(u.get("ocr_batches", 0))
        secs = u.get("ocr_seconds", 0.0)
        per_image = f"{secs / images:.1f}s" if images else "-"
        avg_wait = f"{u.get('ocr_wait_seconds', 0.0) / batches:.0f}s" if batches else "-"
        return (
            f"{label[:22]:<22} {batches:>5} {images:>6} "
            f"{int(u.get('ocr_failed', 0)):>5} {secs / 60:>7.1f} {per_image:>6} {avg_wait:>6}"
        )

    ordered = sorted(
        by_guild.items(), key=lambda kv: kv[1].get("ocr_images", 0), reverse=True
    )
    totals: dict[str, float] = {}
    for _, u in ordered:
        for k, v in u.items():
            totals[k] = totals.get(k, 0.0) + v
    lines = [
        f"**OCR usage, last {days} day(s)** (UTC days)",
        "```",
        f"{'Server':<22} {'Batch':>5} {'Images':>6} {'Fail':>5} {'OCR min':>7} "
        f"{'s/img':>6} {'Wait':>6}",
        *(row(names.get(gid, gid), u) for gid, u in ordered[:MAX_USAGE_ROWS]),
        *(
            [f"…and {len(ordered) - MAX_USAGE_ROWS} more server(s)"]
            if len(ordered) > MAX_USAGE_ROWS
            else []
        ),
        "-" * 63,
        row("Total", totals),
        "```",
    ]
    if images_by_day:
        peak_day, peak = max(images_by_day, key=lambda d: d[1])
        lines.append(f"Busiest day: **{peak_day}** with **{int(peak)}** images.")
        if len(images_by_day) <= 14:
            lines.append(
                "Images per day: "
                + ", ".join(f"{d[5:]} {int(n)}" for d, n in images_by_day)
            )
    lines.append("_Wait = average time a batch queued behind other servers' batches._")
    return "\n".join(lines)


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def format_queue_report(
    states: list[GuildQueueState], slots: int, names: dict[str, str]
) -> str:
    """Markdown view of the live OCR queue, per server."""
    running = sum(s.running for s in states)
    waiting = sum(s.waiting for s in states)
    head = (
        f"**OCR queue:** {running}/{slots} slot(s) busy, "
        f"{waiting} request(s) waiting across {len(states)} server(s)."
    )
    if not states:
        return head
    lines = [
        head,
        "```",
        f"{'Server':<22} {'Run':>3} {'Wait':>4} {'Running for':>11} {'Oldest wait':>11}",
    ]
    for s in states[:MAX_USAGE_ROWS]:
        lines.append(
            f"{names.get(s.guild_id, s.guild_id)[:22]:<22} {s.running:>3} {s.waiting:>4} "
            f"{_duration(s.longest_run_seconds) if s.running else '-':>11} "
            f"{_duration(s.longest_wait_seconds) if s.waiting else '-':>11}"
        )
    if len(states) > MAX_USAGE_ROWS:
        lines.append(f"…and {len(states) - MAX_USAGE_ROWS} more server(s)")
    lines.append("```")
    return "\n".join(lines)


def format_purges_report(servers: list[RemovedServer], retention_days: int) -> str:
    """Servers that removed the bot and when their data will be deleted."""
    head = (
        f"**Removed servers** (data is kept {retention_days} day(s) after the bot is "
        "removed or the subscription ends, whichever is later; adding the bot back "
        "cancels the deletion)"
    )
    if not servers:
        return head + "\nNone."
    # Soonest deletion first; servers kept by a subscription last.
    ordered = sorted(
        servers, key=lambda r: (r.deletes_at is None, r.deletes_at or r.removed_at)
    )
    lines = [head, "```", f"{'Server ID':<20} {'Removed (UTC)':<16} {'Deletes (UTC)':<16}"]
    for r in ordered[:MAX_USAGE_ROWS]:
        deletes = (
            r.deletes_at.strftime("%Y-%m-%d %H:%M") if r.deletes_at else "kept: subscribed"
        )
        lines.append(f"{r.guild_id:<20} {r.removed_at:%Y-%m-%d %H:%M} {deletes:<16}")
    if len(ordered) > MAX_USAGE_ROWS:
        lines.append(f"…and {len(ordered) - MAX_USAGE_ROWS} more")
    lines.append("```")
    return "\n".join(lines)


def is_operator(bot: commands.Bot, user_id: int) -> bool:
    return user_id in bot.settings.bot_owner_ids


class Ops(commands.Cog):
    """Bot operator commands (owner-only, control server only)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = interaction.guild_id == self.bot.settings.control_guild_id and (
            is_operator(self.bot, interaction.user.id)
        )
        if not allowed:
            log.warning(
                "Refused /ops from user %s in guild %s",
                interaction.user.id,
                interaction.guild_id,
            )
            await interaction.response.send_message(NOT_OPERATOR_MESSAGE, ephemeral=True)
        return allowed

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            return  # interaction_check already replied
        log.exception("Ops command error: %s", error)
        text = f"Error: `{error}`"
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    async def cog_check(self, ctx: commands.Context) -> bool:
        if is_operator(self.bot, ctx.author.id):
            return True
        raise commands.NotOwner(NOT_OPERATOR_MESSAGE)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        if guild.id != self.bot.settings.control_guild_id:
            return
        try:
            count = await sync_control_guild(self.bot.tree, guild.id)
            log.info("Joined control guild %s; registered %d commands", guild.id, count)
        except discord.HTTPException:
            log.exception("Failed to register /ops in control guild %s", guild.id)

    ops = app_commands.Group(name="ops", description="Bot operator commands")

    @ops.command(name="reload", description="Reload a cog module")
    @app_commands.describe(cog="Cog module name, e.g. ingest or reports")
    async def reload(self, interaction: discord.Interaction, cog: str) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            msg = await reload_cog(self.bot, cog)
        except Exception as exc:
            log.exception("Failed to reload %s", cog)
            await interaction.followup.send(f"Reload failed: `{exc}`", ephemeral=True)
            return
        log.info("%s (by %s)", msg, interaction.user)
        await interaction.followup.send(msg, ephemeral=True)

    @ops.command(
        name="sync",
        description="Register commands globally and remove duplicate copies in every server",
    )
    async def sync(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            msg = await run_command_sync(self.bot)
        except Exception as exc:
            log.exception("Command sync failed")
            await interaction.followup.send(f"Sync failed: `{exc}`", ephemeral=True)
            return
        log.info("Synced commands by %s", interaction.user)
        await interaction.followup.send(msg, ephemeral=True)

    @ops.command(
        name="backup",
        description="Write a database backup to the host backup folder now",
    )
    async def backup(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not getattr(self.bot, "backups_enabled", False):
            await interaction.followup.send(
                "Backups are disabled: no backup folder is mounted "
                "(set `BOT_BACKUP_DIR` in .env). Check the bot logs.",
                ephemeral=True,
            )
            return
        settings = self.bot.settings
        try:
            path = await create_backup(
                self.bot.db, settings.backup_dir, "manual", keep=settings.backup_keep
            )
        except Exception as exc:
            log.exception("Manual database backup failed")
            await interaction.followup.send(f"Backup failed: `{exc}`", ephemeral=True)
            return
        log.info("Manual backup %s by %s", path.name, interaction.user)
        await interaction.followup.send(
            f"Backup saved on the host as `{path.name}` "
            f"({path.stat().st_size / 1024:,.0f} KB).",
            ephemeral=True,
        )

    @ops.command(name="queue", description="Live OCR queue: running and waiting requests per server")
    async def queue(self, interaction: discord.Interaction) -> None:
        ingest = self.bot.get_cog("Ingest")
        ocr_queue = getattr(ingest, "ocr_queue", None)
        if ocr_queue is None:
            await interaction.response.send_message(
                "The ingest cog isn't loaded, so there is no OCR queue.", ephemeral=True
            )
            return
        names = {str(g.id): g.name for g in self.bot.guilds}
        await interaction.response.send_message(
            format_queue_report(ocr_queue.snapshot(), ocr_queue.slots, names),
            ephemeral=True,
        )

    @ops.command(name="purges", description="Servers that removed the bot and when their data will be deleted")
    async def purges(self, interaction: discord.Interaction) -> None:
        days = self.bot.settings.data_retention_days
        servers = await removed_servers(
            self.bot.db, timedelta(days=days), datetime.now(timezone.utc)
        )
        await interaction.response.send_message(
            format_purges_report(servers, days), ephemeral=True
        )

    @ops.command(name="usage", description="OCR usage per server (images, OCR time, queue wait)")
    @app_commands.describe(days="How many days back, including today (default 7)")
    async def usage(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 90] = 7,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        since = (datetime.now(timezone.utc).date() - timedelta(days=days - 1)).isoformat()
        by_guild = await self.bot.db.usage_by_guild(since)
        by_day = await self.bot.db.usage_by_day(since, "ocr_images")
        names = {str(g.id): g.name for g in self.bot.guilds}
        await interaction.followup.send(
            format_usage_report(days, by_guild, by_day, names), ephemeral=True
        )

    # Prefix fallbacks: operator only, in any server (useful before /ops is
    # registered in the control server).
    @commands.command(name="reload")
    async def reload_prefix(self, ctx: commands.Context, cog: str) -> None:
        await ctx.reply(await reload_cog(self.bot, cog))

    @commands.command(name="sync")
    async def sync_prefix(self, ctx: commands.Context) -> None:
        """Register commands globally and remove duplicate copies everywhere."""
        await ctx.reply(await run_command_sync(self.bot))


async def setup(bot: commands.Bot) -> None:
    s = bot.settings
    if not s.bot_owner_ids or not s.control_guild_id:
        log.warning(
            "BOT_OWNER_IDS or CONTROL_GUILD_ID is not set; operator commands "
            "(/ops reload, sync, backup, queue, purges, usage) are disabled"
        )
        return
    # guild= registers every slash command in this cog to the control server
    # only; nothing here is added globally. override: on reload, discord.py
    # doesn't remove guild-scoped cog commands, so replace them in place.
    await bot.add_cog(
        Ops(bot), guild=discord.Object(id=s.control_guild_id), override=True
    )
