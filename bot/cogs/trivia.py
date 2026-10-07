"""Trivia: multiple-choice matches in one server, or across servers.

`/trivia start` runs a match in the current channel. With the default
**This server** mode it stays in that channel. With **Cross-server** it
opens a lobby that channels in other servers can join, by `/trivia join`,
`/trivia start mode:Cross-server`, or the Join button on the invitation the
bot posts in servers that set a trivia channel with /setup. Every joined
channel then gets the same questions at the same time, and all answers go
into one scoreboard that also totals points per server. Where a server has a
trivia channel, matches run only there (bot/utils/channel_rules.py).

Answers are buttons, so the bot never needs to read messages (it has no
Message Content intent). Matches live in memory and end if the bot
restarts; finished matches add to each player's totals in TriviaScore.

Plans: Free servers get the Last Z questions only, Alliance adds the
standard bank, and Command adds the extended bank and cross-server play
(hosting, joining and invitations). Without TIERS_ENFORCED every server
gets everything.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from bot.cogs.data import ConfirmDeleteView
from bot.trivia.engine import AnswerResult, Match, Mode, Player, RoundResult
from bot.trivia.questions import (
    DIFFICULTIES,
    TIER_KEYS,
    AskedQuestion,
    Question,
    categories,
    load_bank,
    pick_questions,
    pool_for,
)
from bot.utils.guild import guild_id_from_interaction
from bot.utils.tiers import FeatureLocked, requires_feature, upgrade_message

log = logging.getLogger(__name__)

LETTERS = "ABCD"
CROSS_SERVER = "cross_server_trivia"  # tier feature (bot/utils/tiers.py)
SERVER_COUNTDOWN = 5  # seconds between /trivia start and the first question
LOBBY_SECONDS = 45  # how long a cross-server lobby stays open
REVEAL_PAUSE = 5  # seconds the answer shows before the next question
MAX_CHANNELS = 20  # channels in one cross-server match
GLOBAL_START_COOLDOWN = 300  # per server, so invitations can't be spammed
RECENT_QUESTIONS = 300  # per server, asked again only once fresh ones run out
NAME_LIMIT = 32

MODE_CHOICES = [
    app_commands.Choice(name="This server", value=Mode.SERVER.value),
    app_commands.Choice(name="Cross-server", value=Mode.GLOBAL.value),
]
DIFFICULTY_CHOICES = [app_commands.Choice(name=d.title(), value=d) for d in DIFFICULTIES]
BOARD_CHOICES = [
    app_commands.Choice(name="This server", value="server"),
    app_commands.Choice(name="Cross-server", value="global"),
]

COLOR = discord.Color.blurple()
NO_MENTIONS = discord.AllowedMentions.none()


def _name(text: str) -> str:
    text = text if len(text) <= NAME_LIMIT else text[: NAME_LIMIT - 1] + "…"
    return discord.utils.escape_markdown(discord.utils.escape_mentions(text))


def player_label(player: Player, mode: Mode) -> str:
    if mode is Mode.GLOBAL:
        return f"**{_name(player.name)}** ({_name(player.guild_name)})"
    return f"**{_name(player.name)}**"


@dataclass
class Seat:
    """One channel taking part in a game."""

    guild_id: str
    guild_name: str
    channel: discord.abc.Messageable
    joined_by: int
    message: discord.Message | None = None  # lobby message, then each question


@dataclass
class Game:
    id: int
    match: Match
    host_id: int
    host_guild_name: str
    category: str | None
    difficulty: str | None
    starts_at: datetime
    seats: dict[int, Seat] = field(default_factory=dict)  # channel_id -> Seat
    invites: list[discord.Message] = field(default_factory=list)
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Set when everyone expected has answered, so the round can end early.
    round_done: asyncio.Event = field(default_factory=asyncio.Event)
    stopped_by: str | None = None
    task: asyncio.Task | None = None

    @property
    def mode(self) -> Mode:
        return self.match.mode

    def server_names(self) -> list[str]:
        return list(dict.fromkeys(s.guild_name for s in self.seats.values()))


# --- Embeds -----------------------------------------------------------------


def _settings_line(game: Game) -> str:
    return (
        f"**{len(game.match.questions)} questions** · {game.match.seconds:g}s each · "
        f"Category: {game.category or 'Any'} · "
        f"Difficulty: {(game.difficulty or 'any').title()}"
    )


def lobby_embed(game: Game) -> discord.Embed:
    starts = discord.utils.format_dt(game.starts_at, "R")
    if game.mode is Mode.SERVER:
        embed = discord.Embed(
            title="🧠 Trivia",
            description=(
                f"{_settings_line(game)}\nFirst question {starts}. Anyone in this "
                "channel can play: press a button to answer. Faster correct "
                "answers score more, and a question ends early once everyone "
                "playing has answered."
            ),
            color=COLOR,
        )
        return embed
    names = game.server_names()
    embed = discord.Embed(
        title="🌐 Cross-server trivia",
        description=(
            f"{_settings_line(game)}\nHosted from **{_name(game.host_guild_name)}**. "
            f"First question {starts}.\n\nPlay from another server with "
            "`/trivia join` in its trivia channel. Everyone in a joined channel "
            "can answer, and points also count toward their server's total."
        ),
        color=COLOR,
    )
    embed.add_field(
        name=f"Servers in ({len(names)})",
        value="\n".join(f"• {_name(n)}" for n in names[:MAX_CHANNELS]) or "—",
        inline=False,
    )
    return embed


def ended_lobby_embed(game: Game, note: str) -> discord.Embed:
    """A lobby message once its match ended before the first question."""
    title = "🧠 Trivia" if game.mode is Mode.SERVER else "🌐 Cross-server trivia"
    return discord.Embed(
        title=title, description=f"{_settings_line(game)}\n{note}", color=COLOR
    )


def invite_embed(game: Game) -> discord.Embed:
    return discord.Embed(
        title="🌐 Cross-server trivia is starting",
        description=(
            f"**{_name(game.host_guild_name)}** opened a match: {_settings_line(game)}.\n"
            f"Press **Join** to play from this channel. It starts "
            f"{discord.utils.format_dt(game.starts_at, 'R')}."
        ),
        color=COLOR,
    )


def question_embed(game: Game, asked: AskedQuestion) -> discord.Embed:
    q = asked.question
    n, total = game.match.index + 1, len(game.match.questions)
    closes = datetime.now(timezone.utc) + timedelta(seconds=game.match.seconds)
    embed = discord.Embed(
        title=f"Question {n}/{total}",
        description=f"**{discord.utils.escape_markdown(q.text)}**\n\n"
        + "\n".join(
            f"`{LETTERS[i]}` {discord.utils.escape_markdown(c)}"
            for i, c in enumerate(asked.choices)
        )
        + f"\n\nCloses {discord.utils.format_dt(closes, 'R')}",
        color=COLOR,
    )
    embed.set_footer(text=question_footer(q))
    return embed


def question_footer(q: Question) -> str:
    """Category and difficulty, plus the credit for questions that need one."""
    parts = [q.category, q.difficulty.title()]
    if q.credit:
        parts.append(f"via {q.credit}")
    return " · ".join(parts)


def _standings_lines(game: Game, limit: int) -> list[str]:
    return [
        f"{i}. {player_label(p, game.mode)} — {p.points:,} pts ({p.correct}/{p.answered})"
        for i, p in enumerate(game.match.standings()[:limit], start=1)
    ]


def reveal_embed(game: Game, result: RoundResult) -> discord.Embed:
    asked = result.asked
    total = len(game.match.questions)
    lines = []
    for i, choice in enumerate(asked.choices):
        mark = "✅" if i == asked.answer_index else "▫️"
        count = result.choice_counts[i]
        lines.append(f"{mark} `{LETTERS[i]}` {discord.utils.escape_markdown(choice)} — {count}")
    embed = discord.Embed(
        title=f"Question {result.index + 1}/{total}",
        description=f"**{discord.utils.escape_markdown(asked.question.text)}**\n\n"
        + "\n".join(lines),
        color=discord.Color.green(),
    )
    if result.winners:
        fastest = ", ".join(
            f"{player_label(p, game.mode)} +{pts}" for p, pts in result.winners[:5]
        )
        more = len(result.winners) - 5
        if more > 0:
            fastest += f" and {more} more"
        embed.add_field(
            name=f"Correct: {len(result.winners)} of {result.answered}",
            value=fastest[:1024],
            inline=False,
        )
    else:
        embed.add_field(
            name=f"Correct: 0 of {result.answered}",
            value="Nobody got this one.",
            inline=False,
        )
    standings = _standings_lines(game, 5)
    if standings:
        embed.add_field(name="Standings", value="\n".join(standings)[:1024], inline=False)
    embed.set_footer(text=question_footer(asked.question))
    return embed


def final_embed(game: Game) -> discord.Embed:
    winners = game.match.winners()
    if game.stopped_by:
        title = "Trivia stopped"
        head = f"Stopped by {game.stopped_by}."
    elif winners:
        title = "🏆 Trivia results"
        head = "Winner: " + ", ".join(player_label(p, game.mode) for p in winners)
    else:
        title = "Trivia results"
        head = "Nobody scored this time."
    embed = discord.Embed(title=title, description=head, color=discord.Color.gold())
    standings = _standings_lines(game, 10)
    embed.add_field(
        name="Final standings",
        value="\n".join(standings)[:1024] if standings else "Nobody answered.",
        inline=False,
    )
    if game.mode is Mode.GLOBAL:
        totals = game.match.guild_totals()
        if totals:
            embed.add_field(
                name="Servers",
                value="\n".join(
                    f"{i}. **{_name(name)}** — {pts:,} pts ({n} player{'s' if n != 1 else ''})"
                    for i, (_, name, pts, n) in enumerate(totals[:10], start=1)
                )[:1024],
                inline=False,
            )
    embed.set_footer(text="/trivia leaderboard shows all-time totals")
    return embed


def format_leaderboard(rows: list[dict], *, cross_server: bool) -> str:
    if not rows:
        return "No trivia scores yet. Start a match with `/trivia start`."
    lines = []
    for i, r in enumerate(rows, start=1):
        who = f"**{_name(r['DisplayName'])}**"
        if cross_server:
            who += f" ({_name(r['GuildName'])})"
        wins = f" · {r['Wins']} win{'s' if r['Wins'] != 1 else ''}" if r["Wins"] else ""
        lines.append(
            f"{i}. {who} — {r['Points']:,} pts · {r['Correct']}/{r['Answered']} correct"
            f" · {r['Games']} game{'s' if r['Games'] != 1 else ''}{wins}"
        )
    return "\n".join(lines)


# --- Views ------------------------------------------------------------------


class AnswerButton(discord.ui.Button["AnswerView"]):
    def __init__(self, index: int, text: str, *, style: discord.ButtonStyle, disabled: bool) -> None:
        label = f"{LETTERS[index]}. {text}"
        super().__init__(label=label[:80], style=style, disabled=disabled, row=index // 2)
        self.index = index

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.view is not None
        await self.view.cog.answer(interaction, self.view.game, self.view.round_index, self.index)


class AnswerView(discord.ui.View):
    def __init__(self, cog: Trivia, game: Game, round_index: int, *, reveal: bool = False) -> None:
        # A revealed question's buttons are disabled; the short timeout just
        # drops the view from discord.py's store (the message keeps them).
        super().__init__(timeout=5 if reveal else game.match.seconds + 30)
        self.cog, self.game, self.round_index = cog, game, round_index
        asked = game.match.questions[round_index]
        for i, choice in enumerate(asked.choices):
            style = discord.ButtonStyle.secondary
            if reveal and i == asked.answer_index:
                style = discord.ButtonStyle.success
            self.add_item(AnswerButton(i, choice, style=style, disabled=reveal))


class JoinView(discord.ui.View):
    """Join button on cross-server invitations."""

    def __init__(self, cog: Trivia, game: Game) -> None:
        super().__init__(timeout=LOBBY_SECONDS + 5)
        self.cog, self.game = cog, game

    @discord.ui.button(label="Join", emoji="🌐", style=discord.ButtonStyle.primary)
    async def join(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.join_lobby(interaction, self.game)


# --- Cog --------------------------------------------------------------------


class Trivia(commands.Cog):
    """`/trivia`: matches in one channel, or across servers."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        path = getattr(bot.settings, "trivia_questions_path", None)  # type: ignore[attr-defined]
        try:
            self.bank: list[Question] = load_bank(path)
        except (OSError, ValueError) as exc:
            if path is None:
                raise
            # A bad custom bank shouldn't keep the rest of the bot from starting.
            log.error("TRIVIA_QUESTIONS_PATH %s unusable (%s); using the bundled questions", path, exc)
            path, self.bank = None, load_bank()
        # Questions and categories per plan key ("free", "mid", "full").
        self.pools = {key: pool_for(self.bank, key) for key in TIER_KEYS}
        self.categories = {key: categories(pool) for key, pool in self.pools.items()}
        log.info(
            "Trivia bank: %d questions in %d categories (%s); per plan: %s",
            len(self.bank), len(self.categories["full"]), path or "bundled",
            ", ".join(f"{key} {len(pool)}" for key, pool in self.pools.items()),
        )
        self.games: dict[int, Game] = {}  # channel_id -> game
        self.lobby: Game | None = None  # the open cross-server lobby, if any
        self._ids = itertools.count(1)
        self._recent: dict[str, deque[str]] = {}
        self._last_global_start: dict[str, float] = {}

    async def cog_unload(self) -> None:
        tasks = {g.task for g in self.games.values() if g.task and not g.task.done()}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=5)

    trivia = app_commands.Group(name="trivia", description="Play trivia in this server or across servers")

    # --- Commands -----------------------------------------------------------

    @trivia.command(name="start", description="Start a trivia match in this channel")
    @app_commands.describe(
        mode="This server (default), or Cross-server: other servers can join",
        questions="Number of questions (default 10)",
        seconds="Seconds to answer each question (default 20)",
        category="Only ask questions from this category",
        difficulty="Only ask questions of this difficulty",
    )
    @app_commands.choices(mode=MODE_CHOICES, difficulty=DIFFICULTY_CHOICES)
    async def start(
        self,
        interaction: discord.Interaction,
        mode: app_commands.Choice[str] | None = None,
        questions: app_commands.Range[int, 3, 20] = 10,
        seconds: app_commands.Range[int, 10, 60] = 20,
        category: str | None = None,
        difficulty: app_commands.Choice[str] | None = None,
    ) -> None:
        cross_server = mode is not None and mode.value == Mode.GLOBAL.value
        if cross_server and not await self._ensure_cross_server(interaction):
            return
        if cross_server and self.lobby is not None:
            # One lobby at a time: starting a cross-server match joins it.
            await self.join_lobby(interaction, self.lobby, note_ignored=True)
            return
        if not await self._check_channel(interaction):
            return
        guild_id = guild_id_from_interaction(interaction)
        tier = await self._tier_key(guild_id)
        if category is not None:
            match = next(
                (c for c in self.categories[tier] if c.lower() == category.lower()), None
            )
            if match is None:
                await interaction.response.send_message(
                    f"Unknown category `{category}`. Pick one from the list: "
                    + ", ".join(self.categories[tier]),
                    ephemeral=True,
                )
                return
            category = match
        if cross_server:
            if not (await self.bot.db.trivia_settings(guild_id))["allow_global"]:
                await interaction.response.send_message(
                    "Cross-server trivia is turned off in this server. An admin can "
                    "turn it on with `/trivia settings cross_server:True`.",
                    ephemeral=True,
                )
                return
            wait = self._global_cooldown(guild_id)
            if wait:
                await interaction.response.send_message(
                    f"This server started a cross-server match recently. Try again in "
                    f"{wait // 60 + 1} minute(s), or play in this server only.",
                    ephemeral=True,
                )
                return

        diff = difficulty.value if difficulty else None
        asked = pick_questions(
            self.pools[tier], questions, category=category, difficulty=diff,
            avoid=self._recent.get(guild_id, ()),
        )
        if len(asked) < 3:
            await interaction.response.send_message(
                "Not enough questions match that category and difficulty. Try a "
                "broader choice.",
                ephemeral=True,
            )
            return
        self._remember(guild_id, asked)

        game_mode = Mode.GLOBAL if cross_server else Mode.SERVER
        countdown = LOBBY_SECONDS if cross_server else SERVER_COUNTDOWN
        game = Game(
            id=next(self._ids),
            match=Match(mode=game_mode, questions=asked, seconds=float(seconds)),
            host_id=interaction.user.id,
            host_guild_name=interaction.guild.name if interaction.guild else "?",
            category=category,
            difficulty=diff,
            starts_at=datetime.now(timezone.utc) + timedelta(seconds=countdown),
        )
        seat = self._seat(interaction)
        game.seats[interaction.channel_id] = seat
        self.games[interaction.channel_id] = game
        if cross_server:
            self.lobby = game
            self._last_global_start[guild_id] = time.monotonic()

        note = ""
        if len(asked) < questions:
            note = f"Only {len(asked)} questions match, so the match is shorter."
        await interaction.response.send_message(
            note or None, embed=lobby_embed(game), allowed_mentions=NO_MENTIONS
        )
        seat.message = await interaction.original_response()
        log.info(
            "Trivia game %d (%s) started by %s in guild %s channel %s: %d questions",
            game.id, game_mode.value, interaction.user.id, guild_id,
            interaction.channel_id, len(asked),
        )
        game.task = asyncio.create_task(self._run(game, countdown))
        if cross_server:
            await self._send_invites(game, exclude_guild=guild_id)

    @start.autocomplete("category")
    async def _category_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        current = current.lower()
        tier = await self._tier_key(guild_id_from_interaction(interaction))
        return [
            app_commands.Choice(name=c, value=c)
            for c in self.categories[tier]
            if current in c.lower()
        ][:25]

    @trivia.command(name="join", description="Join the open cross-server trivia match from this channel")
    @requires_feature(CROSS_SERVER)
    async def join(self, interaction: discord.Interaction) -> None:
        if self.lobby is None:
            await interaction.response.send_message(
                "No cross-server match is open right now. Open one with "
                "`/trivia start mode:Cross-server`.",
                ephemeral=True,
            )
            return
        await self.join_lobby(interaction, self.lobby)

    @trivia.command(name="stop", description="Stop the trivia match in this channel")
    async def stop(self, interaction: discord.Interaction) -> None:
        game = self.games.get(interaction.channel_id)
        if game is None:
            await interaction.response.send_message(
                "No trivia match is running in this channel.", ephemeral=True
            )
            return
        seat = game.seats[interaction.channel_id]
        allowed = (
            interaction.user.id in (game.host_id, seat.joined_by)
            or interaction.permissions.manage_messages
        )
        if not allowed:
            await interaction.response.send_message(
                "Only whoever started or joined this match here, or someone with "
                "Manage Messages, can stop it.",
                ephemeral=True,
            )
            return
        who = _name(interaction.user.display_name)
        if game.mode is Mode.GLOBAL and len(game.seats) > 1:
            # Other servers keep playing; this channel just leaves.
            self._release_seat(game, interaction.channel_id)
            await interaction.response.send_message(
                f"This channel left the cross-server match ({who} stopped it here). "
                "The other servers carry on.",
                allowed_mentions=NO_MENTIONS,
            )
            if game.match.index < 0:
                await self._retire_lobby(
                    game, [seat], f"This channel left before the first question ({who})."
                )
                await self._refresh_lobby(game)
            return
        game.stopped_by = who
        game.stop_event.set()
        await interaction.response.send_message(
            f"Stopping trivia… ({who})", allowed_mentions=NO_MENTIONS
        )

    @trivia.command(name="leaderboard", description="All-time trivia scores")
    @app_commands.describe(scope="This server (default), or the cross-server board")
    @app_commands.choices(scope=BOARD_CHOICES)
    async def leaderboard(
        self, interaction: discord.Interaction, scope: app_commands.Choice[str] | None = None
    ) -> None:
        cross_server = scope is not None and scope.value == "global"
        rows = await self.bot.db.trivia_leaderboard(
            guild_id=None if cross_server else guild_id_from_interaction(interaction),
            limit=15,
        )
        embed = discord.Embed(
            title="🌐 Cross-server trivia leaderboard"
            if cross_server
            else f"🧠 Trivia leaderboard: {_name(interaction.guild.name)}",
            description=format_leaderboard(rows, cross_server=cross_server),
            color=COLOR,
        )
        if cross_server:
            embed.set_footer(text="Points from cross-server matches, per player per server")
        else:
            embed.set_footer(text="Points from every match played in this server")
        await interaction.response.send_message(embed=embed, allowed_mentions=NO_MENTIONS)

    @trivia.command(name="settings", description="Trivia options for this server (Manage Server)")
    @app_commands.describe(
        cross_server="Allow members to play cross-server matches from this server",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def settings(
        self, interaction: discord.Interaction, cross_server: bool | None = None
    ) -> None:
        guild_id = guild_id_from_interaction(interaction)
        current = (await self.bot.db.trivia_settings(guild_id))["allow_global"]
        allow = current if cross_server is None else cross_server
        if allow != current:
            await self.bot.db.set_trivia_settings(guild_id, allow_global=allow)
            log.info(
                "Trivia settings for guild %s by %s: cross_server=%s",
                guild_id, interaction.user.id, allow,
            )
        channel_id = (await self.bot.db.guild_settings(guild_id))["trivia_channel_id"]
        where = (
            f"<#{channel_id}>: matches run only there, and invitations to other "
            "servers' cross-server matches are posted there"
            if channel_id
            else "not set, so matches can run anywhere and no invitations are posted"
        )
        await interaction.response.send_message(
            "**Trivia settings**\n"
            f"• Cross-server play: **{'on' if allow else 'off'}**"
            + ("" if allow else " (members can't join or open cross-server matches, "
               "this server gets no invitations, and it's left off the "
               "cross-server leaderboard)")
            + f"\n• Trivia channel: {where}. Change it with `/setup`.",
            ephemeral=True,
        )

    @trivia.command(name="reset", description="Delete this server's trivia scores (Administrator)")
    @app_commands.checks.has_permissions(administrator=True)
    async def reset(self, interaction: discord.Interaction) -> None:
        guild_id = guild_id_from_interaction(interaction)
        rows = await self.bot.db.trivia_leaderboard(guild_id=guild_id, limit=10_000)
        if not rows:
            await interaction.response.send_message(
                "This server has no trivia scores.", ephemeral=True
            )
            return
        view = ConfirmDeleteView(interaction.user.id)
        await interaction.response.send_message(
            f"⚠️ This permanently deletes trivia scores for **{len(rows)}** player(s) "
            "in this server, including their cross-server points from here.",
            view=view,
            ephemeral=True,
        )
        if await view.wait():
            await interaction.edit_original_response(
                content="Timed out. Nothing was deleted.", view=None
            )
            return
        if not view.confirmed:
            return
        deleted = await self.bot.db.delete_trivia_scores(guild_id)
        log.warning(
            "/trivia reset by %s: %d score row(s) in guild %s",
            interaction.user.id, deleted, guild_id,
        )
        await interaction.edit_original_response(
            content=f"Deleted trivia scores for **{len(rows)}** player(s)."
        )

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, FeatureLocked):
            return  # the upgrade message was already sent
        if isinstance(error, app_commands.MissingPermissions):
            needed = " and ".join(p.replace("_", " ").title() for p in error.missing_permissions)
            text = f"You need the {needed} permission for this command."
        else:
            log.exception("Trivia command error: %s", error)
            text = f"Error: `{error}`"
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    # --- Lobby --------------------------------------------------------------

    async def join_lobby(
        self, interaction: discord.Interaction, game: Game, *, note_ignored: bool = False
    ) -> None:
        if self.lobby is not game:
            await interaction.response.send_message(
                "That match has already started or ended.", ephemeral=True
            )
            return
        if interaction.channel_id in game.seats:
            await interaction.response.send_message(
                "This channel is already in the match. Everyone here can answer.",
                ephemeral=True,
            )
            return
        if not await self._check_channel(interaction):
            return
        if not await self._ensure_cross_server(interaction):
            return
        guild_id = guild_id_from_interaction(interaction)
        if not (await self.bot.db.trivia_settings(guild_id))["allow_global"]:
            await interaction.response.send_message(
                "Cross-server trivia is turned off in this server. An admin can turn "
                "it on with `/trivia settings cross_server:True`.",
                ephemeral=True,
            )
            return
        if len(game.seats) >= MAX_CHANNELS:
            await interaction.response.send_message(
                f"That match is full ({MAX_CHANNELS} channels).", ephemeral=True
            )
            return
        seat = self._seat(interaction)
        game.seats[interaction.channel_id] = seat
        self.games[interaction.channel_id] = game
        note = (
            "A cross-server match was already open, so this channel joined it "
            "(its question settings apply)."
            if note_ignored
            else None
        )
        await interaction.response.send_message(
            note, embed=lobby_embed(game), allowed_mentions=NO_MENTIONS
        )
        seat.message = await interaction.original_response()
        log.info(
            "Trivia game %d: guild %s channel %s joined (%d channels)",
            game.id, guild_id, interaction.channel_id, len(game.seats),
        )
        await self._refresh_lobby(game, skip=interaction.channel_id)

    async def _send_invites(self, game: Game, *, exclude_guild: str) -> None:
        for guild_id, channel_id in await self.bot.db.trivia_announce_channels():
            if guild_id == exclude_guild or not await self._has_cross_server(guild_id):
                continue
            channel = self.bot.get_channel(int(channel_id))
            if channel is None or not isinstance(channel, discord.abc.Messageable):
                continue
            try:
                msg = await channel.send(
                    embed=invite_embed(game), view=JoinView(self, game),
                    allowed_mentions=NO_MENTIONS,
                )
                game.invites.append(msg)
            except discord.HTTPException as exc:
                log.warning("Trivia invite to channel %s failed: %s", channel_id, exc)

    async def _refresh_lobby(self, game: Game, *, skip: int | None = None) -> None:
        embed = lobby_embed(game)
        await asyncio.gather(
            *(
                s.message.edit(embed=embed)
                for cid, s in game.seats.items()
                if s.message is not None and cid != skip
            ),
            return_exceptions=True,
        )

    async def _retire_lobby(self, game: Game, seats: list[Seat], note: str) -> None:
        """Replace the countdown on these channels' lobby messages with ``note``."""
        embed = ended_lobby_embed(game, note)
        await asyncio.gather(
            *(s.message.edit(embed=embed) for s in seats if s.message is not None),
            return_exceptions=True,
        )

    async def _close_lobby(self, game: Game) -> None:
        if self.lobby is game:
            self.lobby = None
        started = discord.Embed(
            title="🌐 Cross-server trivia",
            description=f"The match from **{_name(game.host_guild_name)}** has started. "
            "Open your own with `/trivia start mode:Cross-server`.",
            color=COLOR,
        )
        await asyncio.gather(
            *(m.edit(embed=started, view=None) for m in game.invites),
            return_exceptions=True,
        )

    # --- Playing ------------------------------------------------------------

    async def answer(
        self, interaction: discord.Interaction, game: Game, round_index: int, choice: int
    ) -> None:
        if game.match.index != round_index or interaction.channel_id not in game.seats:
            result = AnswerResult.CLOSED
        else:
            result = game.match.answer(
                interaction.user.id,
                choice,
                asyncio.get_running_loop().time(),
                guild_id=str(interaction.guild_id),
                guild_name=interaction.guild.name if interaction.guild else "?",
                name=interaction.user.display_name,
            )
            if result is AnswerResult.LOCKED and game.match.everyone_answered:
                game.round_done.set()
        asked = game.match.questions[round_index]
        text = {
            AnswerResult.LOCKED: f"🔒 Locked in **{LETTERS[choice]}. "
            f"{discord.utils.escape_markdown(asked.choices[choice])}**",
            AnswerResult.ALREADY_ANSWERED: "You've already answered this question.",
            AnswerResult.CLOSED: "Time's up for this question.",
        }[result]
        await interaction.response.send_message(text, ephemeral=True)

    async def _run(self, game: Game, countdown: float) -> None:
        # Shown on the lobby messages if the match ends before its first question.
        lobby_note = "Cancelled before the first question."
        try:
            started = not await self._pause(game, countdown)
            if game.mode is Mode.GLOBAL:
                await self._close_lobby(game)
            loop = asyncio.get_running_loop()
            while started and game.match.has_next and game.seats:
                game.round_done.clear()
                asked = game.match.open_round(loop.time())
                await self._broadcast_question(game, asked)
                stopped = await self._pause(game, game.match.seconds, until=game.round_done)
                result = game.match.close_round()
                await self._broadcast_reveal(game, result)
                if stopped or (game.match.has_next and await self._pause(game, REVEAL_PAUSE)):
                    break
            await self._finish(game)
        except asyncio.CancelledError:
            lobby_note = "Cancelled: the bot restarted before the first question."
            await self._post_all(
                game, discord.Embed(description="Trivia stopped: the bot is restarting.")
            )
            raise
        except Exception:
            log.exception("Trivia game %d failed", game.id)
            await self._post_all(
                game, discord.Embed(description="Trivia stopped after an error. Sorry!")
            )
        finally:
            if self.lobby is game:
                self.lobby = None
            if game.match.index < 0:
                if game.stopped_by:
                    lobby_note = f"Stopped by {game.stopped_by} before the first question."
                await self._retire_lobby(game, list(game.seats.values()), lobby_note)
            for channel_id in list(game.seats):
                self._release_seat(game, channel_id)

    async def _pause(
        self, game: Game, seconds: float, *, until: asyncio.Event | None = None
    ) -> bool:
        """Wait ``seconds``, or until ``until`` is set; True if the game was stopped meanwhile."""
        waiters = [asyncio.create_task(e.wait()) for e in (game.stop_event, until) if e]
        try:
            await asyncio.wait(waiters, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
        return game.stop_event.is_set()

    async def _broadcast_question(self, game: Game, asked: AskedQuestion) -> None:
        embed = question_embed(game, asked)
        seats = list(game.seats.items())
        sent = await asyncio.gather(
            *(
                s.channel.send(
                    embed=embed, view=AnswerView(self, game, game.match.index),
                    allowed_mentions=NO_MENTIONS,
                )
                for _, s in seats
            ),
            return_exceptions=True,
        )
        for (channel_id, seat), msg in zip(seats, sent):
            if isinstance(msg, BaseException):
                # Lost access (permissions changed, channel deleted): drop out.
                log.warning("Trivia game %d: channel %s dropped: %s", game.id, channel_id, msg)
                self._release_seat(game, channel_id)
            else:
                seat.message = msg

    async def _broadcast_reveal(self, game: Game, result: RoundResult) -> None:
        embed = reveal_embed(game, result)
        await asyncio.gather(
            *(
                s.message.edit(
                    embed=embed, view=AnswerView(self, game, result.index, reveal=True)
                )
                for s in game.seats.values()
                if s.message is not None
            ),
            return_exceptions=True,
        )

    async def _finish(self, game: Game) -> None:
        await self._post_all(game, final_embed(game))
        winners = {p.user_id for p in game.match.winners()} if not game.stopped_by else set()
        players = [
            {
                "guild_id": p.guild_id, "user_id": p.user_id, "name": p.name,
                "guild_name": p.guild_name, "points": p.points, "correct": p.correct,
                "answered": p.answered, "won": p.user_id in winners,
            }
            for p in game.match.players.values()
            if p.answered
        ]
        await self.bot.db.record_trivia_match(game.mode.value, players)
        log.info(
            "Trivia game %d finished%s: %d player(s), %d channel(s)",
            game.id, " (stopped)" if game.stopped_by else "", len(players), len(game.seats),
        )

    async def _post_all(self, game: Game, embed: discord.Embed) -> None:
        await asyncio.gather(
            *(
                s.channel.send(embed=embed, allowed_mentions=NO_MENTIONS)
                for s in game.seats.values()
            ),
            return_exceptions=True,
        )

    # --- Helpers ------------------------------------------------------------

    async def _check_channel(self, interaction: discord.Interaction) -> bool:
        problem = None
        if interaction.channel_id in self.games:
            problem = (
                "A trivia match is already running in this channel. "
                "`/trivia stop` ends it."
            )
        elif not isinstance(interaction.channel, discord.abc.Messageable):
            problem = "Trivia can only be played in a text channel or thread."
        else:
            perms = interaction.app_permissions
            if not (perms.send_messages and perms.embed_links):
                problem = (
                    "I need Send Messages and Embed Links in this channel to run trivia."
                )
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return False
        return True

    async def _tier_key(self, guild_id: str) -> str:
        """Plan key whose questions this server gets."""
        tiers = getattr(self.bot, "tiers", None)
        if tiers is None:
            return "full"
        return (await tiers.trivia_tier(guild_id)).key

    async def _ensure_cross_server(self, interaction: discord.Interaction) -> bool:
        """Reply with the upgrade message and return False if the plan lacks cross-server play."""
        tiers = getattr(self.bot, "tiers", None)
        if tiers is None or interaction.guild_id is None:
            return True
        allowed, status = await tiers.check_feature(str(interaction.guild_id), CROSS_SERVER)
        if not allowed:
            await interaction.response.send_message(
                upgrade_message(CROSS_SERVER, status), ephemeral=True
            )
        return allowed

    async def _has_cross_server(self, guild_id: str) -> bool:
        """Whether to invite this server; quiet, unlike check_feature's log line."""
        tiers = getattr(self.bot, "tiers", None)
        if tiers is None or not tiers.enforced:
            return True
        return (await tiers.status(guild_id)).policy.allows(CROSS_SERVER)

    def _seat(self, interaction: discord.Interaction) -> Seat:
        return Seat(
            guild_id=str(interaction.guild_id),
            guild_name=interaction.guild.name if interaction.guild else "?",
            channel=interaction.channel,  # type: ignore[arg-type]
            joined_by=interaction.user.id,
        )

    def _release_seat(self, game: Game, channel_id: int) -> None:
        game.seats.pop(channel_id, None)
        if self.games.get(channel_id) is game:
            del self.games[channel_id]
        if not game.seats:
            game.stop_event.set()

    def _remember(self, guild_id: str, asked: list[AskedQuestion]) -> None:
        recent = self._recent.setdefault(guild_id, deque(maxlen=RECENT_QUESTIONS))
        recent.extend(a.question.id for a in asked)

    def _global_cooldown(self, guild_id: str) -> int:
        """Seconds until this server may open another cross-server lobby."""
        last = self._last_global_start.get(guild_id)
        if last is None:
            return 0
        return max(0, int(GLOBAL_START_COOLDOWN - (time.monotonic() - last)))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Trivia(bot))
