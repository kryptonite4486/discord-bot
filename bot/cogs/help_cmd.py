"""/help: the user-facing command list."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot.utils.parsing import chunk_message

HELP_TEXT = """\
**LastZ Assistant — Commands**

Data is stored **per Discord server + channel**. Commands only work in a server
(DMs unsupported).

```
Admin (server administrators only)
  /admin stats           Datastore stats (this channel; scope:server for all)
  /admin duplicates      List player names stored under several spellings
  /admin rename-player   Move a player's rows to the correct spelling
  /data delete           Permanently delete this channel's (or server's) data

Add / Ingest
  /add versus       Add Versus Points for a player
  /add tech         Add Tech Contribution for a player
  /add arena        Add Arena Power for a player
  /add kills        Add total Kills for a player
  /add general      Add HQ Level and Power
  /ingest image     OCR up to 10 images on this command
  /ingest zip       OCR every image in a .zip (up to 50)
  /ingest batch     Collect images/zips from messages that @mention the bot
                    (20; 50 with zips); finish with `@LastZ Assistant done`
  /ingest text      Ingest pasted CSV/text rows

Reports
  /report week      Weekly summary — all players
  /report player    Player history + chart
  /report versus    Versus Points leaderboard
  /report tech      Tech Contribution leaderboard
  /report trend     Multi-week metric trends
  /report growth    Top/bottom 15 growth + full .csv file
  /report leaderboard  Ranked list (optional limit) + chart

Report scope (same server only — never cross-server)
  Every /report requires **scope**:
    • Select Channels — picker of channels that have ingested data
    • All Channels — every channel with data in this server
  Optional channel / channel2 / channel3 skip the picker (shortcut).
  Multi-channel reports show a Channel column (#name).
  Player values are not summed across channels; each row stays separate.
  Charts are omitted when more than one channel is included.

Planner
  /planner          Link the territory planner web app
  /planner plan:    Share a plan (paste a link from its Share link button)

Help
  /help             Show this command list
```

Week args accept `YYYY-MM-DD`, `current`, or `last` (normalized to Sunday).
Discord allows ~10 attachments per message; use `/ingest zip` or `/ingest batch`
for larger sets. Zip uploads are bound by your server's file size limit.

Every image import needs a **dataset**: versus, tech, general, power, arena or kills.
Lookalike screens: General vs Arena (member cards) and Power vs Kills (leaderboards).
The bot flags (🚩) likely mistakes: Arena Power above total Power in the same week
(misread) or equal to it for many players (mix-up), Power collapsing or jumping
5x+ week over week (e.g. a dropped decimal), or Kills going down or jumping 5x+.
"""


class HelpCmd(commands.Cog):
    """User-facing command help (`/help`)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="help", description="List bot slash commands")
    async def help_slash(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        for chunk in chunk_message(HELP_TEXT, limit=1900):
            await interaction.followup.send(chunk, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HelpCmd(bot))
