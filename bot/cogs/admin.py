"""Admin slash and prefix commands."""

from __future__ import annotations

import importlib
import logging
import sys

import discord
from discord import app_commands
from discord.ext import commands

from bot.utils.guild import (
    channel_id_from_context,
    channel_id_from_interaction,
    guild_id_from_context,
    guild_id_from_interaction,
)

log = logging.getLogger(__name__)

SCOPE_CHOICES = [
    app_commands.Choice(name="This channel", value="channel"),
    app_commands.Choice(name="Entire server", value="server"),
]

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
        "bot.utils.guild",
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


class Admin(commands.Cog):
    """Bot administration: reload, sync, stats."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    admin = app_commands.Group(name="admin", description="Bot administration")

    @admin.command(name="reload", description="Reload a cog module")
    @app_commands.describe(cog="Cog module name, e.g. ingest or reports")
    @app_commands.checks.has_permissions(administrator=True)
    async def reload(self, interaction: discord.Interaction, cog: str) -> None:
        await interaction.response.defer(ephemeral=True)
        module = cog if cog.startswith("bot.cogs.") else f"bot.cogs.{cog}"
        try:
            deps = _reload_dependencies(module, self.bot)
            await self.bot.reload_extension(module)
        except commands.ExtensionNotLoaded:
            deps = _reload_dependencies(module, self.bot)
            await self.bot.load_extension(module)
        except Exception as exc:
            log.exception("Failed to reload %s", module)
            await interaction.followup.send(f"Reload failed: `{exc}`", ephemeral=True)
            return
        log.info(
            "Reloaded extension %s (deps=%s) by %s",
            module,
            deps,
            interaction.user,
        )
        extra = f" (also reloaded: {', '.join(deps)})" if deps else ""
        await interaction.followup.send(
            f"Reloaded `{module}`{extra}.",
            ephemeral=True,
        )

    @admin.command(name="sync", description="Sync slash commands with Discord")
    @app_commands.describe(
        scope="Where to register commands (this server is instant; global can take up to 1 hour)",
    )
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="This server (instant)", value="guild"),
            app_commands.Choice(name="All servers the bot is in (instant)", value="all"),
            app_commands.Choice(name="Global (can take up to 1 hour)", value="global"),
        ]
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def sync(
        self,
        interaction: discord.Interaction,
        scope: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        assert guild is not None
        mode = scope.value if scope else "guild"
        try:
            if mode == "global":
                synced = await self.bot.tree.sync()
                msg = f"Synced **{len(synced)}** global commands (may take up to ~1 hour to appear everywhere)."
            elif mode == "all":
                synced_g = await self.bot.tree.sync()
                counts: list[str] = [f"global: {len(synced_g)}"]
                for g in list(self.bot.guilds):
                    self.bot.tree.copy_global_to(guild=g)
                    synced = await self.bot.tree.sync(guild=g)
                    counts.append(f"{g.name}: {len(synced)}")
                msg = "Synced commands:\n" + "\n".join(f"• {c}" for c in counts)
            else:
                # Refresh global too so clients are not stuck on a stale global schema.
                await self.bot.tree.sync()
                self.bot.tree.copy_global_to(guild=guild)
                synced = await self.bot.tree.sync(guild=guild)
                msg = (
                    f"Synced global + **{len(synced)}** commands to "
                    f"**{guild.name}** (guild is instant; global may lag)."
                )
        except Exception as exc:
            log.exception("Command sync failed")
            await interaction.followup.send(f"Sync failed: `{exc}`", ephemeral=True)
            return
        log.info("Synced commands mode=%s by %s", mode, interaction.user)
        await interaction.followup.send(msg, ephemeral=True)

    @admin.command(name="stats", description="Show datastore statistics for this channel")
    @app_commands.describe(
        scope="This channel (default) or entire server",
    )
    @app_commands.choices(scope=SCOPE_CHOICES)
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
        embed.set_footer(text=stats["path"])
        await interaction.followup.send(embed=embed, ephemeral=True)

    @reload.error
    @sync.error
    @stats.error
    async def admin_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
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

    # Prefix fallbacks — reload/sync require Administrator; dbstats does not.
    @commands.command(name="reload")
    @commands.has_permissions(administrator=True)
    async def reload_prefix(self, ctx: commands.Context, cog: str) -> None:
        module = cog if cog.startswith("bot.cogs.") else f"bot.cogs.{cog}"
        deps = _reload_dependencies(module, self.bot)
        try:
            await self.bot.reload_extension(module)
        except commands.ExtensionNotLoaded:
            await self.bot.load_extension(module)
        extra = f" (also reloaded: {', '.join(deps)})" if deps else ""
        await ctx.reply(f"Reloaded `{module}`{extra}.")

    @commands.command(name="sync")
    @commands.has_permissions(administrator=True)
    async def sync_prefix(self, ctx: commands.Context, scope: str = "guild") -> None:
        """Sync slash commands. Usage: !sync [guild|all|global] — run !sync in a new server to register commands instantly."""
        assert ctx.guild is not None
        mode = (scope or "guild").strip().lower()
        if mode in {"all", "every", "guilds"}:
            synced_g = await self.bot.tree.sync()
            lines = [f"• global: {len(synced_g)}"]
            for g in list(self.bot.guilds):
                self.bot.tree.copy_global_to(guild=g)
                synced = await self.bot.tree.sync(guild=g)
                lines.append(f"• {g.name}: {len(synced)}")
            await ctx.reply("Synced:\n" + "\n".join(lines))
            return
        if mode in {"global", "globals"}:
            synced = await self.bot.tree.sync()
            await ctx.reply(
                f"Synced **{len(synced)}** global commands (may take up to ~1 hour)."
            )
            return
        await self.bot.tree.sync()
        self.bot.tree.copy_global_to(guild=ctx.guild)
        synced = await self.bot.tree.sync(guild=ctx.guild)
        await ctx.reply(
            f"Synced global + **{len(synced)}** commands to **{ctx.guild.name}**."
        )
    @commands.command(name="dbstats")
    async def stats_prefix(self, ctx: commands.Context, scope: str = "channel") -> None:
        """Datastore stats. Usage: !dbstats [channel|server]"""
        guild_id = guild_id_from_context(ctx)
        channel_id = channel_id_from_context(ctx)
        mode = (scope or "channel").strip().lower()
        filter_channel_id = None if mode in {"server", "guild", "all"} else channel_id
        stats = await self.bot.db.stats(
            guild_id,
            channel_id=filter_channel_id,
            include_unassigned=False,
        )
        scope_label = "server" if filter_channel_id is None else "channel"
        await ctx.reply(
            f"Scope={scope_label} Guild={guild_id} Channel={channel_id} "
            f"Rows={stats['rows']} Players={stats['players']} "
            f"Weeks={stats['weeks']} Unassigned={stats['unassigned_rows']} "
            f"Metrics={stats['by_metric']}"
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot))
