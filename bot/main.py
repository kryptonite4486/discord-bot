"""Discord bot entrypoint."""

from __future__ import annotations

import asyncio
import logging
import signal

import discord
from discord.ext import commands

from bot import __version__
from bot.config import Settings
from bot.db import Database
from bot.utils import setup_logging
from bot.utils.command_sync import sync_commands
from bot.utils.guild import reject_dm_context, reject_dm_interaction
from bot.utils.storage import check_persistent_paths

log = logging.getLogger(__name__)

COGS = (
    "bot.cogs.admin",
    "bot.cogs.data",
    "bot.cogs.help_cmd",
    "bot.cogs.ingest",
    "bot.cogs.ops",
    "bot.cogs.planner",
    "bot.cogs.reports",
)


class LastZAssistant(commands.Bot):
    """LastZ Assistant: modular discord.py bot with SQLite-backed weekly metrics."""

    def __init__(self, settings: Settings, *, backups_enabled: bool = False) -> None:
        intents = discord.Intents.default()
        intents.message_content = settings.message_content_intent
        intents.members = False
        intents.presences = False

        super().__init__(
            command_prefix=commands.when_mentioned_or(settings.command_prefix),
            intents=intents,
            application_id=settings.app_id,
            help_command=commands.DefaultHelpCommand(),
        )
        self.settings = settings
        self.backups_enabled = backups_enabled
        self.db = Database(
            settings.database_path,
            legacy_guild_id=settings.legacy_guild_id,
        )
        self._commands_synced = False
        if not settings.message_content_intent:
            log.warning(
                "MESSAGE_CONTENT_INTENT=false — prefix commands and /ingest batch "
                "will not work; other slash commands still will."
            )

        # Central guild-only gates so every command (and future cogs) inherit them.
        self.add_check(self._guild_only_prefix)
        self.tree.interaction_check = self._guild_only_interaction

    async def _guild_only_prefix(self, ctx: commands.Context) -> bool:
        return await reject_dm_context(ctx)

    async def _guild_only_interaction(self, interaction: discord.Interaction) -> bool:
        # Allow non-command interactions (components, etc.) without a guild check
        # only when they are not application commands; still reject DM slash use.
        if interaction.type is not discord.InteractionType.application_command:
            return True
        return await reject_dm_interaction(interaction)

    async def setup_hook(self) -> None:
        await self.db.connect()
        unassigned = await self.db.count_all_unassigned()
        if unassigned:
            log.warning(
                "Phase 2 channel isolation is on, but %d row(s) still have "
                "empty ChannelId. Those rows are hidden from channel-scoped "
                "reports until reassigned. Deploy Phase 1 + /admin assign-channel "
                "before Phase 2 if this is unexpected.",
                unassigned,
            )
        for ext in COGS:
            await self.load_extension(ext)
            log.info("Loaded extension %s", ext)

    async def on_ready(self) -> None:
        user = self.user
        log.info(
            "Logged in as %s (id=%s) version=%s guilds=%d",
            user,
            user.id if user else "?",
            __version__,
            len(self.guilds),
        )
        if not self._commands_synced:
            # Global commands everywhere, /ops in the control server only, and
            # leftover per-server copies removed. DEV_GUILD_ID: that server only.
            try:
                result = await sync_commands(
                    self.tree,
                    list(self.guilds),
                    dev_guild_id=self.settings.dev_guild_id,
                    control_guild_id=self.settings.control_guild_id,
                )
                for line in result.summary().splitlines():
                    log.info("Command sync: %s", line.replace("**", ""))
            except Exception:
                log.exception("Failed to sync application commands")
            self._commands_synced = True
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="alliance stats",
            )
        )

    async def on_command_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("You lack permission for that command.")
            return
        if isinstance(error, commands.CheckFailure):
            # Guild-only (and similar) checks reply themselves.
            return
        if isinstance(error, commands.BadArgument):
            await ctx.reply(f"Bad argument: {error}")
            return
        log.exception("Command error in %s: %s", ctx.command, error)
        await ctx.reply(f"Error: `{error}`")

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: discord.app_commands.AppCommandError,
    ) -> None:
        """Ensure deferred slash commands always get a visible failure reply."""
        # discord.py calls this after a command's or cog's own error handler;
        # those reply themselves, so a second "Command failed" would duplicate.
        command = interaction.command
        if isinstance(command, discord.app_commands.Command) and (
            command._has_any_error_handlers()
        ):
            return
        log.exception("App command error in %s: %s", interaction.command, error)
        text = f"Command failed: `{error}`"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except Exception:
            log.exception("Failed to send app command error followup")

    async def close(self) -> None:
        await self.db.close()
        await super().close()


async def amain() -> None:
    settings = Settings.from_env()
    setup_logging(settings.log_level)
    log.info("Starting LastZ Assistant v%s", __version__)
    log.info(
        "OCR vision model=%s base_url=%s timeout=%.0fs",
        settings.ocr_vision_model,
        settings.ocr_vision_base_url,
        settings.ocr_vision_timeout,
    )
    backups_enabled = check_persistent_paths(
        settings.database_path,
        settings.backup_dir,
        allow_unmounted=settings.allow_unmounted_data,
    )
    log.info(
        "Database=%s backups=%s",
        settings.database_path,
        f"{settings.backup_dir} (keep {settings.backup_keep} daily; "
        f"none older than {settings.backup_max_age_days} days)"
        if backups_enabled
        else "disabled",
    )

    bot = LastZAssistant(settings, backups_enabled=backups_enabled)
    # Prefer Bot.on_app_command_error when present; also bind tree for compatibility.
    bot.tree.on_error = bot.on_app_command_error
    async with bot:
        _install_shutdown_handlers(bot)
        await bot.start(settings.discord_token)


def _install_shutdown_handlers(bot: LastZAssistant) -> None:
    """Close the bot cleanly on SIGTERM/SIGINT.

    In Docker the bot runs as PID 1, where SIGTERM has no default action, so
    without a handler ``docker stop`` waits out the grace period and SIGKILLs
    us before the database is closed and the WAL checkpointed.
    """
    loop = asyncio.get_running_loop()
    closing: list[asyncio.Task[None]] = []  # hold a strong ref to the task

    def request_shutdown(sig: signal.Signals) -> None:
        if closing:
            return
        log.info("Received %s, shutting down", sig.name)
        closing.append(loop.create_task(bot.close()))

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_shutdown, sig)
        except (NotImplementedError, RuntimeError):
            # Windows / non-main thread: fall back to default handling.
            pass


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        log.info("Shutdown requested")


if __name__ == "__main__":
    main()
