"""Data ingestion: manual add, CSV paste, and OCR image upload."""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from bot.config import DATASET_KINDS, resolve_metric
from bot.ocr import VisionOCR, extract_metrics_from_image
from bot.utils.archive import (
    MAX_ZIP_IMAGES,
    ArchiveError,
    ImageSource,
    extract_images_from_zip,
    is_image_name,
    is_zip_upload,
)
from bot.utils.guild import (
    channel_id_from_context,
    channel_id_from_interaction,
    channel_id_from_message,
    guild_id_from_context,
    guild_id_from_interaction,
)
from bot.utils.plausibility import check_batch
from bot.utils.parsing import (
    format_value,
    parse_numeric_value,
    parse_pasted_rows,
    parse_week_start,
)

log = logging.getLogger(__name__)

# Soft cap for OCR images in one batch session.
MAX_INGEST_IMAGES = 20
# Discord's hard limit on attachments per message / slash interaction.
MAX_ATTACHMENTS_PER_MESSAGE = 10
# Cap for one run once a .zip is involved (loose images still cap at 20).
MAX_BATCH_IMAGES = MAX_ZIP_IMAGES
# Refuse to download zips larger than this (Discord's own upload cap is lower
# on most servers; this is a sanity bound).
MAX_ZIP_UPLOAD_BYTES = 50 * 1024 * 1024

ProgressCallback = Callable[[int, int, str], Awaitable[None]]

DATASET_CHOICES = [
    app_commands.Choice(name="Versus", value="versus"),
    app_commands.Choice(name="Tech", value="tech"),
    app_commands.Choice(name="General (HQ+Power)", value="general"),
    app_commands.Choice(name="Power leaderboard", value="power"),
    app_commands.Choice(name="Arena Power (member cards)", value="arena"),
    app_commands.Choice(name="Kills leaderboard", value="kills"),
]
DATASET_USAGE = ", ".join(DATASET_KINDS)


def _is_image_attachment(attachment: discord.Attachment) -> bool:
    if attachment.content_type and attachment.content_type.startswith("image/"):
        return True
    return is_image_name(attachment.filename)


def _is_zip_attachment(attachment: discord.Attachment) -> bool:
    return is_zip_upload(attachment.filename, attachment.content_type)


