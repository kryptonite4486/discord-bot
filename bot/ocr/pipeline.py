"""OCR for game screenshots via an OpenAI-compatible vision model."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from bot.config import LEADERBOARD_DATASETS, MEMBER_CARD_DATASETS

log = logging.getLogger(__name__)

# Keep in sync with bot.config.DATASET_KINDS.
DatasetKind = Literal["versus", "tech", "general", "power", "arena", "kills"]


@dataclass
class ExtractedMetric:
    player_name: str
    metric_type: str
    value: float


@dataclass
class OCRResult:
    kind: str
    metrics: list[ExtractedMetric]
    raw_text: str
    warnings: list[str]


@dataclass(frozen=True)
class VisionOCR:
    """Connection settings for the vision model server (e.g. oMLX)."""

    base_url: str
    model: str
    api_key: str = ""
    timeout: float = 120.0

    @property
    def label(self) -> str:
        return f"vision/{self.model}"


def relabel_member_cards(
    metrics: list[ExtractedMetric], kind: str
) -> list[ExtractedMetric]:
    """Map member-card rows (HQLevel + Power) to the metrics ``kind`` stores."""
    if kind == "general":
        return metrics
    power_metric = MEMBER_CARD_DATASETS[kind]
    return [
        ExtractedMetric(m.player_name, power_metric, m.value)
        for m in metrics
        if m.metric_type == "Power"
    ]


def extract_metrics_from_image(
    image_path: Path | str,
    *,
    kind: DatasetKind,
    ocr: VisionOCR,
) -> OCRResult:
    """
    Run vision OCR and parse into WeeklyMetrics-ready rows.

    kind:
      - versus -> VersusPoints
      - tech -> TechContribution
      - general -> HQLevel + Power
      - power -> Power (leaderboard-style values)
      - kills -> Kills (leaderboard-style values)
      - arena -> ArenaPower (member cards, same layout as general)

    Screens of the same layout are indistinguishable (general vs arena, power
    vs kills), so the caller must always say which one a screenshot is.
    """
    if kind not in LEADERBOARD_DATASETS and kind not in MEMBER_CARD_DATASETS:
        raise ValueError(f"Unknown dataset kind: {kind!r}")

    # Lazy import avoids circular dependency with bot.ocr.vision
    from bot.ocr.vision import extract_metrics_via_vision

    log.info("Vision OCR model=%s kind=%s", ocr.model, kind)
    return extract_metrics_via_vision(
        image_path,
        kind=kind,
        base_url=ocr.base_url,
        model=ocr.model,
        api_key=ocr.api_key,
        timeout=ocr.timeout,
    )
