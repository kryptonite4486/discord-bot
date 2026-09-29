"""Data ingestion: manual add, CSV paste, and OCR image upload."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from bot.config import resolve_metric
from bot.ocr import OCREngine, extract_metrics_from_image
from bot.utils.guild import guild_id_from_context, guild_id_from_interaction
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

DATASET_CHOICES = [
    app_commands.Choice(name="Auto-detect", value="auto"),
    app_commands.Choice(name="Versus", value="versus"),
    app_commands.Choice(name="Tech", value="tech"),
    app_commands.Choice(name="General (HQ+Power)", value="general"),
    app_commands.Choice(name="Power leaderboard", value="power"),
]

DATASET_REQUIRED_CHOICES = [
    app_commands.Choice(name="Versus", value="versus"),
    app_commands.Choice(name="Tech", value="tech"),
    app_commands.Choice(name="General (HQ+Power)", value="general"),
    app_commands.Choice(name="Power leaderboard", value="power"),
    app_commands.Choice(name="Auto-detect", value="auto"),
]


def _is_image_attachment(attachment: discord.Attachment) -> bool:
    if attachment.content_type and attachment.content_type.startswith("image/"):
        return True
    return Path(attachment.filename).suffix.lower() in {
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
        ".bmp",
    }


class Ingest(commands.Cog):
    """Commands for adding weekly player metrics."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._ocr_engine: OCREngine | None = None

    def _engine(self) -> OCREngine:
        if self._ocr_engine is None:
            s = self.bot.settings
            log.info(
                "Initializing OCR engine=%s%s",
                s.ocr_engine,
                (
                    f" model={s.ocr_vision_model} url={s.ocr_vision_base_url}"
                    if s.ocr_engine == "vision"
                    else ""
                ),
            )
            self._ocr_engine = OCREngine(
                s.ocr_engine,
                vision_base_url=s.ocr_vision_base_url,
                vision_model=s.ocr_vision_model,
                vision_api_key=s.ocr_vision_api_key,
                vision_timeout=s.ocr_vision_timeout,
            )
        return self._ocr_engine

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
    def _merge_unique(
        *groups: list[discord.Attachment],
        limit: int = MAX_INGEST_IMAGES,
    ) -> list[discord.Attachment]:
        seen: set[int] = set()
        out: list[discord.Attachment] = []
        for group in groups:
            for image in group:
                if image.id in seen:
                    continue
                seen.add(image.id)
                out.append(image)
                if len(out) >= limit:
                    return out
        return out

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
            week_start = self._default_week(week)
            hq_val = parse_numeric_value(hq)
            power_val = parse_numeric_value(power)
            await self.bot.db.upsert_metric(
                guild_id, week_start, player, "HQLevel", hq_val
            )
            await self.bot.db.upsert_metric(
                guild_id, week_start, player, "Power", power_val
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
            week_start = self._default_week(week)
            numeric = parse_numeric_value(value)
            await self.bot.db.upsert_metric(
                guild_id, week_start, player, metric_type, numeric
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
        dataset: app_commands.Choice[str] | None = None,
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
                f"per message. For larger sets use `/ingest batch` "
                f"(up to {MAX_INGEST_IMAGES}).\n"
                "Fill **image / image2 / …** separately — multi-select on one "
                "slot usually only sends the first file.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        guild_id = guild_id_from_interaction(interaction)
        kind = dataset.value if dataset else "auto"
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

        summary = await self._process_attachments(
            images, kind, week_start, guild_id
        )
        await self._send_long_followup(interaction, summary, ephemeral=True)

    @ingest.command(
        name="batch",
        description=f"Collect up to {MAX_INGEST_IMAGES} images across messages, then OCR",
    )
    @app_commands.describe(
        dataset="Data type in the images",
        week="Week start YYYY-MM-DD / current / last",
        timeout_minutes="How long to wait for uploads (1–15)",
    )
    @app_commands.choices(dataset=DATASET_REQUIRED_CHOICES)
    async def ingest_batch(
        self,
        interaction: discord.Interaction,
        dataset: app_commands.Choice[str],
        week: str | None = None,
        timeout_minutes: app_commands.Range[int, 1, 15] = 10,
    ) -> None:
        guild_id = guild_id_from_interaction(interaction)
        kind = dataset.value
        week_start = self._default_week(week, context="ingest_batch")
        log.info(
            "Batch OCR starting guild=%s kind=%s week_start=%s timeout_min=%s",
            guild_id,
            kind,
            week_start,
            timeout_minutes,
        )
        await interaction.response.send_message(
            f"**Batch OCR armed** — `{kind}` → week `{week_start}`\n"
            f"Send image messages in this channel (Discord max "
            f"**{MAX_ATTACHMENTS_PER_MESSAGE}** files each).\n"
            f"I'll queue up to **{MAX_INGEST_IMAGES}** images.\n"
            f"Type `done` when finished (or wait until the cap / "
            f"{timeout_minutes} min timeout)."
        )

        channel = interaction.channel
        if channel is None:
            await interaction.followup.send("No channel available for batch upload.")
            return

        collected: list[discord.Attachment] = []

        def check(message: discord.Message) -> bool:
            if message.author.id != interaction.user.id:
                return False
            if message.channel.id != channel.id:
                return False
            content = (message.content or "").strip().lower()
            if content in {"done", "finish", "go", "process"}:
                return True
            return bool(
                self._filter_images(
                    list(message.attachments),
                    limit=MAX_ATTACHMENTS_PER_MESSAGE,
                )
            )

        deadline = asyncio.get_running_loop().time() + (timeout_minutes * 60)
        while len(collected) < MAX_INGEST_IMAGES:
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
            new_images = self._filter_images(
                list(message.attachments),
                limit=MAX_ATTACHMENTS_PER_MESSAGE,
            )
            before = len(collected)
            collected = self._merge_unique(collected, new_images)
            added = len(collected) - before

            if content in {"done", "finish", "go", "process"}:
                break

            try:
                await message.add_reaction("✅")
            except discord.HTTPException:
                pass
            await interaction.followup.send(
                f"Queued **{added}** image(s) "
                f"(**{len(collected)}/{MAX_INGEST_IMAGES}** total).",
                ephemeral=True,
            )
            if len(collected) >= MAX_INGEST_IMAGES:
                break

        if not collected:
            await interaction.followup.send(
                "No images received — batch cancelled.",
                ephemeral=True,
            )
            return

        status = await interaction.followup.send(
            f"Processing **{len(collected)}** queued image(s)…"
        )
        summary = await self._process_attachments(
            collected, kind, week_start, guild_id
        )
        short = summary.split("\n\n", 1)[0]
        try:
            await status.edit(content=short[:1900])
        except discord.HTTPException:
            pass
        await self._send_long_followup(interaction, summary, ephemeral=True)

    @ingest.command(name="text", description="Ingest pasted CSV/text rows")
    @app_commands.describe(
        dataset="Row format",
        data="Paste rows: player,value  OR  player,hq,power",
        week="Week start YYYY-MM-DD / current / last",
    )
    @app_commands.choices(
        dataset=[
            app_commands.Choice(name="Versus", value="versus"),
            app_commands.Choice(name="Tech", value="tech"),
            app_commands.Choice(name="General (HQ+Power)", value="general"),
        ]
    )
    async def ingest_text(
        self,
        interaction: discord.Interaction,
        dataset: app_commands.Choice[str],
        data: str,
        week: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = guild_id_from_interaction(interaction)
        week_start = self._default_week(week)
        try:
            count = await self._ingest_pasted(
                guild_id, dataset.value, data, week_start
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

        return await self.bot.db.upsert_metrics(guild_id, payload)

    async def _process_attachments(
        self,
        images: list[discord.Attachment],
        kind: str,
        week_start: str,
        guild_id: str,
    ) -> str:
        if not images:
            return "No images to process."

        truncated = ""
        if len(images) > MAX_INGEST_IMAGES:
            images = images[:MAX_INGEST_IMAGES]
            truncated = f"\n_(Capped at {MAX_INGEST_IMAGES} images.)_"

        engine = self._engine()
        engine_label = engine.engine_name
        if engine.is_vision:
            engine_label = f"vision/{engine.vision_model}"

        log.info(
            "OCR batch start: %d image(s) engine=%s kind=%s week=%s guild=%s files=%s",
            len(images),
            engine_label,
            kind,
            week_start,
            guild_id,
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
            try:
                detail, count, names = await self._process_attachment(
                    image, kind, week_start, guild_id
                )
                total_rows += count
                players.update(names)
                per_file.append(f"• `{image.filename}` — **{count}** row(s)")
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
                engine = self._engine()
                engine_tag = (
                    f"vision/{engine.vision_model}"
                    if engine.is_vision
                    else engine.engine_name
                )
                per_file.append(f"• `{image.filename}` — **failed** ({engine_tag})")
                sections.append(
                    f"### {index}/{len(images)} — `{image.filename}`\n"
                    f"**OCR failed** (`{engine_tag}`): `{exc}`"
                )

        summary_line = (
            f"Batch complete: **{total_rows}** metric row(s) across "
            f"**{len(players)}** player name(s) from **{len(images)}** image(s)."
        )
        file_rollups = "\n".join(per_file)
        return "\n".join(
            header
            + [summary_line, truncated, "", "**Per file:**", file_rollups, ""]
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
        if len(text) <= limit:
            await interaction.followup.send(text, ephemeral=ephemeral)
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
            await interaction.followup.send(chunk, ephemeral=ephemeral)
        if len(chunks) > 8:
            await interaction.followup.send(
                f"_…truncated {len(chunks) - 8} more chunk(s)._",
                ephemeral=ephemeral,
            )

    async def _process_attachment(
        self,
        attachment: discord.Attachment,
        kind: str,
        week_start: str,
        guild_id: str,
    ) -> tuple[str, int, set[str]]:
        suffix = Path(attachment.filename).suffix or ".png"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = Path(tmp.name)

        try:
            await attachment.save(tmp_path)
            metric_override = {
                "tech": "TechContribution",
                "versus": "VersusPoints",
                "power": "Power",
            }.get(kind)

            result = await asyncio.to_thread(
                extract_metrics_from_image,
                tmp_path,
                kind=kind,  # type: ignore[arg-type]
                engine=self._engine(),
                metric_type_override=metric_override,
            )

            engine = self._engine()
            engine_tag = (
                f"vision/{engine.vision_model}"
                if engine.is_vision
                else engine.engine_name
            )

            payload = [
                (week_start, m.player_name, m.metric_type, m.value)
                for m in result.metrics
            ]
            count = await self.bot.db.upsert_metrics(guild_id, payload)
            names = {m.player_name for m in result.metrics}

            lines = [
                f"**OCR ingest complete** (`{engine_tag}` / `{result.kind}` → week `{week_start}`)",
                f"Saved **{count}** metric row(s) from **{len(names)}** player(s).",
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
                "OCR saved %d rows kind=%s week=%s guild=%s file=%s",
                count,
                result.kind,
                week_start,
                guild_id,
                attachment.filename,
            )
            return "\n".join(lines), count, names
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

        images = self._filter_images(
            list(message.attachments),
            limit=MAX_ATTACHMENTS_PER_MESSAGE,
        )
        if not images:
            return

        guild_id = str(message.guild.id)
        hint = (message.content or "").strip().split()
        kind = "auto"
        week = None
        if hint:
            token = hint[0].lower()
            if token in {"versus", "tech", "general", "power", "auto"}:
                kind = token
            if len(hint) >= 2:
                try:
                    week = parse_week_start(hint[1])
                except ValueError:
                    week = None

        week_start = week or self._default_week(None)
        image_count_on_msg = len(
            [a for a in message.attachments if _is_image_attachment(a)]
        )
        status = await message.reply(
            f"Processing **{len(images)}** image(s) with OCR for week `{week_start}`"
            + (
                f" (message had {image_count_on_msg}; Discord max "
                f"{MAX_ATTACHMENTS_PER_MESSAGE}/message)"
                if image_count_on_msg > len(images)
                else ""
            )
            + "…\n"
            + f"_For up to {MAX_INGEST_IMAGES} across several messages, use `/ingest batch`._"
        )
        summary = await self._process_attachments(
            images, kind, week_start, guild_id
        )
        if len(summary) > 1900:
            summary = summary[:1900] + "\n…"
        await status.edit(content=summary)

    @commands.command(name="ingestimage")
    async def ingest_image_prefix(
        self,
        ctx: commands.Context,
        dataset: str = "auto",
        week: str | None = None,
    ) -> None:
        """OCR attachments on this message (max 10). Usage: !ingestimage versus current"""
        images = self._filter_images(
            list(ctx.message.attachments),
            limit=MAX_ATTACHMENTS_PER_MESSAGE,
        )
        if not images:
            await ctx.reply(
                f"Attach 1–{MAX_ATTACHMENTS_PER_MESSAGE} images to this message. "
                f"For larger sets use `/ingest batch` (up to {MAX_INGEST_IMAGES})."
            )
            return
        kind = dataset.lower().strip()
        if kind not in {"versus", "tech", "general", "power", "auto"}:
            await ctx.reply("Dataset must be versus, tech, general, power, or auto.")
            return
        guild_id = guild_id_from_context(ctx)
        week_start = self._default_week(week)
        status = await ctx.reply(
            f"Processing **{len(images)}** image(s) (`{kind}` → `{week_start}`)…"
        )
        summary = await self._process_attachments(
            images, kind, week_start, guild_id
        )
        if len(summary) > 1900:
            summary = summary[:1900] + "\n…"
        await status.edit(content=summary)

    @commands.command(name="addversus")
    async def add_versus_prefix(
        self, ctx: commands.Context, player: str, value: str, week: str | None = None
    ) -> None:
        guild_id = guild_id_from_context(ctx)
        week_start = self._default_week(week)
        numeric = parse_numeric_value(value)
        await self.bot.db.upsert_metric(
            guild_id, week_start, player, "VersusPoints", numeric
        )
        await ctx.reply(
            f"Saved **{player}** VersusPoints={format_value('VersusPoints', numeric)} `{week_start}`"
        )

    @commands.command(name="addtech")
    async def add_tech_prefix(
        self, ctx: commands.Context, player: str, value: str, week: str | None = None
    ) -> None:
        guild_id = guild_id_from_context(ctx)
        week_start = self._default_week(week)
        numeric = parse_numeric_value(value)
        await self.bot.db.upsert_metric(
            guild_id, week_start, player, "TechContribution", numeric
        )
        await ctx.reply(
            f"Saved **{player}** TechContribution={format_value('TechContribution', numeric)} `{week_start}`"
        )

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
        week_start = self._default_week(week)
        hq_val = parse_numeric_value(hq)
        power_val = parse_numeric_value(power)
        await self.bot.db.upsert_metric(
            guild_id, week_start, player, "HQLevel", hq_val
        )
        await self.bot.db.upsert_metric(
            guild_id, week_start, player, "Power", power_val
        )
        await ctx.reply(
            f"Saved **{player}** HQ={hq_val:.0f} Power={format_value('Power', power_val)} `{week_start}`"
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Ingest(bot))
