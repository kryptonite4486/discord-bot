"""Operator-only commands: reload, sync, backup, queue, purges, usage,
export, and gifting plans (grant, revoke, extend, show, list, and gift codes).

These act on the whole bot, not one server, so they are limited to the
users in BOT_OWNER_IDS. The /ops slash group is registered only in
CONTROL_GUILD_ID, so it never appears in customer servers, and every call
is checked again for both the server and the user.
"""

from __future__ import annotations

import importlib
import io
import logging
import re
import sys
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from bot.utils.backup import create_backup
from bot.utils.command_sync import sync_commands, sync_control_guild
from bot.utils.export import build_export, upload_limit
from bot.utils.fair_queue import LEVEL_NAMES, GuildQueueState
from bot.utils.gift_codes import code_hint, generate_code, hash_code, normalize_code
from bot.utils.report_channel import ReportChannelUnavailable, send_to_report_channel
from bot.utils.retention import RemovedServer, removed_servers
from bot.utils.tiers import TIERS, status_from_entitlements

log = logging.getLogger(__name__)

NOT_OPERATOR_MESSAGE = "Only the bot operator can use this command."
# Keeps /ops usage and /ops queue under Discord's 2,000-character limit.
MAX_USAGE_ROWS = 20

# Cog reload alone does not refresh already-imported helpers. Reload deps in
# dependency order (leaves first) so formatters pick up a fresh format_value.
_RELOAD_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "bot.cogs.reports": (
        "bot.db.database",
        "bot.utils.parsing",
        "bot.utils.guild",
        "bot.reporting.formatters",
        "bot.reporting.charts",
        "bot.reporting",
    ),
    "bot.cogs.ingest": (
        "bot.db.database",
        "bot.utils.parsing",
        "bot.ocr.vision",
        "bot.ocr.pipeline",
        "bot.ocr",
    ),
    "bot.cogs.help_cmd": (
        "bot.utils.parsing",
    ),
    "bot.cogs.admin": (
        "bot.db.database",
        "bot.utils.guild",
        "bot.utils.backup",
        "bot.utils.names",
        "bot.utils.parsing",
    ),
    "bot.cogs.data": (
        "bot.db.database",
        "bot.utils.backup",
        "bot.utils.export",
        "bot.utils.guild",
        "bot.utils.retention",
    ),
    "bot.cogs.ops": (
        "bot.utils.gift_codes",
        "bot.utils.backup",
        "bot.utils.command_sync",
        "bot.utils.export",
        "bot.utils.retention",
        "bot.utils.report_channel",
    ),
    "bot.cogs.premium": (
        "bot.utils.gift_codes",
        "bot.utils.tiers",
    ),
}


def _rebind_database(bot: commands.Bot) -> None:
    """Point the live Database instance at the reloaded class.

    importlib.reload updates the module, but bot.db remains an instance of the
    previous class — method lookup would keep serving stale SQL otherwise.
    """
    mod = sys.modules.get("bot.db.database")
    db = getattr(bot, "db", None)
    if mod is None or db is None or not hasattr(mod, "Database"):
        return
    db.__class__ = mod.Database
    pkg = sys.modules.get("bot.db")
    if pkg is not None:
        pkg.Database = mod.Database


def _reload_dependencies(extension: str, bot: commands.Bot | None = None) -> list[str]:
    refreshed: list[str] = []
    for name in _RELOAD_DEPENDENCIES.get(extension, ()):
        mod = sys.modules.get(name)
        if mod is None:
            continue
        importlib.reload(mod)
        refreshed.append(name)
        if name == "bot.db.database" and bot is not None:
            _rebind_database(bot)
    return refreshed


async def reload_cog(bot: commands.Bot, cog: str) -> str:
    module = cog if cog.startswith("bot.cogs.") else f"bot.cogs.{cog}"
    deps = _reload_dependencies(module, bot)
    try:
        await bot.reload_extension(module)
    except commands.ExtensionNotLoaded:
        await bot.load_extension(module)
    extra = f" (also reloaded: {', '.join(deps)})" if deps else ""
    return f"Reloaded `{module}`{extra}."


async def run_command_sync(bot: commands.Bot) -> str:
    """Register commands and clean duplicate copies in every server."""
    s = bot.settings
    result = await sync_commands(
        bot.tree,
        list(bot.guilds),
        dev_guild_id=s.dev_guild_id,
        control_guild_id=s.control_guild_id,
    )
    return result.summary()


