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
from bot.utils.parsing import (
    format_value,
    parse_numeric_value,
    parse_pasted_rows,
    parse_week_start,
)

log = logging.getLogger(__name__)


class Ingest(commands.Cog):
    """Commands for adding weekly player metrics."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._ocr_engine: OCREngine | None = None

    def _engine(self) -> OCREngine:
        if self._ocr_engine is None:
            self._ocr_engine = OCREngine(self.bot.settings.ocr_engine)
        return self._ocr_engine

    def _default_week(self, week: str | None) -> str:
        if week:
            return parse_week_start(week)
        if self.bot.settings.default_week_start:
            return parse_week_start(self.bot.settings.default_week_start)
        return parse_week_start(None)

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
            week_start = self._default_week(week)
            hq_val = parse_numeric_value(hq)
            power_val = parse_numeric_value(power)
            await self.bot.db.upsert_metric(week_start, player, "HQLevel", hq_val)
            await self.bot.db.upsert_metric(week_start, player, "Power", power_val)
        except Exception as exc:
            log.exception("add general failed")
            await interaction.followup.send(f"Failed: `{exc}`", ephemeral=True)
            return

        log.info(
            "Added general %s week=%s hq=%s power=%s by %s",
            player,
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
            week_start = self._default_week(week)
            numeric = parse_numeric_value(value)
            await self.bot.db.upsert_metric(week_start, player, metric_type, numeric)
        except Exception as exc:
            log.exception("add %s failed", metric_type)
            await interaction.followup.send(f"Failed: `{exc}`", ephemeral=True)
            return

        log.info(
            "Added %s=%s for %s week=%s by %s",
            metric_type,
            numeric,
            player,
            week_start,
            interaction.user,
        )
        await interaction.followup.send(
            f"Saved **{player}** `{metric_type}` = "
            f"**{format_value(metric_type, numeric)}** for `{week_start}`.",
            ephemeral=True,
        )

    @ingest.command(name="image", description="OCR extract metrics from an uploaded image")
    @app_commands.describe(
        image="Screenshot to parse",
        dataset="Data type in the image",
        week="Week start YYYY-MM-DD / current / last",
    )
    @app_commands.choices(
        dataset=[
            app_commands.Choice(name="Auto-detect", value="auto"),
            app_commands.Choice(name="Versus", value="versus"),
            app_commands.Choice(name="Tech", value="tech"),
            app_commands.Choice(name="General (HQ+Power)", value="general"),
            app_commands.Choice(name="Power leaderboard", value="power"),
        ]
    )
    async def ingest_image(
        self,
        interaction: discord.Interaction,
        image: discord.Attachment,
        dataset: app_commands.Choice[str] | None = None,
        week: str | None = None,
    ) -> None:
        if not image.content_type or not image.content_type.startswith("image/"):
            await interaction.response.send_message(
                "Please upload an image file.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        kind = dataset.value if dataset else "auto"
        week_start = self._default_week(week)

        try:
            summary = await self._process_attachment(image, kind, week_start)
        except Exception as exc:
            log.exception("OCR ingest failed")
            await interaction.followup.send(f"OCR failed: `{exc}`", ephemeral=True)
            return

        await interaction.followup.send(summary, ephemeral=True)

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
        week_start = self._default_week(week)
        try:
            count = await self._ingest_pasted(dataset.value, data, week_start)
        except Exception as exc:
            log.exception("Text ingest failed")
            await interaction.followup.send(f"Failed: `{exc}`", ephemeral=True)
            return
        await interaction.followup.send(
            f"Ingested **{count}** metric row(s) for `{week_start}` ({dataset.value}).",
            ephemeral=True,
        )

    async def _ingest_pasted(self, dataset: str, data: str, week_start: str) -> int:
        rows = parse_pasted_rows(data)
        payload: list[tuple[str, str, str, float]] = []

        if dataset == "general":
            for row in rows:
                if len(row) < 3:
                    # try "name hq power" already split
                    if len(row) >= 3:
                        pass
                    else:
                        continue
                # If first cells joined oddly, assume last two are hq/power
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

        return await self.bot.db.upsert_metrics(payload)

    async def _process_attachment(
        self,
        attachment: discord.Attachment,
        kind: str,
        week_start: str,
    ) -> str:
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

            payload = [
                (week_start, m.player_name, m.metric_type, m.value)
                for m in result.metrics
            ]
            count = await self.bot.db.upsert_metrics(payload)

            lines = [
                f"**OCR ingest complete** (`{result.kind}` → week `{week_start}`)",
                f"Saved **{count}** metric row(s) from **{len({m.player_name for m in result.metrics})}** player(s).",
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
                "OCR saved %d rows kind=%s week=%s file=%s",
                count,
                result.kind,
                week_start,
                attachment.filename,
            )
            return "\n".join(lines)
        finally:
            tmp_path.unlink(missing_ok=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Auto-OCR images posted in the configured OCR channel."""
        if message.author.bot:
            return
        channel_id = self.bot.settings.ocr_channel_id
        if not channel_id or message.channel.id != channel_id:
            return
        if not message.attachments:
            return

        images = [
            a for a in message.attachments
            if a.content_type and a.content_type.startswith("image/")
        ]
        if not images:
            return

        # Optional: first word can hint dataset type
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
        status = await message.reply(
            f"Processing {len(images)} image(s) with OCR for week `{week_start}`…"
        )
        summaries = []
        for image in images[:5]:
            try:
                summaries.append(await self._process_attachment(image, kind, week_start))
            except Exception as exc:
                log.exception("Channel OCR failed for %s", image.filename)
                summaries.append(f"**{image.filename}**: failed — `{exc}`")

        text = "\n\n".join(summaries)
        if len(text) > 1900:
            text = text[:1900] + "\n…"
        await status.edit(content=text)

    # Prefix helpers
    @commands.command(name="addversus")
    async def add_versus_prefix(
        self, ctx: commands.Context, player: str, value: str, week: str | None = None
    ) -> None:
        week_start = self._default_week(week)
        numeric = parse_numeric_value(value)
        await self.bot.db.upsert_metric(week_start, player, "VersusPoints", numeric)
        await ctx.reply(
            f"Saved **{player}** VersusPoints={format_value('VersusPoints', numeric)} `{week_start}`"
        )

    @commands.command(name="addtech")
    async def add_tech_prefix(
        self, ctx: commands.Context, player: str, value: str, week: str | None = None
    ) -> None:
        week_start = self._default_week(week)
        numeric = parse_numeric_value(value)
        await self.bot.db.upsert_metric(week_start, player, "TechContribution", numeric)
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
        week_start = self._default_week(week)
        hq_val = parse_numeric_value(hq)
        power_val = parse_numeric_value(power)
        await self.bot.db.upsert_metric(week_start, player, "HQLevel", hq_val)
        await self.bot.db.upsert_metric(week_start, player, "Power", power_val)
        await ctx.reply(
            f"Saved **{player}** HQ={hq_val:.0f} Power={format_value('Power', power_val)} `{week_start}`"
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Ingest(bot))
