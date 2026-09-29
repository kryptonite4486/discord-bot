"""Admin slash and prefix commands."""

from __future__ import annotations

import importlib
import logging
import sys

import discord
from discord import app_commands
from discord.ext import commands

from bot.utils.guild import guild_id_from_context, guild_id_from_interaction

log = logging.getLogger(__name__)

# Cog reload alone does not refresh already-imported helpers. Reload deps in
# dependency order (leaves first) so formatters pick up a fresh format_value.
_RELOAD_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "bot.cogs.reports": (
        "bot.utils.parsing",
        "bot.reporting.formatters",
        "bot.reporting.charts",
        "bot.reporting",
    ),
    "bot.cogs.ingest": (
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


def _reload_dependencies(extension: str) -> list[str]:
    refreshed: list[str] = []
    for name in _RELOAD_DEPENDENCIES.get(extension, ()):
        mod = sys.modules.get(name)
        if mod is None:
            continue
        importlib.reload(mod)
        refreshed.append(name)
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
            deps = _reload_dependencies(module)
            await self.bot.reload_extension(module)
        except commands.ExtensionNotLoaded:
            deps = _reload_dependencies(module)
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
                counts: list[str] = []
                for g in list(self.bot.guilds):
                    self.bot.tree.copy_global_to(guild=g)
                    synced = await self.bot.tree.sync(guild=g)
                    counts.append(f"{g.name}: {len(synced)}")
                msg = "Synced guild commands for all servers:\n" + "\n".join(
                    f"• {c}" for c in counts
                )
            else:
                self.bot.tree.copy_global_to(guild=guild)
                synced = await self.bot.tree.sync(guild=guild)
                msg = f"Synced **{len(synced)}** commands to **{guild.name}** (instant)."
        except Exception as exc:
            log.exception("Command sync failed")
            await interaction.followup.send(f"Sync failed: `{exc}`", ephemeral=True)
            return
        log.info("Synced commands mode=%s by %s", mode, interaction.user)
        await interaction.followup.send(msg, ephemeral=True)

    @admin.command(name="stats", description="Show datastore statistics for this server")
    @app_commands.checks.has_permissions(administrator=True)
    async def stats(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = guild_id_from_interaction(interaction)
        stats = await self.bot.db.stats(guild_id)
        by_metric = "\n".join(
            f"• {k}: {v}" for k, v in sorted(stats["by_metric"].items())
        ) or "_none_"
        embed = discord.Embed(
            title="Datastore Stats (this server)",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Guild", value=guild_id, inline=False)
        embed.add_field(name="Rows", value=str(stats["rows"]))
        embed.add_field(name="Players", value=str(stats["players"]))
        embed.add_field(name="Weeks", value=str(stats["weeks"]))
        embed.add_field(
            name="Unassigned channel rows",
            value=str(stats["unassigned_rows"]),
            inline=False,
        )
        embed.add_field(name="By Metric", value=by_metric, inline=False)
        embed.set_footer(text=stats["path"])
        await interaction.followup.send(embed=embed, ephemeral=True)

    @admin.command(
        name="assign-channel",
        description=(
            "Phase 1 backfill: assign unassigned rows in this server to a channel"
        ),
    )
    @app_commands.describe(
        channel=(
            "Target channel (default: current channel). "
            "Only updates rows with empty ChannelId."
        ),
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def assign_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = guild_id_from_interaction(interaction)
        target = channel or interaction.channel
        if target is None or not isinstance(target, discord.abc.GuildChannel):
            await interaction.followup.send(
                "Could not resolve a target channel.", ephemeral=True
            )
            return
        if target.guild is None or str(target.guild.id) != guild_id:
            await interaction.followup.send(
                "Channel must belong to this server.", ephemeral=True
            )
            return

        before = await self.bot.db.count_unassigned(guild_id)
        updated = await self.bot.db.assign_channel(
            guild_id, str(target.id), only_unassigned=True
        )
        remaining = await self.bot.db.count_unassigned(guild_id)
        log.info(
            "assign-channel guild=%s channel=%s updated=%s remaining=%s by %s",
            guild_id,
            target.id,
            updated,
            remaining,
            interaction.user,
        )
        await interaction.followup.send(
            f"Assigned **{updated}** unassigned row(s) "
            f"(was {before}) to {target.mention} (`{target.id}`).\n"
            f"Unassigned remaining in this server: **{remaining}**.\n"
            "_Run once per server after deploying Phase 1; "
            "re-run is safe (0 rows if already assigned)._",
            ephemeral=True,
        )

    @reload.error
    @sync.error
    @stats.error
    @assign_channel.error
    async def admin_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        msg = "You need administrator permission for this command."
        if isinstance(error, app_commands.errors.MissingPermissions):
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        else:
            log.exception("Admin command error: %s", error)
            text = f"Error: `{error}`"
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)

    # Prefix fallbacks
    @commands.command(name="reload")
    @commands.has_permissions(administrator=True)
    async def reload_prefix(self, ctx: commands.Context, cog: str) -> None:
        module = cog if cog.startswith("bot.cogs.") else f"bot.cogs.{cog}"
        deps = _reload_dependencies(module)
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
            lines = []
            for g in list(self.bot.guilds):
                self.bot.tree.copy_global_to(guild=g)
                synced = await self.bot.tree.sync(guild=g)
                lines.append(f"• {g.name}: {len(synced)}")
            await ctx.reply("Synced all servers:\n" + "\n".join(lines))
            return
        if mode in {"global", "globals"}:
            synced = await self.bot.tree.sync()
            await ctx.reply(
                f"Synced **{len(synced)}** global commands (may take up to ~1 hour)."
            )
            return
        self.bot.tree.copy_global_to(guild=ctx.guild)
        synced = await self.bot.tree.sync(guild=ctx.guild)
        await ctx.reply(
            f"Synced **{len(synced)}** commands to **{ctx.guild.name}** (instant)."
        )
    @commands.command(name="dbstats")
    @commands.has_permissions(administrator=True)
    async def stats_prefix(self, ctx: commands.Context) -> None:
        guild_id = guild_id_from_context(ctx)
        stats = await self.bot.db.stats(guild_id)
        await ctx.reply(
            f"Guild={guild_id} Rows={stats['rows']} Players={stats['players']} "
            f"Weeks={stats['weeks']} Unassigned={stats['unassigned_rows']} "
            f"Metrics={stats['by_metric']}"
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot))