def format_usage_report(
    days: int,
    by_guild: dict[str, dict[str, float]],
    images_by_day: list[tuple[str, float]],
    names: dict[str, str],
) -> str:
    """Markdown summary of OCR usage per server over the last ``days`` days."""
    if not by_guild:
        return f"No OCR usage recorded in the last {days} day(s)."

    def row(label: str, u: dict[str, float]) -> str:
        images = int(u.get("ocr_images", 0))
        batches = int(u.get("ocr_batches", 0))
        secs = u.get("ocr_seconds", 0.0)
        per_image = f"{secs / images:.1f}s" if images else "-"
        avg_wait = f"{u.get('ocr_wait_seconds', 0.0) / batches:.0f}s" if batches else "-"
        return (
            f"{label[:22]:<22} {batches:>5} {images:>6} "
            f"{int(u.get('ocr_failed', 0)):>5} {secs / 60:>7.1f} {per_image:>6} {avg_wait:>6}"
        )

    ordered = sorted(
        by_guild.items(), key=lambda kv: kv[1].get("ocr_images", 0), reverse=True
    )
    totals: dict[str, float] = {}
    for _, u in ordered:
        for k, v in u.items():
            totals[k] = totals.get(k, 0.0) + v
    lines = [
        f"**OCR usage, last {days} day(s)** (UTC days)",
        "```",
        f"{'Server':<22} {'Batch':>5} {'Images':>6} {'Fail':>5} {'OCR min':>7} "
        f"{'s/img':>6} {'Wait':>6}",
        *(row(names.get(gid, gid), u) for gid, u in ordered[:MAX_USAGE_ROWS]),
        *(
            [f"…and {len(ordered) - MAX_USAGE_ROWS} more server(s)"]
            if len(ordered) > MAX_USAGE_ROWS
            else []
        ),
        "-" * 63,
        row("Total", totals),
        "```",
    ]
    if images_by_day:
        peak_day, peak = max(images_by_day, key=lambda d: d[1])
        lines.append(f"Busiest day: **{peak_day}** with **{int(peak)}** images.")
        if len(images_by_day) <= 14:
            lines.append(
                "Images per day: "
                + ", ".join(f"{d[5:]} {int(n)}" for d, n in images_by_day)
            )
    lines.append("_Wait = average time a batch queued behind other servers' batches._")
    return "\n".join(lines)


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def format_queue_report(
    states: list[GuildQueueState], slots: int, names: dict[str, str]
) -> str:
    """Markdown view of the live OCR queue, per server."""
    running = sum(s.running for s in states)
    waiting = sum(s.waiting for s in states)
    head = (
        f"**OCR queue:** {running}/{slots} slot(s) busy, "
        f"{waiting} request(s) waiting across {len(states)} server(s)."
    )
    if not states:
        return head
    lines = [
        head,
        "```",
        f"{'Server':<22} {'Run':>3} {'Wait':>4} {'Running for':>11} {'Oldest wait':>11} Lane",
    ]
    for s in states[:MAX_USAGE_ROWS]:
        lines.append(
            f"{names.get(s.guild_id, s.guild_id)[:22]:<22} {s.running:>3} {s.waiting:>4} "
            f"{_duration(s.longest_run_seconds) if s.running else '-':>11} "
            f"{_duration(s.longest_wait_seconds) if s.waiting else '-':>11} "
            f"{LEVEL_NAMES[s.priority] if s.waiting else '-'}"
        )
    if len(states) > MAX_USAGE_ROWS:
        lines.append(f"…and {len(states) - MAX_USAGE_ROWS} more server(s)")
    lines.append("```")
    return "\n".join(lines)


def format_purges_report(servers: list[RemovedServer], retention_days: int) -> str:
    """Servers that removed the bot and when their data will be deleted."""
    head = (
        f"**Removed servers** (data is kept {retention_days} day(s) after the bot is "
        "removed or the subscription ends, whichever is later; adding the bot back "
        "cancels the deletion)"
    )
    if not servers:
        return head + "\nNone."
    # Soonest deletion first; servers kept by a subscription last.
    ordered = sorted(
        servers, key=lambda r: (r.deletes_at is None, r.deletes_at or r.removed_at)
    )
    lines = [head, "```", f"{'Server ID':<20} {'Removed (UTC)':<16} {'Deletes (UTC)':<16}"]
    for r in ordered[:MAX_USAGE_ROWS]:
        deletes = (
            r.deletes_at.strftime("%Y-%m-%d %H:%M") if r.deletes_at else "kept: subscribed"
        )
        lines.append(f"{r.guild_id:<20} {r.removed_at:%Y-%m-%d %H:%M} {deletes:<16}")
    if len(ordered) > MAX_USAGE_ROWS:
        lines.append(f"…and {len(ordered) - MAX_USAGE_ROWS} more")
    lines.append("```")
    return "\n".join(lines)


