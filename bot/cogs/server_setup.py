"""/setup: per-server options for server admins (Manage Server).

- Trivia channel: trivia runs only there, it's where invitations to other
  servers' cross-server matches are posted, and /add and /ingest are refused
  there (see bot/utils/channel_rules.py).
- Report channel: where the bot posts to the server on its own, such as gift
  notices (see bot/utils/report_channel.py).
- Default week: the week /add, /ingest and /report week use when none is
  given, either the current week or last week.

With no options, /setup shows the current settings.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.utils.guild import guild_id_from_interaction
from bot.utils.report_channel import missing_permissions, permission_names

log = logging.getLogger(__name__)

HIDE_COMMANDS_TIP = (
    "To also hide commands from the `/` menu in particular channels: "
    "**Server Settings → Integrations → LastZ Assistant**, pick a command, and "
    "add channel overrides. (Discord only lets server admins change that, so "
    "the bot can't do it for you.)"
)

DEFAULT_WEEK_CHOICES = [
    app_commands.Choice(name="Current week", value="current"),
    app_commands.Choice(name="Last week", value="last"),
    app_commands.Choice(name="Bot default", value="default"),
]
_WEEK_LABELS = {"current": "the current week", "last": "last week"}


def describe_settings(settings: dict, bot_default_week: str | None = None) -> str:
    """The /setup summary for ``Database.guild_settings`` output."""
    trivia_channel_id = settings.get("trivia_channel_id")
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
    report_channel_id = settings.get("report_channel_id")
    if report_channel_id:
        report = (
            f"• Report channel: <#{report_channel_id}>. The bot posts notices "
            "for this server there, such as gifted plans."
        )
    else:
        report = (
            "• Report channel: not set, so the bot never posts on its own. "
            "Set one with `/setup report_channel:#channel` to get notices "
            "such as gifted plans."
        )
    default_week = settings.get("default_week")
    if default_week:
        week = f"• Default week: {_WEEK_LABELS[default_week]}"
    elif bot_default_week:
        week = f"• Default week: not set, so the bot's default (`{bot_default_week}`)"
    else:
        week = "• Default week: not set, so the current week"
    week += (
        ". `/add`, `/ingest` and `/report week` use it when you don't give "
        "a `week`. Choose last week if your members post stats after the "
        "weekly reset."
    )
    return f"**Server setup**\n{trivia}\n{report}\n{week}\n\n{HIDE_COMMANDS_TIP}"


class ServerSetup(commands.Cog):
    """`/setup`: server options for admins."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="setup",
        description="Server options: trivia channel, report channel, default week (Manage Server)",
    )
    @app_commands.describe(
        trivia_channel="The only channel trivia runs in; data can't be added there",
        clear_trivia_channel="Remove the trivia channel, so trivia can run anywhere",
        report_channel="Where the bot posts notices for this server, such as gifted plans",
        clear_report_channel="Remove the report channel, so the bot never posts on its own",
        default_week="Week used by /add, /ingest and /report week when none is given",
    )
    @app_commands.choices(default_week=DEFAULT_WEEK_CHOICES)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_command(
        self,
        interaction: discord.Interaction,
        trivia_channel: discord.TextChannel | None = None,
        clear_trivia_channel: bool = False,
        report_channel: discord.TextChannel | None = None,
        clear_report_channel: bool = False,
        default_week: app_commands.Choice[str] | None = None,
    ) -> None:
        guild_id = guild_id_from_interaction(interaction)
        for name, chosen, clear in (
            ("trivia_channel", trivia_channel, clear_trivia_channel),
            ("report_channel", report_channel, clear_report_channel),
        ):
            if chosen is not None and clear:
                await interaction.response.send_message(
                    f"Pick either `{name}` or `clear_{name}`, not both.", ephemeral=True
                )
                return
        for chosen in (trivia_channel, report_channel):
            if chosen is None:
                continue
            missing = missing_permissions(chosen, interaction.guild.me)
            if missing:
                await interaction.response.send_message(
                    f"I can't post in {chosen.mention}. Give me "
                    f"{permission_names(missing)} there first.",
                    ephemeral=True,
                )
                return

        db = self.bot.db
        current = await db.guild_settings(guild_id)
        new = dict(current)
        if trivia_channel is not None:
            new["trivia_channel_id"] = str(trivia_channel.id)
        elif clear_trivia_channel:
            new["trivia_channel_id"] = None
        if report_channel is not None:
            new["report_channel_id"] = str(report_channel.id)
        elif clear_report_channel:
            new["report_channel_id"] = None
        if default_week is not None:
            new["default_week"] = None if default_week.value == "default" else default_week.value

        setters = {
            "trivia_channel_id": db.set_trivia_channel,
            "report_channel_id": db.set_report_channel,
            "default_week": db.set_default_week,
        }
        for key, setter in setters.items():
            if new[key] != current[key]:
                await setter(guild_id, new[key])
                log.info(
                    "/setup by %s in guild %s: %s %s -> %s",
                    interaction.user.id, guild_id, key, current[key] or "none", new[key] or "none",
                )
        settings = getattr(self.bot, "settings", None)
        await interaction.response.send_message(
            describe_settings(new, getattr(settings, "default_week_start", None)),
            ephemeral=True,
        )

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
