"""Reminders before gifted plans and free trials expire
(docs/monetization-plan.md sections 2 and 6).

Two stages, each sent once per entitlement and end date:

- operator_7d: one DM to each operator in BOT_OWNER_IDS listing gifts
  (Source 'gift' or 'code') that end within 7 days, with the /ops extend
  command for each. Trials aren't included: they end on their own.
- server_3d: a heads-up in the server's report channel 3 days before a gift
  or trial ends. A trial's heads-up spells out what changes when it ends.
  Skipped while an active paid subscription outlasts it, for a trial when
  another plan keeps the server on the same tier, and when the server has
  no report channel set.

What was sent is stored in EntitlementReminder, keyed on EndsAt, so restarts
don't repeat a reminder and extending a gift re-arms both stages. A send
that fails (or is skipped) isn't recorded, so the next run tries again.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable

import discord

from bot.utils.retention import fmt_time, utc_now
from bot.utils.tiers import FEATURE_NAMES, TIERS, TierStatus, status_from_entitlements

log = logging.getLogger(__name__)

GIFT_SOURCES = frozenset({"gift", "code"})
PAID_SOURCES = frozenset({"discord", "stripe"})
TRIAL_SOURCE = "trial"

STAGE_OPERATOR = "operator_7d"
STAGE_SERVER = "server_3d"
OPERATOR_WINDOW = timedelta(days=7)
SERVER_WINDOW = timedelta(days=3)

# Discord's limit is 2,000 characters per message.
MAX_MESSAGE = 1900


def tier_name(tier: str) -> str:
    return TIERS[tier].name if tier in TIERS else tier


def gifts_ending_within(
    rows: list[dict[str, Any]], now: datetime, window: timedelta, sources: frozenset[str] = GIFT_SOURCES
) -> list[dict[str, Any]]:
    """Active entitlements from ``sources`` (gifts and code redemptions by
    default) ending within ``window`` of ``now``."""
    cutoff = fmt_time(now + window)
    return [r for r in rows if r["Source"] in sources and r["EndsAt"] and r["EndsAt"] <= cutoff]


def status_after(ending: dict[str, Any], active: list[dict[str, Any]]) -> TierStatus:
    """The server's plan once ``ending`` ends, from the plans that outlast it."""
    return status_from_entitlements([
        r for r in active
        if r["GuildId"] == ending["GuildId"] and r["Id"] != ending["Id"]
        and (r["EndsAt"] is None or r["EndsAt"] > ending["EndsAt"])
    ])


def paid_outlasts(gift: dict[str, Any], active: list[dict[str, Any]]) -> bool:
    """Whether an active paid subscription in the gift's server ends after it."""
    return any(
        r["Source"] in PAID_SOURCES
        and r["GuildId"] == gift["GuildId"]
        and (r["EndsAt"] is None or r["EndsAt"] > gift["EndsAt"])
        for r in active
    )


def format_operator_summary(gifts: list[dict[str, Any]], names: dict[str, str]) -> list[str]:
    """The operator DM, split into messages under Discord's length limit."""
    lines = [f"**{len(gifts)} gifted plan(s) end within {OPERATOR_WINDOW.days} days**"]
    for g in gifts:
        name = names.get(g["GuildId"], "Unknown server (bot not in it)")
        lines.append(
            f"• **{name}** `{g['GuildId']}`: {tier_name(g['Tier'])} ({g['Source']}) "
            f"ends {g['EndsAt'][:16]} UTC — "
            f"`/ops extend entitlement_id:{g['Id']} duration:30 days`"
        )
    messages, current = [], ""
    for line in lines:
        if current and len(current) + 1 + len(line) > MAX_MESSAGE:
            messages.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    messages.append(current)
    return messages


def format_server_heads_up(gift: dict[str, Any], after: TierStatus) -> str:
    return (
        f"⏳ Heads-up: this server's gifted **{tier_name(gift['Tier'])}** plan ends on "
        f"**{gift['EndsAt'][:10]}** ({gift['EndsAt'][11:16]} UTC). After that the server "
        f"will be on **{after.describe()}**. Run `/premium` to see what each plan includes."
    )