EXPORT_FORMAT_CHOICES = [
    app_commands.Choice(name="CSV", value="csv"),
    app_commands.Choice(name="JSON", value="json"),
]
GIFT_TIER_CHOICES = [
    app_commands.Choice(name="Alliance", value="mid"),
    app_commands.Choice(name="Command", value="full"),
]
DURATION_CHOICES = [
    app_commands.Choice(name="30 days", value="30"),
    app_commands.Choice(name="90 days", value="90"),
    app_commands.Choice(name="1 year", value="365"),
    app_commands.Choice(name="Permanent", value="permanent"),
]
# Sources an operator may revoke. Paid subscriptions end through billing.
REVOCABLE_SOURCES = {"gift", "trial", "code"}
_TS = "%Y-%m-%d %H:%M:%S"


def gift_end(duration: str, start: datetime) -> str | None:
    """EndsAt for a duration choice; None for permanent."""
    if duration == "permanent":
        return None
    return (start + timedelta(days=int(duration))).strftime(_TS)


def extended_end(current: str, duration: str, now: datetime) -> str | None:
    """Push an end date out by ``duration``, from now if it already passed."""
    if duration == "permanent":
        return None
    base = max(datetime.strptime(current, _TS).replace(tzinfo=timezone.utc), now)
    return (base + timedelta(days=int(duration))).strftime(_TS)


def gift_thank_you_embed(tier_name: str, ends: str | None) -> discord.Embed:
    """The notice /ops grant notify:true posts in the gifted server.

    The operator's reason stays private (it's only in the audit log).
    """
    until = "permanently" if ends is None else f"until **{ends[:10]}**"
    return discord.Embed(
        title="🎁 A gift for this server",
        description=(
            f"This server has been gifted the **{tier_name}** plan of "
            f"LastZ Assistant {until}. Thank you for being part of it!\n\n"
            "Run `/premium` to see what's included."
        ),
        colour=discord.Colour.gold(),
    )


def _entitlement_line(row: dict, now: str) -> str:
    tier = TIERS[row["Tier"]].name if row["Tier"] in TIERS else row["Tier"]
    if row["RevokedAt"]:
        state = f"revoked {row['RevokedAt'][:10]}"
    elif row["StartsAt"] > now:
        state = f"starts {row['StartsAt'][:10]}"
    elif row["EndsAt"] and row["EndsAt"] <= now:
        state = f"ended {row['EndsAt'][:10]}"
    else:
        state = f"active, until {row['EndsAt'][:10]}" if row["EndsAt"] else "active, no end"
    reason = f" — {row['Reason']}" if row.get("Reason") else ""
    return f"#{row['Id']} {tier} ({row['Source']}): {state}{reason}"


def format_show(
    guild_id: str,
    name: str | None,
    rows: list[dict],
    audit: list[dict],
    used: int,
    now: str,
) -> str:
    active = [r for r in rows if not r["RevokedAt"] and r["StartsAt"] <= now
              and (r["EndsAt"] is None or r["EndsAt"] > now)]
    status = status_from_entitlements(active)
    lines = [
        f"**{name or 'Unknown server (bot not in it)'}** `{guild_id}`",
        f"Plan: **{status.describe()}** · screenshots this week: "
        f"{used}/{status.policy.ocr_images_per_week}",
    ]
    lines.append("**Entitlements**" if rows else "No entitlements.")
    lines += [f"• {_entitlement_line(r, now)}" for r in rows[:15]]
    if audit:
        lines.append("**Recent changes**")
        lines += [
            f"• {a['At'][:16]} {a['Action']} by `{a['ActorId'] or '?'}`: {a['Detail'] or ''}"
            for a in audit[:5]
        ]
    return "\n".join(lines)[:1900]