class Ingest(commands.Cog):
    """Commands for adding weekly player metrics."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        s = bot.settings
        self._ocr = VisionOCR(
            base_url=s.ocr_vision_base_url,
            model=s.ocr_vision_model,
            api_key=s.ocr_vision_api_key,
            timeout=s.ocr_vision_timeout,
        )

    def _default_week(self, week: str | None, *, context: str = "ingest") -> str:
        if week:
            source = "user"
            raw = week
        elif self.bot.settings.default_week_start:
            source = "DEFAULT_WEEK_START"
            raw = self.bot.settings.default_week_start
        else:
            source = "current"
            raw = None
        week_start = parse_week_start(raw)
        log.info(
            "Resolved week_start=%s (source=%s raw=%r context=%s)",
            week_start,
            source,
            raw,
            context,
        )
        return week_start

    @staticmethod
    def _filter_images(
        attachments: list[discord.Attachment] | tuple[discord.Attachment | None, ...],
        *,
        limit: int = MAX_INGEST_IMAGES,
    ) -> list[discord.Attachment]:
        images = [a for a in attachments if a is not None and _is_image_attachment(a)]
        seen: set[int] = set()
        unique: list[discord.Attachment] = []
        for image in images:
            if image.id in seen:
                continue
            seen.add(image.id)
            unique.append(image)
        return unique[:limit]

    @staticmethod
    def _filter_uploads(
        attachments: Sequence[discord.Attachment | None],
        *,
        limit: int = MAX_ATTACHMENTS_PER_MESSAGE,
    ) -> list[discord.Attachment]:
        """Images and .zip archives, de-duplicated, in upload order."""
        seen: set[int] = set()
        out: list[discord.Attachment] = []
        for a in attachments:
            if a is None or a.id in seen:
                continue
            if not (_is_image_attachment(a) or _is_zip_attachment(a)):
                continue
            seen.add(a.id)
            out.append(a)
        return out[:limit]

    async def _load_sources(
        self, attachments: Sequence[discord.Attachment]
    ) -> tuple[list[ImageSource], list[str]]:
        """Download attachments, expanding .zip files into their images."""
        sources: list[ImageSource] = []
        notes: list[str] = []
        for a in attachments:
            if _is_zip_attachment(a):
                if a.size > MAX_ZIP_UPLOAD_BYTES:
                    notes.append(
                        f"Skipped `{a.filename}` "
                        f"({a.size // (1024 * 1024)} MB is over the "
                        f"{MAX_ZIP_UPLOAD_BYTES // (1024 * 1024)} MB zip limit)."
                    )
                    continue
                try:
                    blob = await a.read()
                    images, warnings = await asyncio.to_thread(
                        extract_images_from_zip,
                        blob,
                        archive_name=a.filename,
                        key_prefix=f"zip:{a.id}",
                    )
                except ArchiveError as exc:
                    notes.append(f"Skipped {exc}.")
                    continue
                except discord.HTTPException as exc:
                    notes.append(f"Could not download `{a.filename}`: {exc}")
                    continue
                notes.extend(warnings)
                if not images:
                    notes.append(f"No images found in `{a.filename}`.")
                log.info(
                    "Expanded zip %s -> %d image(s)", a.filename, len(images)
                )
                sources.extend(images)
            elif _is_image_attachment(a):
                try:
                    blob = await a.read()
                except discord.HTTPException as exc:
                    notes.append(f"Could not download `{a.filename}`: {exc}")
                    continue
                sources.append(
                    ImageSource(key=f"att:{a.id}", filename=a.filename, data=blob)
                )
        return sources, notes

    @staticmethod
    def _merge_sources(
        collected: list[ImageSource],
        new: list[ImageSource],
        *,
        limit: int,
        loose_limit: int = MAX_INGEST_IMAGES,
    ) -> list[ImageSource]:
        """Append unseen sources; loose images stop at ``loose_limit``."""
        seen = {s.key for s in collected}
        out = list(collected)
        loose = sum(1 for s in out if not s.from_archive)
        for source in new:
            if len(out) >= limit:
                break
            if source.key in seen:
                continue
            if not source.from_archive:
                if loose >= loose_limit:
                    continue
                loose += 1
            seen.add(source.key)
            out.append(source)
        return out

    @staticmethod
    def _cap_sources(
        sources: list[ImageSource],
    ) -> list[ImageSource]:
        """Apply the per-run caps to a single upload's sources."""
        return Ingest._merge_sources([], sources, limit=MAX_BATCH_IMAGES)

    @staticmethod
    def _progress_editor(
        message: discord.Message, label: str, *, min_interval: float = 5.0
    ) -> ProgressCallback:
        """Throttled callback that edits ``message`` with OCR progress."""
        last = 0.0

        async def update(index: int, total: int, filename: str) -> None:
            nonlocal last
            now = time.monotonic()
            if now - last < min_interval:
                return
            last = now
            try:
                await message.edit(
                    content=f"{label}\nOCR **{index}/{total}** — `{filename}`"
                )
            except discord.HTTPException:
                pass

        return update

    add = app_commands.Group(name="add", description="Manually add weekly metrics")
    ingest = app_commands.Group(name="ingest", description="Ingest metrics from files/images")

    @add.command(name="versus", description="Add Versus Points for a player")
    @app_commands.describe(
        week="Week start YYYY-MM-DD / current / last",
        player="Player name",
        value="Versus points value",
    )
    async def add_versus(
        self,
        interaction: discord.Interaction,
        player: str,
        value: str,
        week: str | None = None,
    ) -> None:
        await self._add_single(interaction, "VersusPoints", player, value, week)

    @add.command(name="tech", description="Add Tech Contribution for a player")
    @app_commands.describe(
        week="Week start YYYY-MM-DD / current / last",
        player="Player name",
        value="Tech contribution value",
    )
    async def add_tech(
        self,
        interaction: discord.Interaction,
        player: str,
        value: str,
        week: str | None = None,
    ) -> None:
        await self._add_single(interaction, "TechContribution", player, value, week)

    @add.command(name="arena", description="Add Arena Power for a player")
    @app_commands.describe(
        week="Week start YYYY-MM-DD / current / last",
        player="Player name",
        value="Arena Power value",
    )
    async def add_arena(
        self,
        interaction: discord.Interaction,
        player: str,
        value: str,
        week: str | None = None,
    ) -> None:
        await self._add_single(interaction, "ArenaPower", player, value, week)

    @add.command(name="kills", description="Add total Kills for a player")
    @app_commands.describe(
        week="Week start YYYY-MM-DD / current / last",
        player="Player name",
        value="Total kills (lifetime count)",
    )
    async def add_kills(
        self,
        interaction: discord.Interaction,
        player: str,
        value: str,
        week: str | None = None,
    ) -> None:
        await self._add_single(interaction, "Kills", player, value, week)

    @add.command(name="general", description="Add HQ Level and Power for a player")
    @app_commands.describe(
        week="Week start YYYY-MM-DD / current / last",
        player="Player name",
        hq="HQ level",
        power="Power (supports 65.4M)",
    )
    async def add_general(
        self,
        interaction: discord.Interaction,
        player: str,
        hq: str,
        power: str,
        week: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            guild_id = guild_id_from_interaction(interaction)
            channel_id = channel_id_from_interaction(interaction)
            week_start = self._default_week(week)
            hq_val = parse_numeric_value(hq)
            power_val = parse_numeric_value(power)
            await self.bot.db.upsert_metric(
                guild_id,
                week_start,
                player,
                "HQLevel",
                hq_val,
                channel_id=channel_id,
            )
            await self.bot.db.upsert_metric(
                guild_id,
                week_start,
                player,
                "Power",
                power_val,
                channel_id=channel_id,
            )
        except Exception as exc:
            log.exception("add general failed")
            await interaction.followup.send(f"Failed: `{exc}`", ephemeral=True)
            return

        log.info(
            "Added general %s guild=%s week=%s hq=%s power=%s by %s",
            player,
            guild_id,
            week_start,
            hq_val,
            power_val,
            interaction.user,
        )
        await interaction.followup.send(
            f"Saved **{player}** for `{week_start}` — "
            f"HQ **{format_value('HQLevel', hq_val)}**, "
            f"Power **{format_value('Power', power_val)}**.",
            ephemeral=True,
        )

    async def _add_single(
        self,
        interaction: discord.Interaction,
        metric_type: str,
        player: str,
        value: str,
        week: str | None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            guild_id = guild_id_from_interaction(interaction)
            channel_id = channel_id_from_interaction(interaction)
            week_start = self._default_week(week)
            numeric = parse_numeric_value(value)
            await self.bot.db.upsert_metric(
                guild_id,
                week_start,
                player,
                metric_type,
                numeric,
                channel_id=channel_id,
            )
        except Exception as exc:
            log.exception("add %s failed", metric_type)
            await interaction.followup.send(f"Failed: `{exc}`", ephemeral=True)
            return

        log.info(
            "Added %s=%s for %s guild=%s week=%s by %s",
            metric_type,
            numeric,
            player,
            guild_id,
            week_start,
            interaction.user,
        )
        await interaction.followup.send(
            f"Saved **{player}** `{metric_type}` = "
            f"**{format_value(metric_type, numeric)}** for `{week_start}`.",
            ephemeral=True,
        )

    @ingest.command(
        name="image",
        description="OCR up to 10 images on this command (use /ingest batch for more)",
    )
    @app_commands.describe(
        dataset="Data type in the images",
        week="Week start YYYY-MM-DD / current / last",
        image="Screenshot 1",
        image2="Screenshot 2",
        image3="Screenshot 3",
        image4="Screenshot 4",
        image5="Screenshot 5",
        image6="Screenshot 6",
        image7="Screenshot 7",
        image8="Screenshot 8",
        image9="Screenshot 9",
        image10="Screenshot 10",
    )
    @app_commands.choices(dataset=DATASET_CHOICES)
    async def ingest_image(
        self,
        interaction: discord.Interaction,
        image: discord.Attachment,
        dataset: app_commands.Choice[str],
        week: str | None = None,
        image2: discord.Attachment | None = None,
        image3: discord.Attachment | None = None,
        image4: discord.Attachment | None = None,
        image5: discord.Attachment | None = None,
        image6: discord.Attachment | None = None,
        image7: discord.Attachment | None = None,
        image8: discord.Attachment | None = None,
        image9: discord.Attachment | None = None,
        image10: discord.Attachment | None = None,
    ) -> None:
        images = self._filter_images(
            (
                image,
                image2,
                image3,
                image4,
                image5,
                image6,
                image7,
                image8,
                image9,
                image10,
            ),
            limit=MAX_ATTACHMENTS_PER_MESSAGE,
        )
        if not images:
            await interaction.response.send_message(
                "Please upload at least one image file.\n"
                f"Tip: Discord allows **{MAX_ATTACHMENTS_PER_MESSAGE}** attachments "
                f"per message. For larger sets use `/ingest zip` (up to "
                f"{MAX_BATCH_IMAGES} in one .zip) or `/ingest batch`.\n"
                "Fill **image / image2 / …** separately — multi-select on one "
                "slot usually only sends the first file.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        guild_id = guild_id_from_interaction(interaction)
        channel_id = channel_id_from_interaction(interaction)
        kind = dataset.value
        week_start = self._default_week(week)

        names = ", ".join(f"`{img.filename}`" for img in images)
        tip = ""
        if len(images) == 1:
            tip = (
                "\n_Only **1** image was received. Discord multi-select on a single "
                "attachment slot usually keeps one file. Fill image2…image10, or use "
                f"`/ingest batch` for up to {MAX_INGEST_IMAGES} across messages._"
            )
        await interaction.followup.send(
            f"Received **{len(images)}** image(s) for `{kind}` / `{week_start}`: "
            f"{names}.{tip}\nProcessing…",
            ephemeral=True,
        )

        sources, notes = await self._load_sources(images)
        summary = await self._process_attachments(
            sources, kind, week_start, guild_id, channel_id, notes=notes
        )
        await self._send_long_followup(interaction, summary, ephemeral=True)

    @ingest.command(
        name="zip",
        description=f"OCR every image inside a .zip (up to {MAX_BATCH_IMAGES})",
    )
    @app_commands.describe(
        archive=".zip file of screenshots",
        dataset="Data type in the images",
        week="Week start YYYY-MM-DD / current / last",
    )
    @app_commands.choices(dataset=DATASET_CHOICES)
    async def ingest_zip(
        self,
        interaction: discord.Interaction,
        archive: discord.Attachment,
        dataset: app_commands.Choice[str],
        week: str | None = None,
    ) -> None:
        if not _is_zip_attachment(archive):
            await interaction.response.send_message(
                f"`{archive.filename}` isn't a .zip file. "
                "Use `/ingest image` for individual screenshots.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = guild_id_from_interaction(interaction)
        channel_id = channel_id_from_interaction(interaction)
        kind = dataset.value
        week_start = self._default_week(week, context="ingest_zip")

        sources, notes = await self._load_sources([archive])
        sources = self._cap_sources(sources)
        if not sources:
            detail = "\n".join(f"• {n}" for n in notes) or "No images found."
            await interaction.followup.send(
                f"Nothing to process from `{archive.filename}`.\n{detail}",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Unpacked **{len(sources)}** image(s) from `{archive.filename}` "
            f"for `{kind}` / `{week_start}`. Progress is posted in the channel.",
            ephemeral=True,
        )

        # A regular channel message stays editable past the 15-minute
        # interaction window, so long runs can keep reporting progress.
        status: discord.Message | None = None
        label = (
            f"{interaction.user.mention} — OCR `{archive.filename}` "
            f"({len(sources)} image(s), `{kind}` → `{week_start}`)"
        )
        channel = interaction.channel
        if channel is not None and hasattr(channel, "send"):
            try:
                status = await channel.send(  # type: ignore[union-attr]
                    f"{label}\nStarting…",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                status = None

        summary = await self._process_attachments(
            sources,
            kind,
            week_start,
            guild_id,
            channel_id,
            progress=self._progress_editor(status, label) if status else None,
            notes=notes,
        )
        if status is not None:
            short = summary.split("\n\n", 1)[0]
            try:
                await status.edit(content=f"{label}\n{short}"[:1900])
            except discord.HTTPException:
                pass
        await self._send_long_followup(interaction, summary, ephemeral=True)

    @ingest.command(
        name="batch",
        description=(
            f"Collect images or .zip files across messages (up to "
            f"{MAX_INGEST_IMAGES} images, {MAX_BATCH_IMAGES} with zips), then OCR"
        ),
    )
    @app_commands.describe(
        dataset="Data type in the images",
        week="Week start YYYY-MM-DD / current / last",
        timeout_minutes="How long to wait for uploads (1–15)",
    )
    @app_commands.choices(dataset=DATASET_CHOICES)
    async def ingest_batch(
        self,
        interaction: discord.Interaction,
        dataset: app_commands.Choice[str],
        week: str | None = None,
        timeout_minutes: app_commands.Range[int, 1, 15] = 10,
    ) -> None:
        guild_id = guild_id_from_interaction(interaction)
        channel_id = channel_id_from_interaction(interaction)
        kind = dataset.value
        week_start = self._default_week(week, context="ingest_batch")
        log.info(
            "Batch OCR starting guild=%s channel=%s kind=%s week_start=%s timeout_min=%s",
            guild_id,
            channel_id,
            kind,
            week_start,
            timeout_minutes,
        )
        await interaction.response.send_message(
            f"**Batch OCR armed** — `{kind}` → week `{week_start}`\n"
            f"Send images or `.zip` files in this channel (Discord max "
            f"**{MAX_ATTACHMENTS_PER_MESSAGE}** files each).\n"
            f"I'll queue up to **{MAX_INGEST_IMAGES}** loose images, or "
            f"**{MAX_BATCH_IMAGES}** in total once a zip is included.\n"
            f"Type `done` when finished (or wait until the cap / "
            f"{timeout_minutes} min timeout)."
        )

        channel = interaction.channel
        if channel is None:
            await interaction.followup.send("No channel available for batch upload.")
            return

        collected: list[ImageSource] = []
        notes: list[str] = []

        def cap() -> int:
            if any(src.from_archive for src in collected):
                return MAX_BATCH_IMAGES
            return MAX_INGEST_IMAGES

        def check(message: discord.Message) -> bool:
            if message.author.id != interaction.user.id:
                return False
            if message.channel.id != channel.id:
                return False
            content = (message.content or "").strip().lower()
            if content in {"done", "finish", "go", "process"}:
                return True
            return bool(self._filter_uploads(list(message.attachments)))

        deadline = asyncio.get_running_loop().time() + (timeout_minutes * 60)
        while len(collected) < cap():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                message = await self.bot.wait_for(
                    "message", check=check, timeout=remaining
                )
            except asyncio.TimeoutError:
                break

            content = (message.content or "").strip().lower()
            new_sources, new_notes = await self._load_sources(
                self._filter_uploads(list(message.attachments))
            )
            notes.extend(new_notes)
            before = len(collected)
            collected = self._merge_sources(
                collected, new_sources, limit=MAX_BATCH_IMAGES
            )
            added = len(collected) - before

            if content in {"done", "finish", "go", "process"}:
                break

            try:
                await message.add_reaction("✅")
            except discord.HTTPException:
                pass
            dropped = len(new_sources) - added
            extra = "\n".join(f"⚠️ {n}" for n in new_notes)
            if dropped > 0:
                extra += (
                    f"\n⚠️ {dropped} image(s) not queued (duplicate or over the cap)."
                )
            await interaction.followup.send(
                f"Queued **{added}** image(s) "
                f"(**{len(collected)}/{cap()}** total).{extra}"[:1900],
                ephemeral=True,
            )

        if not collected:
            await interaction.followup.send(
                "No images received — batch cancelled.",
                ephemeral=True,
            )
            return

        label = (
            f"{interaction.user.mention} — batch OCR "
            f"({len(collected)} image(s), `{kind}` → `{week_start}`)"
        )
        # Channel message (not a follow-up) so it stays editable after the
        # 15-minute interaction window.
        try:
            status: discord.Message | None = await channel.send(  # type: ignore[union-attr]
                f"{label}\nStarting…",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            status = None
        summary = await self._process_attachments(
            collected,
            kind,
            week_start,
            guild_id,
            channel_id,
            progress=self._progress_editor(status, label) if status else None,
            notes=notes,
        )
        if status is not None:
            short = summary.split("\n\n", 1)[0]
            try:
                await status.edit(content=f"{label}\n{short}"[:1900])
            except discord.HTTPException:
                pass
        await self._send_long_followup(interaction, summary, ephemeral=True)

    @ingest.command(name="text", description="Ingest pasted CSV/text rows")
    @app_commands.describe(
        dataset="Row format",
        data="Paste rows: player,value  OR  player,hq,power",
        week="Week start YYYY-MM-DD / current / last",
    )
    @app_commands.choices(dataset=DATASET_CHOICES)
    async def ingest_text(
        self,
        interaction: discord.Interaction,
        dataset: app_commands.Choice[str],
        data: str,
        week: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = guild_id_from_interaction(interaction)
        channel_id = channel_id_from_interaction(interaction)
        week_start = self._default_week(week)
        try:
            count = await self._ingest_pasted(
                guild_id, channel_id, dataset.value, data, week_start
            )
        except Exception as exc:
            log.exception("Text ingest failed")
            await interaction.followup.send(f"Failed: `{exc}`", ephemeral=True)
            return
        await interaction.followup.send(
            f"Ingested **{count}** metric row(s) for `{week_start}` ({dataset.value}).",
            ephemeral=True,
        )

    async def _ingest_pasted(
        self,
        guild_id: str,
        channel_id: str,
        dataset: str,
        data: str,
        week_start: str,
    ) -> int:
        rows = parse_pasted_rows(data)
        payload: list[tuple[str, str, str, float]] = []

        if dataset == "general":
            for row in rows:
                if len(row) < 3:
                    continue
                if len(row) == 3:
                    name, hq, power = row
                else:
                    power = row[-1]
                    hq = row[-2]
                    name = " ".join(row[:-2])
                payload.append((week_start, name, "HQLevel", parse_numeric_value(hq)))
                payload.append((week_start, name, "Power", parse_numeric_value(power)))
        else:
            metric = resolve_metric(dataset)
            for row in rows:
                if len(row) < 2:
                    continue
                value = row[-1]
                name = " ".join(row[:-1])
                payload.append((week_start, name, metric, parse_numeric_value(value)))

        return await self.bot.db.upsert_metrics(
            guild_id, payload, channel_id=channel_id
        )

    async def _process_attachments(
        self,
        images: list[ImageSource],
        kind: str,
        week_start: str,
        guild_id: str,
        channel_id: str,
        *,
        progress: ProgressCallback | None = None,
        notes: Sequence[str] = (),
    ) -> str:
        if not images:
            return "No images to process."

        truncated = ""
        if len(images) > MAX_BATCH_IMAGES:
            images = images[:MAX_BATCH_IMAGES]
            truncated = f"\n_(Capped at {MAX_BATCH_IMAGES} images.)_"

        engine_label = self._ocr.label

        log.info(
            "OCR batch start: %d image(s) engine=%s kind=%s week=%s "
            "guild=%s channel=%s files=%s",
            len(images),
            engine_label,
            kind,
            week_start,
            guild_id,
            channel_id,
            [img.filename for img in images],
        )

        header = [
            f"**OCR batch** — {len(images)} image(s), engine `{engine_label}`, "
            f"`{kind}` → week `{week_start}`",
            "",
        ]
        sections: list[str] = []
        per_file: list[str] = []
        total_rows = 0
        players: set[str] = set()

        for index, image in enumerate(images, start=1):
            if progress is not None:
                await progress(index, len(images), image.filename)
            try:
                detail, count, names, alerts = await self._process_attachment(
                    image, kind, week_start, guild_id, channel_id
                )
                total_rows += count
                players.update(names)
                per_file.append(
                    f"• `{image.filename}` — **{count}** row(s)"
                    + "".join(f"\n  🚩 {a}" for a in alerts)
                )
                sections.append(
                    f"### {index}/{len(images)} — `{image.filename}`\n{detail}"
                )
                log.info(
                    "OCR batch progress %d/%d file=%s rows=%d",
                    index,
                    len(images),
                    image.filename,
                    count,
                )
            except Exception as exc:
                log.exception("OCR ingest failed for %s", image.filename)
                per_file.append(f"• `{image.filename}` — **failed** ({engine_label})")
                sections.append(
                    f"### {index}/{len(images)} — `{image.filename}`\n"
                    f"**OCR failed** (`{engine_label}`): `{exc}`"
                )

        summary_line = (
            f"Batch complete: **{total_rows}** metric row(s) across "
            f"**{len(players)}** player name(s) from **{len(images)}** image(s)."
        )
        file_rollups = "\n".join(per_file)
        note_lines = [f"⚠️ {n}" for n in notes]
        return "\n".join(
            header
            + [summary_line, truncated, *note_lines]
            + ["", "**Per file:**", file_rollups, ""]
            + sections
        ).strip()

    async def _send_long_followup(
        self,
        interaction: discord.Interaction,
        text: str,
        *,
        ephemeral: bool = True,
    ) -> None:
        limit = 1900
        # Interaction follow-ups stop working 15 minutes after the command; long
        # batches fall back to plain channel messages addressed to the user.
        fallback = interaction.channel if interaction.is_expired() else None
        if fallback is not None and hasattr(fallback, "send"):
            text = f"{interaction.user.mention} — your OCR results:\n{text}"

        async def send(chunk: str) -> None:
            if fallback is not None and hasattr(fallback, "send"):
                await fallback.send(  # type: ignore[union-attr]
                    chunk, allowed_mentions=discord.AllowedMentions(users=True)
                )
            else:
                await interaction.followup.send(chunk, ephemeral=ephemeral)

        if len(text) <= limit:
            await send(text)
            return
        chunks: list[str] = []
        buf: list[str] = []
        size = 0
        for line in text.splitlines(keepends=True):
            if size + len(line) > limit and buf:
                chunks.append("".join(buf))
                buf = []
                size = 0
            buf.append(line)
            size += len(line)
        if buf:
            chunks.append("".join(buf))
        for chunk in chunks[:8]:
            await send(chunk)
        if len(chunks) > 8:
            await send(f"_…truncated {len(chunks) - 8} more chunk(s)._")

    async def _process_attachment(
        self,
        source: ImageSource,
        kind: str,
        week_start: str,
        guild_id: str,
        channel_id: str,
    ) -> tuple[str, int, set[str], list[str]]:
        with tempfile.NamedTemporaryFile(suffix=source.suffix, delete=False) as tmp:
            tmp_path = Path(tmp.name)

        try:
            await asyncio.to_thread(tmp_path.write_bytes, source.data)
            result = await asyncio.to_thread(
                extract_metrics_from_image,
                tmp_path,
                kind=kind,  # type: ignore[arg-type]
                ocr=self._ocr,
            )

            payload = [
                (week_start, m.player_name, m.metric_type, m.value)
                for m in result.metrics
            ]
            # Check against stored history before saving, so this upload's own
            # rows don't become the reference.
            by_metric: dict[str, dict[str, float]] = {}
            for m in result.metrics:
                by_metric.setdefault(m.metric_type, {})[m.player_name] = m.value
            alerts = [
                alert
                for metric_type, values in by_metric.items()
                if (
                    alert := await check_batch(
                        self.bot.db,
                        guild_id,
                        channel_id,
                        week_start,
                        metric_type,
                        values,
                    )
                )
            ]
            count = await self.bot.db.upsert_metrics(
                guild_id, payload, channel_id=channel_id
            )
            names = {m.player_name for m in result.metrics}

            lines = [
                f"**OCR ingest complete** (`{self._ocr.label}` / `{result.kind}` → week `{week_start}`)",
                f"Saved **{count}** metric row(s) from **{len(names)}** player(s).",
                *(f"🚩 {a}" for a in alerts),
                "",
            ]
            for m in result.metrics[:20]:
                lines.append(
                    f"• {m.player_name} — {m.metric_type}: "
                    f"{format_value(m.metric_type, m.value)}"
                )
            if len(result.metrics) > 20:
                lines.append(f"_…and {len(result.metrics) - 20} more_")
            for w in result.warnings:
                lines.append(f"⚠️ {w}")
            log.info(
                "OCR saved %d rows kind=%s week=%s guild=%s channel=%s file=%s",
                count,
                result.kind,
                week_start,
                guild_id,
                channel_id,
                source.filename,
            )
            return "\n".join(lines), count, names, alerts
        finally:
            tmp_path.unlink(missing_ok=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Auto-OCR images posted in the configured OCR channel."""
        if message.author.bot:
            return
        if message.guild is None:
            return
        channel_id = self.bot.settings.ocr_channel_id
        if not channel_id or message.channel.id != channel_id:
            return
        if not message.attachments:
            return

        uploads = self._filter_uploads(list(message.attachments))
        if not uploads:
            return

        guild_id = str(message.guild.id)
        msg_channel_id = channel_id_from_message(message)
        hint = (message.content or "").strip().split()
        kind = hint[0].lower() if hint else ""
        if kind not in DATASET_KINDS:
            await message.reply(
                "Which dataset is this? Re-post with the type as the first word "
                f"of the message: `{DATASET_USAGE}` "
                "(optionally followed by a week, e.g. `kills current`)."
            )
            return
        week = None
        if len(hint) >= 2:
            try:
                week = parse_week_start(hint[1])
            except ValueError:
                week = None

        week_start = week or self._default_week(None)
        sources, notes = await self._load_sources(uploads)
        sources = self._cap_sources(sources)
        if not sources:
            await message.reply("\n".join(notes) or "No images found.")
            return
        label = (
            f"Processing **{len(sources)}** image(s) with OCR for week `{week_start}`"
        )
        status = await message.reply(
            f"{label}…\n"
            f"_For more across several messages, use `/ingest batch` "
            f"or upload a `.zip`._"
        )
        summary = await self._process_attachments(
            sources,
            kind,
            week_start,
            guild_id,
            msg_channel_id,
            progress=self._progress_editor(status, label),
            notes=notes,
        )
        if len(summary) > 1900:
            summary = summary[:1900] + "\n…"
        await status.edit(content=summary)

    @commands.command(name="ingestimage")
    async def ingest_image_prefix(
        self,
        ctx: commands.Context,
        dataset: str | None = None,
        week: str | None = None,
    ) -> None:
        """OCR images or .zip files on this message. Usage: !ingestimage kills current"""
        kind = (dataset or "").lower().strip()
        if kind not in DATASET_KINDS:
            await ctx.reply(
                f"Usage: `!ingestimage <dataset> [week]` — dataset is one of "
                f"`{DATASET_USAGE}`."
            )
            return
        uploads = self._filter_uploads(list(ctx.message.attachments))
        if not uploads:
            await ctx.reply(
                f"Attach 1–{MAX_ATTACHMENTS_PER_MESSAGE} images or a `.zip` to this "
                f"message. For larger sets use `/ingest batch` or `/ingest zip`."
            )
            return
        guild_id = guild_id_from_context(ctx)
        channel_id = channel_id_from_context(ctx)
        week_start = self._default_week(week)
        sources, notes = await self._load_sources(uploads)
        sources = self._cap_sources(sources)
        if not sources:
            await ctx.reply("\n".join(notes) or "No images found.")
            return
        label = f"Processing **{len(sources)}** image(s) (`{kind}` → `{week_start}`)"
        status = await ctx.reply(f"{label}…")
        summary = await self._process_attachments(
            sources,
            kind,
            week_start,
            guild_id,
            channel_id,
            progress=self._progress_editor(status, label),
            notes=notes,
        )
        if len(summary) > 1900:
            summary = summary[:1900] + "\n…"
        await status.edit(content=summary)

    async def _add_single_prefix(
        self,
        ctx: commands.Context,
        metric_type: str,
        player: str,
        value: str,
        week: str | None,
    ) -> None:
        guild_id = guild_id_from_context(ctx)
        channel_id = channel_id_from_context(ctx)
        week_start = self._default_week(week)
        numeric = parse_numeric_value(value)
        await self.bot.db.upsert_metric(
            guild_id,
            week_start,
            player,
            metric_type,
            numeric,
            channel_id=channel_id,
        )
        await ctx.reply(
            f"Saved **{player}** {metric_type}="
            f"{format_value(metric_type, numeric)} `{week_start}`"
        )

    @commands.command(name="addversus")
    async def add_versus_prefix(
        self, ctx: commands.Context, player: str, value: str, week: str | None = None
    ) -> None:
        await self._add_single_prefix(ctx, "VersusPoints", player, value, week)

    @commands.command(name="addtech")
    async def add_tech_prefix(
        self, ctx: commands.Context, player: str, value: str, week: str | None = None
    ) -> None:
        await self._add_single_prefix(ctx, "TechContribution", player, value, week)

    @commands.command(name="addarena")
    async def add_arena_prefix(
        self, ctx: commands.Context, player: str, value: str, week: str | None = None
    ) -> None:
        await self._add_single_prefix(ctx, "ArenaPower", player, value, week)

    @commands.command(name="addkills")
    async def add_kills_prefix(
        self, ctx: commands.Context, player: str, value: str, week: str | None = None
    ) -> None:
        await self._add_single_prefix(ctx, "Kills", player, value, week)

    @commands.command(name="addgeneral")
    async def add_general_prefix(
        self,
        ctx: commands.Context,
        player: str,
        hq: str,
        power: str,
        week: str | None = None,
    ) -> None:
        guild_id = guild_id_from_context(ctx)
        channel_id = channel_id_from_context(ctx)
        week_start = self._default_week(week)
        hq_val = parse_numeric_value(hq)
        power_val = parse_numeric_value(power)
        await self.bot.db.upsert_metric(
            guild_id,
            week_start,
            player,
            "HQLevel",
            hq_val,
            channel_id=channel_id,
        )
        await self.bot.db.upsert_metric(
            guild_id,
            week_start,
            player,
            "Power",
            power_val,
            channel_id=channel_id,
        )
        await ctx.reply(
            f"Saved **{player}** HQ={hq_val:.0f} Power={format_value('Power', power_val)} `{week_start}`"
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Ingest(bot))
