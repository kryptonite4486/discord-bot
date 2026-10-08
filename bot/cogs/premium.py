"""/premium: this server's plan, its usage, and what each plan includes,
with a button for server admins to start the free trial.
/redeem: claim a gift code for this server.

The free trial is 14 days of Command, once per server ever and once per
server owner (TrialClaim in bot/db/database.py). While tiers are enforced it
starts by itself when the bot joins a new server (AUTO_TRIAL), with a
welcome message. Servers that joined before can start it with a button on
/premium rather than a command of its own: admins see it next to what the
trial unlocks, it only appears while the server can start one, and /premium
stays a single command.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from bot.utils.guild import guild_id_from_interaction
from bot.utils.gift_codes import AttemptLimiter, hash_code, normalize_code
from bot.utils.tiers import (
    FEATURE_NAMES,
    FREE,
    FULL,
    MID,
    TIERS,
    TRIAL_DAYS,
    TRIAL_TIER,
    TierStatus,
    quota_week_start,
    status_from_entitlements,
)

log = logging.getLogger(__name__)
CONTACT = "lastzassistant@gmail.com"


def _channel_cap(policy) -> str:
    return "any" if policy.max_channels is None else str(policy.max_channels)


PLANS = (FREE, MID, FULL)
# Label and column widths keep each table row at TABLE_WIDTH characters, so the
# code block doesn't wrap in a narrow window or on a phone.
LABEL_WIDTH = 20
TABLE_WIDTH = LABEL_WIDTH + 5 + 9 + 8


def _row(label: str, free, mid, full) -> str:
    return f"{label:<{LABEL_WIDTH}}{free!s:>5}{mid!s:>9}{full!s:>8}"


def format_premium(
    status: TierStatus,
    used: int,
    *,
    enforced: bool,
    channels: int | None = None,
    trial_note: str | None = None,
    now: datetime | None = None,
    support_url: str | None = None,
    trivia_questions: tuple[int, int, int] | None = None,
) -> str:
    policy = status.policy
    limit = policy.ocr_images_per_week
    plan = f"**Plan: {status.describe()}**"
    if status.source == "trial":
        plan += f" ({status.time_left(now)})"
    lines = [
        plan,
        f"Screenshots this week: **{used} / {limit}** "
        f"(week started {quota_week_start().isoformat()}, resets Sunday UTC)",
    ]
    if channels is not None:
        lines.append(f"Channels with data: **{channels} / {_channel_cap(policy)}**")
    if trial_note:
        lines.append(trial_note)
    lines += [
        "",
        "**What each plan includes**",
        "```",
        _row("", FREE.name, MID.name, FULL.name),
        _row("Screenshots / week", *(t.ocr_images_per_week for t in PLANS)),
        _row("Weeks of history", *(_weeks(t) for t in PLANS)),
        _row("Channels with data", *(_channel_cap(t) for t in PLANS)),
    ]
    if trivia_questions is not None:
        lines.append(_row("Trivia questions", *trivia_questions))
    for key, label in FEATURE_LABELS.items():
        lines.append(_row(label, *("✓" if t.allows(key) else "–" for t in PLANS)))
    lines.append("```")
    lines.append(
        "Manual entry, `/ingest image`, `/ingest text`, the weekly, versus, tech "
        "and leaderboard reports, and the mix-up flags are free on every plan."
    )
    if not enforced:
        lines.append(
            "\n_Plan limits aren't switched on yet, so everything currently works "
            "on every server._"
        )
    support = f"[support server](<{support_url}>) or " if support_url else ""
    lines.append(f"Paid plans aren't on sale yet. Questions: {support}{CONTACT}")
    return "\n".join(lines)


def _trivia_questions(bot) -> tuple[int, int, int] | None:
    """Questions per plan (Free, Alliance, Command), or None without trivia loaded."""
    cog = bot.get_cog("Trivia") if hasattr(bot, "get_cog") else None
    if cog is None:
        return None
    return tuple(len(cog.pools[t.key]) for t in PLANS)  # type: ignore[return-value]


def _weeks(policy) -> str:
    return "All" if policy.history_weeks is None else str(policy.history_weeks)


# Short labels for the comparison table, in display order.
FEATURE_LABELS = {
    "zip_batch": "/ingest zip & batch",
    "advanced_reports": "Player/trend/growth",
    "name_tools": "Duplicate names",
    "multi_channel_reports": "Server-wide reports",
    "clean_charts": "Unwatermarked charts",
    "export": "CSV/JSON export",
    "cross_server_trivia": "Cross-server trivia",
}
assert set(FEATURE_LABELS) == set(FEATURE_NAMES), "label every gated feature"
assert all(len(label) <= LABEL_WIDTH for label in FEATURE_LABELS.values()), "labels must fit"


REDEEM_FAILURES = {
    "format": "That doesn't look like a gift code. Codes look like `ABCD-EFGH-JKMN-PQRS`.",
    "invalid": "That code isn't valid. Check it and try again.",
    "revoked": "That code has been withdrawn and can't be redeemed.",
    "expired": "That code has expired.",
    "used_up": "That code has already been redeemed the maximum number of times.",
    "already": "This server has already redeemed that code.",
}


def redeem_success_message(tier: str, ends_at: str | None, status: TierStatus) -> str:
    name = TIERS[tier].name if tier in TIERS else tier
    lasts = f"until {ends_at[:10]}" if ends_at else "with no end date"
    text = f"🎉 Code redeemed: this server has the **{name}** plan {lasts}."
    if status.policy.key != tier or status.ends_at != ends_at:
        text += f" Its plan is now **{status.describe()}**."
    return text + " Run `/premium` to see what it includes."


def trial_available(status: TierStatus, claimed_at: str | None) -> bool:
    """Whether /premium offers the trial. The database checks again on start."""
    return claimed_at is None and status.policy.rank < TRIAL_TIER.rank


def trial_note(
    status: TierStatus, claimed_at: str | None, *, can_manage: bool, enforced: bool
) -> str | None:
    """The /premium line about the free trial, or None when there's nothing to say."""
    if status.source == "trial":
        return None  # the plan line already shows it
    if claimed_at is not None:
        return f"Free trial: used (started {claimed_at[:10]})."
    if not trial_available(status, claimed_at):
        return None  # already on that plan; the trial waits until it ends
    if not can_manage:
        return (
            f"🎁 A server admin (Manage Server) can start a free {TRIAL_DAYS}-day "
            f"**{TRIAL_TIER.name}** trial from `/premium`."
        )
    text = (
        f"🎁 **Free trial:** this server can try **{TRIAL_TIER.name}** free for "
        f"{TRIAL_DAYS} days, once. Press the button below to start it."
    )
    if not enforced:
        text += (
            " Plan limits aren't switched on yet, so a trial changes nothing for now; "
            "you may prefer to keep it for later."
        )
    return text


