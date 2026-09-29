"""Slash and prefix help for bot commands."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.utils.parsing import chunk_message

HELP_TEXT = """\
**Weekly Metrics Bot — Commands**

Data is stored **per Discord server**. Commands only work in a server (DMs unsupported).

```
Admin
  /admin reload     Reload a cog module
  /admin sync       Sync slash commands
  /admin stats      Datastore statistics (this server)

Add / Ingest
  /add versus       Add Versus Points for a player
  /add tech         Add Tech Contribution for a player
  /add general      Add HQ Level and Power
  /ingest image     OCR up to 10 images on this command
  /ingest batch     Collect up to 20 images across messages
  /ingest text      Ingest pasted CSV/text rows

Reports
  /report week      Weekly summary — all players
  /report player    Player history + chart
  /report versus    Versus Points leaderboard (all)
  /report tech      Tech Contribution leaderboard (all)
  /report trend     Multi-week metric trends
  /report growth    Growth rates for a metric
  /report leaderboard  Ranked list (all; optional limit) + chart

Help
  /help             Show this command list
  !helpbot          Same help (prefix)
```

Week args accept `YYYY-MM-DD`, `current`, or `last` (normalized to Sunday).
Discord allows ~10 attachments per message; use `/ingest batch` for larger sets.
If `OCR_CHANNEL_ID` is set, images posted there are auto-ingested.
"""


class HelpCmd(commands.Cog):
    """User-facing command help (slash `/help`, prefix `!helpbot`)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="help", description="List bot slash commands")
    async def help_slash(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        for chunk in chunk_message(HELP_TEXT, limit=1900):
            await interaction.followup.send(chunk, ephemeral=True)

    @commands.command(name="helpbot")
    async def help_prefix(self, ctx: commands.Context) -> None:
        """Prefix help that does not replace discord.py DefaultHelpCommand."""
        for i, chunk in enumerate(chunk_message(HELP_TEXT, limit=1900)):
            if i == 0:
                await ctx.reply(chunk)
            else:
                await ctx.send(chunk)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HelpCmd(bot))