def format_entitlement_list(rows: list[dict], names: dict[str, str], within_days: int | None) -> str:
    title = (
        f"**Active entitlements ending within {within_days} day(s)**"
        if within_days is not None
        else "**Active entitlements**"
    )
    if not rows:
        return title + "\nNone."
    lines = [title]
    for r in rows[:MAX_USAGE_ROWS]:
        tier = TIERS[r["Tier"]].name if r["Tier"] in TIERS else r["Tier"]
        until = r["EndsAt"][:10] if r["EndsAt"] else "no end"
        lines.append(
            f"• #{r['Id']} {names.get(r['GuildId'], r['GuildId'])}: {tier} "
            f"({r['Source']}) until {until}"
        )
    if len(rows) > MAX_USAGE_ROWS:
        lines.append(f"…and {len(rows) - MAX_USAGE_ROWS} more")
    return "\n".join(lines)


def _code_state(row: dict, now: str) -> str:
    if row["RevokedAt"]:
        return f"revoked {row['RevokedAt'][:10]}"
    if row["Uses"] >= row["MaxUses"]:
        return "used up"
    if row["ExpiresAt"] and row["ExpiresAt"] <= now:
        return f"expired {row['ExpiresAt'][:10]}"
    return f"redeemable until {row['ExpiresAt'][:10]}" if row["ExpiresAt"] else "redeemable"


def format_code_list(rows: list[dict], now: str, *, include_inactive: bool) -> str:
    title = "**Gift codes**" if include_inactive else "**Redeemable gift codes**"
    if not rows:
        return title + "\nNone."
    lines = [title]
    for r in rows[:MAX_USAGE_ROWS]:
        tier = TIERS[r["Tier"]].name if r["Tier"] in TIERS else r["Tier"]
        lasts = f"{r['DurationDays']} days" if r["DurationDays"] is not None else "permanent"
        note = f" — {r['Note']}" if r.get("Note") else ""
        lines.append(
            f"• #{r['Id']} `…{r['Hint']}` {tier}, {lasts}: {r['Uses']}/{r['MaxUses']} used, "
            f"{_code_state(r, now)}{note}"
        )
    if len(rows) > MAX_USAGE_ROWS:
        lines.append(f"…and {len(rows) - MAX_USAGE_ROWS} more")
    return "\n".join(lines)[:1900]


def is_operator(bot: commands.Bot, user_id: int) -> bool:
    return user_id in bot.settings.bot_owner_ids



_GUILD_ID_IN_LABEL = re.compile(r"\((\d{15,22})\)\s*$")


def parse_guild_id(text: str) -> str:
    """The server ID in a guild_id option.

    Normally the autocomplete sends just the ID, but if the field is edited
    after picking, Discord sends the visible label "Name (123…)" instead.
    """
    text = text.strip()
    match = _GUILD_ID_IN_LABEL.search(text)
    return match.group(1) if match else text