def trial_started_message(
    ends_at: str, after: TierStatus, *, enforced: bool, report_channel: bool
) -> str:
    lines = [
        f"🎉 Your free **{TRIAL_TIER.name}** trial has started. It runs until "
        f"**{ends_at[:10]}** ({ends_at[11:16]} UTC); after that the server goes back "
        f"to **{after.describe()}**. Nothing is deleted when it ends.",
        "You'll get a reminder in the report channel 3 days before it ends."
        if report_channel
        else "Set a report channel with `/setup` to get a reminder 3 days before it ends.",
    ]
    if not enforced:
        lines.append(
            "_Plan limits aren't switched on yet, so everything already works on every "
            "server. The trial is recorded, and it counts as this server's one trial._"
        )
    return "\n".join(lines)


def trial_refused_message(outcome: str, details: dict) -> str:
    if outcome == "owner_claimed":
        return (
            "This server's owner has already used a free trial on another server "
            f"(started {details['claimed_at'][:10]}). Each person gets one. "
            f"Run `/premium` to see the plans, or contact {CONTACT}."
        )
    if outcome == "claimed":
        return (
            f"This server has already used its free trial (started "
            f"{details['claimed_at'][:10]}). Each server gets one. "
            f"Run `/premium` to see the plans, or contact {CONTACT}."
        )
    row = details["entitlement"]
    status = TierStatus(TIERS[row["Tier"]], row["Source"], row["EndsAt"])
    if row["Source"] == "trial":
        return f"This server's free trial is already running: **{status.describe()}** ({status.time_left()})."
    return (
        f"This server already has **{status.describe()}**, so a trial would add nothing. "
        "The free trial stays available: you can start it from `/premium` if that plan ends."
    )


