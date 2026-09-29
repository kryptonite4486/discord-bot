"""Discord bot entrypoint."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from bot import __version__
from bot.config import Settings
from bot.db import Database
from bot.utils import setup_logging
from bot.utils.guild import reject_dm_context, reject_dm_interaction

log = logging.getLogger(__name__)

COGS = (
    "bot.cogs.admin",
    "bot.cogs.help_cmd",
    "bot.cogs.ingest",
    "bot.cogs.reports",
)


class WeeklyMetricsBot(commands.Bot):
    """Modular discord.py bot with SQLite-backed weekly metrics."""

    def __init__(self, settings: Settings) -> None:
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
        self.db = Database(
            settings.database_path,
            legacy_guild_id=settings.legacy_guild_id,
        )
        if not settings.message_content_intent:
            log.warning(
                "MESSAGE_CONTENT_INTENT=false — prefix commands and OCR channel "
                "auto-ingest will not work; slash commands still will."
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
        for ext in COGS:
            await self.load_extension(ext)
            log.info("Loaded extension %s", ext)

        if self.settings.dev_guild_id:
            guild = discord.Object(id=self.settings.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d commands to dev guild %s", len(synced), guild.id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global application commands", len(synced))

    async def on_ready(self) -> None:
        user = self.user
        log.info(
            "Logged in as %s (id=%s) version=%s guilds=%d",
            user,
            user.id if user else "?",
            __version__,
            len(self.guilds),
        )
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="weekly metrics",
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

    async def close(self) -> None:
        await self.db.close()
        await super().close()


async def amain() -> None:
    settings = Settings.from_env()
    setup_logging(settings.log_level)
    log.info("Starting Weekly Metrics Bot v%s", __version__)
    if settings.ocr_engine == "vision":
        log.info(
            "OCR engine=vision model=%s base_url=%s timeout=%.0fs",
            settings.ocr_vision_model,
            settings.ocr_vision_base_url,
            settings.ocr_vision_timeout,
        )
    else:
        log.info("OCR engine=%s", settings.ocr_engine)

    bot = WeeklyMetricsBot(settings)
    async with bot:
        await bot.start(settings.discord_token)


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        log.info("Shutdown requested")


if __name__ == "__main__":
    main()
