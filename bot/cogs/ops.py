"""Operator-only commands: reload, sync, backup.

These act on the whole bot, not one server, so they are limited to the
users in BOT_OWNER_IDS. The /ops slash group is registered only in
CONTROL_GUILD_ID, so it never appears in customer servers, and every call
is checked again for both the server and the user.
"""

from __future__ import annotations

import importlib
import logging
import sys

import discord
from discord import app_commands
from discord.ext import commands

from bot.utils.backup import create_backup
from bot.utils.command_sync import sync_commands, sync_control_guild

log = logging.getLogger(__name__)

NOT_OPERATOR_MESSAGE = "Only the bot operator can use this command."

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
    "bot.cogs.ops": (
        "bot.utils.backup",
        "bot.utils.command_sync",
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
            "(/ops reload, sync, backup) are disabled"
        )
        return
    # guild= registers every slash command in this cog to the control server
    # only; nothing here is added globally. override: on reload, discord.py
    # doesn't remove guild-scoped cog commands, so replace them in place.
    await bot.add_cog(
        Ops(bot), guild=discord.Object(id=s.control_guild_id), override=True
    )