def welcome_message(ends_at: str, after: TierStatus, *, support_url: str) -> str:
    """Posted when the bot joins a new server and its free trial starts."""
    return "\n".join([
        f"👋 Thanks for adding LastZ Assistant! This server gets a free {TRIAL_DAYS}-day "
        f"**{TRIAL_TIER.name}** trial, which has started and runs until **{ends_at[:10]}** "
        f"({ends_at[11:16]} UTC). After that it goes back to **{after.describe()}**; "
        "nothing is deleted when it ends.",
        "Run `/help` to get started, `/setup` to pick a report channel (it gets a reminder "
        "3 days before the trial ends), and `/premium` to see the plans.",
        f"Questions? {support_url}",
    ])


def welcome_channel(guild: discord.Guild) -> discord.abc.Messageable | None:
    """Where to post the welcome: the server's system channel, else the
    first text channel the bot can see and send in."""
    me = guild.me
    channels = [guild.system_channel] if guild.system_channel else []
    channels += sorted(guild.text_channels, key=lambda c: c.position)
    for channel in channels:
        perms = channel.permissions_for(me)
        if perms.view_channel and perms.send_messages:
            return channel
    return None


class TrialView(discord.ui.View):
    """The "Start free trial" button under /premium (only shown to admins)."""

    def __init__(self, cog: "Premium", *, timeout: float = 600.0) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.message: discord.WebhookMessage | None = None

    @discord.ui.button(
        label=f"Start free {TRIAL_DAYS}-day {TRIAL_TIER.name} trial",
        style=discord.ButtonStyle.success,
        emoji="🎁",
    )
    async def start(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not getattr(interaction.permissions, "manage_guild", False):
            await interaction.response.send_message(
                "You need the Manage Server permission to start the trial.", ephemeral=True
            )
            return
        self.stop()
        await interaction.response.edit_message(view=None)
        owner_id = getattr(interaction.guild, "owner_id", None)
        text = await self.cog.start_trial(
            guild_id_from_interaction(interaction), interaction.user.id, owner_id=owner_id
        )
        await interaction.followup.send(text, ephemeral=True)

    async def on_timeout(self) -> None:
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


class Premium(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Failed /redeem attempts per user, to stop codes being guessed.
        self.redeem_limiter = AttemptLimiter(max_failures=5, window=15 * 60)

    @app_commands.command(name="premium", description="This server's plan, usage and what each plan includes")
    async def premium(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = guild_id_from_interaction(interaction)
        tiers = self.bot.tiers
        status = await tiers.status(guild_id)
        used = await tiers.ocr_used_this_week(guild_id)
        channels = len(await self.bot.db.tracked_channels(guild_id))
        claimed_at = await self.bot.db.trial_claimed_at(guild_id)
        can_manage = bool(getattr(interaction.permissions, "manage_guild", False))
        note = trial_note(status, claimed_at, can_manage=can_manage, enforced=tiers.enforced)
        text = format_premium(
            status,
            used,
            enforced=tiers.enforced,
            channels=channels,
            trial_note=note,
            support_url=self.bot.settings.support_url,
            trivia_questions=_trivia_questions(self.bot),
        )
        if can_manage and trial_available(status, claimed_at):
            view = TrialView(self)
            view.message = await interaction.followup.send(text, ephemeral=True, view=view, wait=True)
        else:
            await interaction.followup.send(text, ephemeral=True)

    def _trial_owner(self, owner_id: int | None) -> str | None:
        """The owner a trial counts against; None (no per-owner limit) for
        operators, who add the bot to test servers, or an unknown owner."""
        if owner_id is None or owner_id in self.bot.settings.bot_owner_ids:
            return None
        return str(owner_id)

    async def _start(
        self, guild_id: str, user_id: int | None, owner_id: int | None
    ) -> tuple[str, dict, TierStatus | None]:
        """Start the trial; on success also return the plan after it ends."""
        now = datetime.now(timezone.utc)
        outcome, details = await self.bot.db.start_trial(
            guild_id, None if user_id is None else str(user_id),
            tier=TRIAL_TIER.key, days=TRIAL_DAYS, now=now, owner_id=self._trial_owner(owner_id),
        )
        if outcome != "ok":
            log.info("Trial refused (%s) for guild %s by %s", outcome, guild_id,
                     f"user {user_id}" if user_id else "auto-trial on join")
            return outcome, details, None
        self.bot.tiers.invalidate(guild_id)
        active = await self.bot.db.active_entitlements(guild_id, now.strftime("%Y-%m-%d %H:%M:%S"))
        after = status_from_entitlements(
            [r for r in active if r["Id"] != details["entitlement_id"]
             and (r["EndsAt"] is None or r["EndsAt"] > details["ends_at"])]
        )
        log.warning("Guild %s started its free trial (entitlement #%d) by %s, until %s",
                    guild_id, details["entitlement_id"],
                    f"user {user_id}" if user_id else "auto-trial on join", details["ends_at"])
        return outcome, details, after

    async def start_trial(self, guild_id: str, user_id: int, *, owner_id: int | None = None) -> str:
        """Start this server's free trial if it can have one; return the reply text."""
        outcome, details, after = await self._start(guild_id, user_id, owner_id)
        if after is None:
            return trial_refused_message(outcome, details)
        return trial_started_message(
            details["ends_at"], after, enforced=self.bot.tiers.enforced,
            report_channel=await self.bot.db.report_channel_id(guild_id) is not None,
        )

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Start a new server's free trial and say hello (AUTO_TRIAL).

        Only while tiers are enforced: before that a trial changes nothing
        and would run out unused. A server that has had the bot before, or
        whose owner already had a trial elsewhere, gets nothing and no post.
        """
        settings = self.bot.settings
        if not settings.auto_trial or not self.bot.tiers.enforced:
            return
        if guild.id == settings.control_guild_id:
            return
        outcome, details, after = await self._start(str(guild.id), None, guild.owner_id)
        if after is None:
            return
        channel = welcome_channel(guild)
        if channel is None:
            log.info("Guild %s: no channel to post the welcome in", guild.id)
            return
        try:
            await channel.send(welcome_message(
                details["ends_at"], after, support_url=settings.support_url
            ))
        except discord.HTTPException:
            log.exception("Guild %s: welcome message failed", guild.id)

    @app_commands.command(name="redeem", description="Redeem a gift code for this server (Manage Server)")
    @app_commands.describe(code="The gift code, e.g. ABCD-EFGH-JKMN-PQRS")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def redeem(self, interaction: discord.Interaction, code: str) -> None:
        guild_id = guild_id_from_interaction(interaction)
        await interaction.response.send_message(
            await self.redeem_code(guild_id, interaction.user.id, code), ephemeral=True
        )

    async def redeem_code(self, guild_id: str, user_id: int, code: str) -> str:
        """Try to redeem ``code`` for a server; return the reply text."""
        wait = self.redeem_limiter.retry_after(user_id)
        if wait > 0:
            log.warning("/redeem rate-limited: user %s in guild %s", user_id, guild_id)
            return (
                "Too many attempts with codes that didn't work. "
                f"Try again in {max(1, round(wait / 60))} minute(s)."
            )
        normalized = normalize_code(code)
        if normalized is None:
            outcome, details = "format", {}
        else:
            outcome, details = await self.bot.db.redeem_gift_code(
                hash_code(normalized), guild_id, str(user_id), now=datetime.now(timezone.utc)
            )
        if outcome != "ok":
            self.redeem_limiter.record_failure(user_id)
            log.info("/redeem failed (%s) for user %s in guild %s", outcome, user_id, guild_id)
            return REDEEM_FAILURES[outcome]
        self.bot.tiers.invalidate(guild_id)
        status = await self.bot.tiers.status(guild_id)
        log.warning("Guild %s redeemed code #%d (entitlement #%d) by user %s",
                    guild_id, details["code_id"], details["entitlement_id"], user_id)
        return redeem_success_message(details["tier"], details["ends_at"], status)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            text = "You need the Manage Server permission to redeem a code."
        else:
            log.exception("%s error: %s", interaction.command, error)
            text = f"Error: `{error}`"
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Premium(bot))
