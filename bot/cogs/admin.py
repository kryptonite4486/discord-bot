"""Admin slash and prefix commands."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)


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
            await self.bot.reload_extension(module)
        except commands.ExtensionNotLoaded:
            await self.bot.load_extension(module)
        except Exception as exc:
            log.exception("Failed to reload %s", module)
            await interaction.followup.send(f"Reload failed: `{exc}`", ephemeral=True)
            return
        log.info("Reloaded extension %s by %s", module, interaction.user)
        await interaction.followup.send(f"Reloaded `{module}`.", ephemeral=True)

    @admin.command(name="sync", description="Sync slash commands with Discord")
    @app_commands.checks.has_permissions(administrator=True)
    async def sync(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        try:
            if guild is not None:
                self.bot.tree.copy_global_to(guild=guild)
                synced = await self.bot.tree.sync(guild=guild)
                scope = f"guild {guild.id}"
            else:
                synced = await self.bot.tree.sync()
                scope = "global"
        except Exception as exc:
            log.exception("Command sync failed")
            await interaction.followup.send(f"Sync failed: `{exc}`", ephemeral=True)
            return
        log.info("Synced %d commands (%s) by %s", len(synced), scope, interaction.user)
        await interaction.followup.send(
            f"Synced **{len(synced)}** commands ({scope}).",
            ephemeral=True,
        )

    @admin.command(name="stats", description="Show datastore statistics")
    @app_commands.checks.has_permissions(administrator=True)
    async def stats(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        stats = await self.bot.db.stats()
        by_metric = "\n".join(
            f"• {k}: {v}" for k, v in sorted(stats["by_metric"].items())
        ) or "_none_"
        embed = discord.Embed(title="Datastore Stats", color=discord.Color.blurple())
        embed.add_field(name="Rows", value=str(stats["rows"]))
        embed.add_field(name="Players", value=str(stats["players"]))
        embed.add_field(name="Weeks", value=str(stats["weeks"]))
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
        try:
            await self.bot.reload_extension(module)
        except commands.ExtensionNotLoaded:
            await self.bot.load_extension(module)
        await ctx.reply(f"Reloaded `{module}`.")

    @commands.command(name="sync")
    @commands.has_permissions(administrator=True)
    async def sync_prefix(self, ctx: commands.Context) -> None:
        if ctx.guild:
            self.bot.tree.copy_global_to(guild=ctx.guild)
            synced = await self.bot.tree.sync(guild=ctx.guild)
            await ctx.reply(f"Synced **{len(synced)}** guild commands.")
        else:
            synced = await self.bot.tree.sync()
            await ctx.reply(f"Synced **{len(synced)}** global commands.")

    @commands.command(name="dbstats")
    @commands.has_permissions(administrator=True)
    async def stats_prefix(self, ctx: commands.Context) -> None:
        stats = await self.bot.db.stats()
        await ctx.reply(
            f"Rows={stats['rows']} Players={stats['players']} "
            f"Weeks={stats['weeks']} Metrics={stats['by_metric']}"
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot))