def format_trial_heads_up(trial: dict[str, Any], after: TierStatus, *, enforced: bool) -> str:
    """The trial's 3-day heads-up: when it ends and what the server loses."""
    trial_plan = TIERS[trial["Tier"]]
    plan = after.policy
    lines = [
        f"⏳ Heads-up: this server's free **{trial_plan.name}** trial ends on "
        f"**{trial['EndsAt'][:10]}** ({trial['EndsAt'][11:16]} UTC). After that the server "
        f"will be on **{after.describe()}**:",
        f"• screenshots: {plan.ocr_images_per_week:,} a week instead of "
        f"{trial_plan.ocr_images_per_week:,}",
    ]
    if plan.history_weeks != trial_plan.history_weeks:
        lines.append(
            f"• reports show the last {plan.history_weeks} weeks; older weeks are hidden, "
            "not deleted"
        )
    if plan.max_channels != trial_plan.max_channels:
        lines.append(
            f"• new data in up to {plan.max_channels} channel(s); the others stay in "
            "reports but take no new data"
        )
    lost = [FEATURE_NAMES[f] for f in FEATURE_NAMES if trial_plan.allows(f) and not plan.allows(f)]
    if lost:
        lines.append("• no longer included: " + "; ".join(lost))
    lines.append(
        "Nothing is deleted. Run `/premium` to see what each plan includes."
    )
    if not enforced:
        lines.append(
            "_Plan limits aren't switched on yet, so nothing changes for now._"
        )
    return "\n".join(lines)


class GiftReminders:
    """Finds due reminders and sends them. ``clock`` is injectable for tests."""

    def __init__(self, bot, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.bot = bot
        self.clock = clock

    @property
    def db(self):
        return self.bot.db

    async def run(self) -> None:
        now = self.clock()
        active = await self.db.all_active_entitlements(fmt_time(now))
        await self.remind_operators(active, now)
        await self.remind_servers(active, now)

    async def _unsent(self, gifts: list[dict[str, Any]], stage: str) -> list[dict[str, Any]]:
        return [g for g in gifts if not await self.db.reminder_sent(g["Id"], stage, g["EndsAt"])]

    async def remind_operators(self, active: list[dict[str, Any]], now: datetime) -> int:
        """DM each operator one summary of newly due gifts; return how many."""
        operators = sorted(self.bot.settings.bot_owner_ids)
        if not operators:
            return 0
        due = await self._unsent(gifts_ending_within(active, now, OPERATOR_WINDOW), STAGE_OPERATOR)
        if not due:
            return 0
        names = {str(g.id): g.name for g in self.bot.guilds}
        messages = format_operator_summary(due, names)
        delivered = 0
        for user_id in operators:
            try:
                user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                for text in messages:
                    await user.send(text)
                delivered += 1
            except discord.HTTPException:
                log.warning("Couldn't DM gift expiry summary to operator %s", user_id, exc_info=True)
        if not delivered:
            return 0  # try again next run
        for g in due:
            await self.db.mark_reminder_sent(g["Id"], STAGE_OPERATOR, g["EndsAt"], sent_at=fmt_time(now))
        log.info("Sent gift expiry summary (%d gift(s)) to %d operator(s)", len(due), delivered)
        return len(due)

    async def remind_servers(self, active: list[dict[str, Any]], now: datetime) -> int:
        """Post heads-ups in servers whose gift or trial ends within 3 days;
        return how many."""
        sent = 0
        ending = gifts_ending_within(active, now, SERVER_WINDOW, GIFT_SOURCES | {TRIAL_SOURCE})
        enforced = bool(getattr(getattr(self.bot, "tiers", None), "enforced", False))
        for gift in await self._unsent(ending, STAGE_SERVER):
            if paid_outlasts(gift, active):
                continue
            after = status_after(gift, active)
            is_trial = gift["Source"] == TRIAL_SOURCE
            if is_trial and after.policy.rank >= TIERS[gift["Tier"]].rank:
                continue  # another plan keeps the server on the trial's tier
            channel = await self._report_channel(gift["GuildId"])
            if channel is None:
                continue
            text = (
                format_trial_heads_up(gift, after, enforced=enforced) if is_trial
                else format_server_heads_up(gift, after)
            )
            try:
                await channel.send(text)
            except discord.HTTPException:
                log.warning(
                    "Couldn't post %s expiry heads-up for #%s in guild %s",
                    gift["Source"], gift["Id"], gift["GuildId"], exc_info=True,
                )
                continue
            await self.db.mark_reminder_sent(gift["Id"], STAGE_SERVER, gift["EndsAt"], sent_at=fmt_time(now))
            log.info("Posted %s expiry heads-up for #%s in guild %s",
                     gift["Source"], gift["Id"], gift["GuildId"])
            sent += 1
        return sent

    async def _report_channel(self, guild_id: str):
        channel_id = await self.db.report_channel_id(guild_id)
        if channel_id is None:
            log.debug("Guild %s has no report channel; skipping gift heads-up", guild_id)
            return None
        guild = self.bot.get_guild(int(guild_id)) if guild_id.isdigit() else None
        channel = guild.get_channel(int(channel_id)) if guild and channel_id.isdigit() else None
        if channel is None:
            log.debug("Report channel %s of guild %s not found; skipping gift heads-up", channel_id, guild_id)
        return channel
