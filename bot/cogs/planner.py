"""Link to the Last Z territory planner web app (hosted separately)."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

import discord
from discord import app_commands
from discord.ext import commands

# Share links carry the whole plan in the URL fragment: `#plan=<base64url>`.
_PLAN_FRAGMENT = re.compile(r"plan=[A-Za-z0-9_-]+")
# Discord rejects link buttons whose URL is longer than this.
_BUTTON_URL_LIMIT = 512


def parse_share_link(link: str, planner_url: str) -> str | None:
    """Return `link` if it is a share link for this planner, else None.

    Only links on the planner's own host are accepted, so the bot never
    reposts an arbitrary URL under a "shared plan" label.
    """
    link = link.strip().strip("<>")
    try:
        parts, base = urlsplit(link), urlsplit(planner_url)
    except ValueError:
        return None
    if parts.scheme != "https" or parts.netloc.lower() != base.netloc.lower():
        return None
    if not _PLAN_FRAGMENT.fullmatch(parts.fragment):
        return None
    return link


def _link_view(label: str, url: str) -> discord.ui.View | None:
    """A single link button, or None when Discord would reject the URL."""
    if len(url) > _BUTTON_URL_LIMIT:
        return None
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label=label, url=url))
    return view


def _or_missing(view: discord.ui.View | None) -> discord.ui.View:
    # send_message treats MISSING as "no components"; None would raise.
    return discord.utils.MISSING if view is None else view


class Planner(commands.Cog):
    """`/planner` posts the territory planner link, or a shared plan."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def planner_url(self) -> str:
        return self.bot.settings.planner_url  # type: ignore[attr-defined]

    @app_commands.command(
        name="planner",
        description="Link the territory planner, or share a plan from it",
    )
    @app_commands.describe(
        plan="Optional: a plan link from the planner's Share link button",
    )
    async def planner(
        self, interaction: discord.Interaction, plan: str | None = None
    ) -> None:
        if plan is None:
            await interaction.response.send_message(
                "**Territory Planner** — click territories to assign them to "
                "alliances, check connection rules and limits, and share plans. "
                "Plans are saved in your own browser.",
                view=_or_missing(_link_view("Open the planner", self.planner_url)),
            )
            return

        link = parse_share_link(plan, self.planner_url)
        if link is None:
            await interaction.response.send_message(
                "That isn't a planner share link. In the planner, click "
                f"**Share link** and paste the result. It starts with "
                f"`{self.planner_url}/#plan=`.",
                ephemeral=True,
            )
            return

        view = _link_view("Open shared plan", link)
        # Long plans exceed Discord's button URL limit; fall back to a
        # masked link in the message itself (messages allow 2000 chars).
        text = f"{interaction.user.mention} shared a territory plan."
        if view is None:
            text += f" [Open shared plan]({link})"
        await interaction.response.send_message(
            text,
            view=_or_missing(view),
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Planner(bot))
