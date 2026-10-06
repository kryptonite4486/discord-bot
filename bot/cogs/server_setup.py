"""/setup: per-server options for server admins (Manage Server).

For now this is the trivia channel. Once set, trivia runs only there, it's
where invitations to other servers' cross-server matches are posted, and
/add and /ingest are refused there (see bot/utils/channel_rules.py). With no
options, /setup shows the current settings.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.utils.guild import guild_id_from_interaction

log = logging.getLogger(__name__)

HIDE_COMMANDS_TIP = (
    "To also hide commands from the `/` menu in particular channels: "
    "**Server Settings → Integrations → LastZ Assistant**, pick a command, and "
    "add channel overrides. (Discord only lets server admins change that, so "
    "the bot can't do it for you.)"
)


def describe_settings(trivia_channel_id: str | None) -> str:
    if trivia_channel_id:
        trivia = (
            f"• Trivia channel: <#{trivia_channel_id}>. Trivia matches run only "
            "there, invitations to other servers' cross-server matches are "
            "posted there, and `/add` and `/ingest` are refused there."
        )
    else:
        trivia = (
            "• Trivia channel: not set, so trivia can run in any channel. "
            "Tip: create a `#trivia` channel and run "
            "`/setup trivia_channel:#trivia` to keep matches out of your data "
            "channels."
        )
    return f"**Server setup**\n{trivia}\n\n{HIDE_COMMANDS_TIP}"


class ServerSetup(commands.Cog):
    """`/setup`: server options for admins."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="setup", description="Server options, such as the trivia channel (Manage Server)")
    @app_commands.describe(
        trivia_channel="The only channel trivia runs in; data can't be added there",
        clear_trivia_channel="Remove the trivia channel, so trivia can run anywhere",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_command(
        self,
        interaction: discord.Interaction,
        trivia_channel: discord.TextChannel | None = None,
        clear_trivia_channel: bool = False,
    ) -> None:
        guild_id = guild_id_from_interaction(interaction)
        if trivia_channel is not None and clear_trivia_channel:
            await interaction.response.send_message(
                "Pick either `trivia_channel` or `clear_trivia_channel`, not both.",
                ephemeral=True,
            )
            return
        current = (await self.bot.db.guild_settings(guild_id))["trivia_channel_id"]
        new = current
        if trivia_channel is not None:
            perms = trivia_channel.permissions_for(interaction.guild.me)
            if not (perms.view_channel and perms.send_messages and perms.embed_links):
                await interaction.response.send_message(
                    f"I can't post in {trivia_channel.mention}. Give me View Channel, "
                    "Send Messages and Embed Links there first.",
                    ephemeral=True,
                )
                return
            new = str(trivia_channel.id)
        elif clear_trivia_channel:
            new = None
        if new != current:
            await self.bot.db.set_trivia_channel(guild_id, new)
            log.info(
                "/setup by %s in guild %s: trivia channel %s -> %s",
                interaction.user.id, guild_id, current or "none", new or "none",
            )
        await interaction.response.send_message(describe_settings(new), ephemeral=True)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            text = "You need the Manage Server permission for this command."
        else:
            log.exception("/setup error: %s", error)
            text = f"Error: `{error}`"
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ServerSetup(bot))