class Ops(commands.Cog):
    """Bot operator commands (owner-only, control server only)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = interaction.guild_id == self.bot.settings.control_guild_id and (
            is_operator(self.bot, interaction.user.id)
        )
        if not allowed:
            log.warning(
                "Refused /ops from user %s in guild %s",
                interaction.user.id,
                interaction.guild_id,
            )
            await interaction.response.send_message(NOT_OPERATOR_MESSAGE, ephemeral=True)
        return allowed

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            return  # interaction_check already replied
        log.exception("Ops command error: %s", error)
        text = f"Error: `{error}`"
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        if guild.id != self.bot.settings.control_guild_id:
            return
        try:
            count = await sync_control_guild(self.bot.tree, guild.id)
            log.info("Joined control guild %s; registered %d commands", guild.id, count)
        except discord.HTTPException:
            log.exception("Failed to register /ops in control guild %s", guild.id)

    ops = app_commands.Group(name="ops", description="Bot operator commands")

    @ops.command(name="reload", description="Reload a cog module")
    @app_commands.describe(cog="Cog module name, e.g. ingest or reports")
    async def reload(self, interaction: discord.Interaction, cog: str) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            msg = await reload_cog(self.bot, cog)
        except Exception as exc:
            log.exception("Failed to reload %s", cog)
            await interaction.followup.send(f"Reload failed: `{exc}`", ephemeral=True)
            return
        log.info("%s (by %s)", msg, interaction.user)
        await interaction.followup.send(msg, ephemeral=True)

    @ops.command(
        name="sync",
        description="Register commands globally and remove duplicate copies in every server",
    )
    async def sync(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            msg = await run_command_sync(self.bot)
        except Exception as exc:
            log.exception("Command sync failed")
            await interaction.followup.send(f"Sync failed: `{exc}`", ephemeral=True)
            return
        log.info("Synced commands by %s", interaction.user)
        await interaction.followup.send(msg, ephemeral=True)

    @ops.command(
        name="backup",
        description="Write a database backup to the host backup folder now",
    )
    async def backup(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not getattr(self.bot, "backups_enabled", False):
            await interaction.followup.send(
                "Backups are disabled: no backup folder is mounted "
                "(set `BOT_BACKUP_DIR` in .env). Check the bot logs.",
                ephemeral=True,
            )
            return
        settings = self.bot.settings
        try:
            path = await create_backup(
                self.bot.db, settings.backup_dir, "manual", keep=settings.backup_keep
            )
        except Exception as exc:
            log.exception("Manual database backup failed")
            await interaction.followup.send(f"Backup failed: `{exc}`", ephemeral=True)
            return
        log.info("Manual backup %s by %s", path.name, interaction.user)
        await interaction.followup.send(
            f"Backup saved on the host as `{path.name}` "
            f"({path.stat().st_size / 1024:,.0f} KB).",
            ephemeral=True,
        )

    @ops.command(name="queue", description="Live OCR queue: running and waiting requests per server")
    async def queue(self, interaction: discord.Interaction) -> None:
        ingest = self.bot.get_cog("Ingest")
        ocr_queue = getattr(ingest, "ocr_queue", None)
        if ocr_queue is None:
            await interaction.response.send_message(
                "The ingest cog isn't loaded, so there is no OCR queue.", ephemeral=True
            )
            return
        names = {str(g.id): g.name for g in self.bot.guilds}
        await interaction.response.send_message(
            format_queue_report(ocr_queue.snapshot(), ocr_queue.slots, names),
            ephemeral=True,
        )

    # --- Gifting (entitlements) ---------------------------------------------

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _guild_name(self, guild_id: str) -> str | None:
        guild = self.bot.get_guild(int(guild_id)) if guild_id.isdigit() else None
        return guild.name if guild else None

    async def _guild_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        needle = current.casefold()
        return [
            app_commands.Choice(name=f"{g.name} ({g.id})"[:100], value=str(g.id))
            for g in self.bot.guilds
            if needle in g.name.casefold() or needle in str(g.id)
        ][:25]

    @ops.command(name="grant", description="Gift a plan to a server")
    @app_commands.describe(
        guild_id="Server to gift (pick from the list, or paste an ID)",
        tier="Plan to gift",
        duration="How long the gift lasts",
        reason="Why (kept in the audit log)",
        notify="Post a thank-you in the server's report channel (see /setup)",
    )
    @app_commands.choices(tier=GIFT_TIER_CHOICES, duration=DURATION_CHOICES)
    @app_commands.autocomplete(guild_id=_guild_autocomplete)
    async def grant(
        self,
        interaction: discord.Interaction,
        guild_id: str,
        tier: app_commands.Choice[str],
        duration: app_commands.Choice[str],
        reason: str,
        notify: bool = False,
    ) -> None:
        guild_id = parse_guild_id(guild_id)
        if not guild_id.isdigit():
            await interaction.response.send_message("That isn't a server ID.", ephemeral=True)
            return
        # Posting the notice is a second Discord call, so don't risk the
        # 3-second reply window.
        if notify:
            await interaction.response.defer(ephemeral=True)
        now = self._now()
        ends = gift_end(duration.value, now)
        new_id = await self.bot.db.add_entitlement(
            guild_id, tier.value, "gift",
            starts_at=now.strftime(_TS), ends_at=ends,
            granted_by=str(interaction.user.id), reason=reason,
        )
        self.bot.tiers.invalidate(guild_id)
        name = self._guild_name(guild_id)
        warn = "" if name else "\n⚠️ The bot isn't in that server; the gift applies if it's added."
        log.warning("Gift #%d: %s until %s for guild %s by %s (%s)",
                    new_id, tier.name, ends or "no end", guild_id, interaction.user, reason)
        text = (
            f"Gifted **{tier.name}** to **{name or guild_id}** "
            f"{'permanently' if ends is None else 'until ' + ends[:10]} (#{new_id}).{warn}"
        )
        if not notify:
            await interaction.response.send_message(text, ephemeral=True)
            return
        try:
            message = await send_to_report_channel(
                self.bot, guild_id, embed=gift_thank_you_embed(tier.name, ends)
            )
        except ReportChannelUnavailable as exc:
            log.warning("Gift #%d: no thank-you posted in guild %s: %s", new_id, guild_id, exc)
            text += f"\n⚠️ Thank-you **not** posted: {exc}. The gift itself is in place."
        else:
            text += f"\nThank-you posted in {message.channel.mention}."
        await interaction.followup.send(text, ephemeral=True)

    @ops.command(name="revoke", description="Revoke a server's gifts, or one entitlement")
    @app_commands.describe(
        guild_id="Server whose gifts to revoke",
        entitlement_id="Only this entitlement (see /ops show); default: all active gifts",
        reason="Why (kept in the audit log)",
    )
    @app_commands.autocomplete(guild_id=_guild_autocomplete)
    async def revoke(
        self,
        interaction: discord.Interaction,
        guild_id: str,
        reason: str,
        entitlement_id: int | None = None,
    ) -> None:
        guild_id = parse_guild_id(guild_id)
        now = self._now().strftime(_TS)
        if entitlement_id is not None:
            row = await self.bot.db.get_entitlement(entitlement_id)
            if row is None or row["GuildId"] != guild_id:
                await interaction.response.send_message(
                    f"No entitlement #{entitlement_id} for that server.", ephemeral=True
                )
                return
            targets = [row]
        else:
            targets = [
                r for r in await self.bot.db.active_entitlements(guild_id, now)
                if r["Source"] in REVOCABLE_SOURCES
            ]
        paid = [r for r in targets if r["Source"] not in REVOCABLE_SOURCES]
        if paid:
            await interaction.response.send_message(
                f"#{paid[0]['Id']} is a paid subscription; it ends through billing, "
                "not /ops revoke.", ephemeral=True,
            )
            return
        done = [
            r["Id"] for r in targets
            if await self.bot.db.revoke_entitlement(
                r["Id"], at=now, actor_id=str(interaction.user.id), reason=reason
            )
        ]
        self.bot.tiers.invalidate(guild_id)
        if done:
            log.warning("Revoked %s for guild %s by %s (%s)", done, guild_id, interaction.user, reason)
        status = await self.bot.tiers.status(guild_id)
        await interaction.response.send_message(
            (f"Revoked {', '.join(f'#{i}' for i in done)}." if done else "Nothing active to revoke.")
            + f" The server is now on **{status.describe()}**.",
            ephemeral=True,
        )

    @ops.command(name="extend", description="Extend an entitlement, or make it permanent")
    @app_commands.describe(entitlement_id="Entitlement to extend (see /ops show)", duration="How much longer")
    @app_commands.choices(duration=DURATION_CHOICES)
    async def extend(
        self,
        interaction: discord.Interaction,
        entitlement_id: int,
        duration: app_commands.Choice[str],
    ) -> None:
        row = await self.bot.db.get_entitlement(entitlement_id)
        if row is None:
            await interaction.response.send_message(f"No entitlement #{entitlement_id}.", ephemeral=True)
            return
        if row["Source"] not in REVOCABLE_SOURCES:
            await interaction.response.send_message(
                f"#{entitlement_id} is a paid subscription; its dates come from billing.",
                ephemeral=True,
            )
            return
        if row["EndsAt"] is None:
            await interaction.response.send_message(
                f"#{entitlement_id} is already permanent.", ephemeral=True
            )
            return
        ends = extended_end(row["EndsAt"], duration.value, self._now())
        await self.bot.db.set_entitlement_end(entitlement_id, ends, actor_id=str(interaction.user.id))
        self.bot.tiers.invalidate(row["GuildId"])
        await interaction.response.send_message(
            f"#{entitlement_id} now {'has no end date' if ends is None else 'ends ' + ends[:10]}.",
            ephemeral=True,
        )

    @ops.command(name="show", description="A server's plan, entitlements and recent changes")
    @app_commands.autocomplete(guild_id=_guild_autocomplete)
    async def show(self, interaction: discord.Interaction, guild_id: str) -> None:
        guild_id = parse_guild_id(guild_id)
        rows = await self.bot.db.entitlements_for(guild_id)
        audit = await self.bot.db.entitlement_audit(guild_id)
        used = await self.bot.tiers.ocr_used_this_week(guild_id)
        await interaction.response.send_message(
            format_show(guild_id, self._guild_name(guild_id), rows, audit, used,
                        self._now().strftime(_TS)),
            ephemeral=True,
        )

    @ops.command(name="list", description="Active entitlements across servers")
    @app_commands.describe(expiring_within="Only those ending within this many days")
    async def list_entitlements(
        self,
        interaction: discord.Interaction,
        expiring_within: app_commands.Range[int, 1, 365] | None = None,
    ) -> None:
        now = self._now()
        rows = await self.bot.db.all_active_entitlements(now.strftime(_TS))
        if expiring_within is not None:
            cutoff = (now + timedelta(days=expiring_within)).strftime(_TS)
            rows = [r for r in rows if r["EndsAt"] and r["EndsAt"] <= cutoff]
        names = {str(g.id): g.name for g in self.bot.guilds}
        await interaction.response.send_message(
            format_entitlement_list(rows, names, expiring_within), ephemeral=True
        )

    # --- Gift codes -----------------------------------------------------------
    # Covered by interaction_check above like every /ops command: discord.py
    # runs the cog's check for commands in nested groups too.

    code = app_commands.Group(name="code", description="Redeemable gift codes", parent=ops)

    @code.command(name="create", description="Create a code that servers redeem with /redeem")
    @app_commands.describe(
        tier="Plan the code gives",
        duration="How long the plan lasts once redeemed",
        uses="How many servers can redeem it (one use per server)",
        expires="Days until the code can no longer be redeemed (default: never)",
        note="What it's for, e.g. a giveaway (kept in the audit log)",
    )
    @app_commands.choices(tier=GIFT_TIER_CHOICES, duration=DURATION_CHOICES)
    async def code_create(
        self,
        interaction: discord.Interaction,
        tier: app_commands.Choice[str],
        duration: app_commands.Choice[str],
        uses: app_commands.Range[int, 1, 1000],
        expires: app_commands.Range[int, 1, 365] | None = None,
        note: str | None = None,
    ) -> None:
        now = self._now()
        text = generate_code()
        normalized = normalize_code(text)
        expires_at = (now + timedelta(days=expires)).strftime(_TS) if expires else None
        code_id = await self.bot.db.create_gift_code(
            hash_code(normalized), code_hint(normalized), tier.value,
            duration_days=None if duration.value == "permanent" else int(duration.value),
            max_uses=uses, expires_at=expires_at,
            created_by=str(interaction.user.id), note=note,
        )
        log.warning("Gift code #%d: %s %s, %d use(s), until %s, by %s (%s)",
                    code_id, tier.name, duration.name, uses, expires_at or "no end",
                    interaction.user, note)
        await interaction.response.send_message(
            f"Code #{code_id}: **{tier.name}** for **{duration.name.lower()}**, "
            f"{uses} server(s), redeemable "
            f"{'until ' + expires_at[:10] if expires_at else 'until used up or revoked'}.\n"
            f"# `{text}`\n"
            "Copy it now: only a hash is stored, so it can't be shown again. "
            "A server admin redeems it with `/redeem code:<code>`.",
            ephemeral=True,
        )

    @code.command(name="list", description="Gift codes and how many times each was used")
    @app_commands.describe(include_inactive="Also show used-up, expired and revoked codes")
    async def code_list(self, interaction: discord.Interaction, include_inactive: bool = False) -> None:
        now = self._now().strftime(_TS)
        rows = await self.bot.db.gift_codes(now, include_inactive=include_inactive)
        await interaction.response.send_message(
            format_code_list(rows, now, include_inactive=include_inactive), ephemeral=True
        )

    @code.command(name="revoke", description="Stop a gift code from being redeemed")
    @app_commands.describe(
        code_id="Code number (see /ops code list)",
        reason="Why (kept in the audit log)",
        revoke_redeemed="Also revoke the plans servers already got from it",
    )
    async def code_revoke(
        self,
        interaction: discord.Interaction,
        code_id: int,
        reason: str,
        revoke_redeemed: bool = False,
    ) -> None:
        result = await self.bot.db.revoke_gift_code(
            code_id, at=self._now().strftime(_TS), actor_id=str(interaction.user.id),
            reason=reason, revoke_redeemed=revoke_redeemed,
        )
        if result is None:
            await interaction.response.send_message(f"No code #{code_id}.", ephemeral=True)
            return
        newly, entitlements = result
        for _, guild_id in entitlements:
            self.bot.tiers.invalidate(guild_id)
        log.warning("Code #%d revoked by %s (%s); entitlements revoked: %s",
                    code_id, interaction.user, reason, [e for e, _ in entitlements])
        parts = [f"Code #{code_id} revoked." if newly else f"Code #{code_id} was already revoked."]
        if revoke_redeemed:
            parts.append(
                f"Also revoked {', '.join(f'#{e}' for e, _ in entitlements)}."
                if entitlements else "No active plans from it to revoke."
            )
        await interaction.response.send_message(" ".join(parts), ephemeral=True)

    @ops.command(
        name="export",
        description="Export every week of a server's metrics, for deletion or access requests",
    )
    @app_commands.describe(
        guild_id="Server to export (pick from the list, or paste an ID)",
        format="CSV (default) or JSON",
    )
    @app_commands.choices(format=EXPORT_FORMAT_CHOICES)
    @app_commands.autocomplete(guild_id=_guild_autocomplete)
    async def export(
        self,
        interaction: discord.Interaction,
        guild_id: str,
        format: app_commands.Choice[str] | None = None,
    ) -> None:
        # Hands over another server's data, so check the operator again here
        # rather than relying only on interaction_check.
        if not is_operator(self.bot, interaction.user.id):
            log.warning("Refused /ops export from user %s", interaction.user.id)
            await interaction.response.send_message(NOT_OPERATOR_MESSAGE, ephemeral=True)
            return
        guild_id = parse_guild_id(guild_id)
        if not guild_id.isdigit():
            await interaction.response.send_message("That isn't a server ID.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        fmt = format.value if format else "csv"
        # Every week and channel, whatever the server's plan: the operator
        # needs the full data for deletion and access requests.
        target = self.bot.get_guild(int(guild_id))
        result = await build_export(
            self.bot.db, guild_id, target, fmt=fmt, limit=upload_limit(interaction.guild)
        )
        label = f"**{target.name if target else 'Unknown server (bot not in it)'}** `{guild_id}`"
        if result is None:
            await interaction.followup.send(
                f"The export for {label} is too big to upload here even zipped. "
                "Use `/ops backup` and query the backup on the host.",
                ephemeral=True,
            )
            return
        log.warning("/ops export by %s: %d row(s) for guild %s as %s",
                    interaction.user, result.rows, guild_id, result.filename)
        if not result.rows:
            await interaction.followup.send(f"Nothing is stored for {label}.", ephemeral=True)
            return
        zipped = " (zipped)" if result.zipped else ""
        await interaction.followup.send(
            f"**{result.rows}** row(s) for {label}, every week and channel{zipped}.",
            file=discord.File(io.BytesIO(result.data), filename=result.filename),
            ephemeral=True,
        )

    @ops.command(name="purges", description="Servers that removed the bot and when their data will be deleted")
    async def purges(self, interaction: discord.Interaction) -> None:
        days = self.bot.settings.data_retention_days
        servers = await removed_servers(
            self.bot.db, timedelta(days=days), datetime.now(timezone.utc)
        )
        await interaction.response.send_message(
            format_purges_report(servers, days), ephemeral=True
        )

    @ops.command(name="usage", description="OCR usage per server (images, OCR time, queue wait)")
    @app_commands.describe(days="How many days back, including today (default 7)")
    async def usage(
        self,
        interaction: discord.Interaction,
        days: app_commands.Range[int, 1, 90] = 7,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        since = (datetime.now(timezone.utc).date() - timedelta(days=days - 1)).isoformat()
        by_guild = await self.bot.db.usage_by_guild(since)
        by_day = await self.bot.db.usage_by_day(since, "ocr_images")
        names = {str(g.id): g.name for g in self.bot.guilds}
        await interaction.followup.send(
            format_usage_report(days, by_guild, by_day, names), ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    s = bot.settings
    if not s.bot_owner_ids or not s.control_guild_id:
        log.warning(
            "BOT_OWNER_IDS or CONTROL_GUILD_ID is not set; operator commands "
            "(/ops reload, sync, backup, queue, purges, usage, export) are disabled"
        )
        return
    # guild= registers every slash command in this cog to the control server
    # only; nothing here is added globally. override: on reload, discord.py
    # doesn't remove guild-scoped cog commands, so replace them in place.
    await bot.add_cog(
        Ops(bot), guild=discord.Object(id=s.control_guild_id), override=True
    )
