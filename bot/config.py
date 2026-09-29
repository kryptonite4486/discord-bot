"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _optional_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    return int(raw)


@dataclass(frozen=True)
class Settings:
    discord_token: str
    command_prefix: str
    database_path: Path
    ocr_channel_id: int | None
    ocr_engine: str
    default_week_start: str | None
    log_level: str
    dev_guild_id: int | None
    app_id: int | None

    @classmethod
    def from_env(cls) -> Settings:
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token or token == "your_bot_token_here":
            raise RuntimeError(
                "DISCORD_TOKEN is required. Copy .env.example to .env and set your bot token."
            )

        db_path = Path(os.getenv("DATABASE_PATH", "/app/data/weekly.db"))
        default_week = os.getenv("DEFAULT_WEEK_START", "").strip() or None
        engine = os.getenv("OCR_ENGINE", "easyocr").strip().lower()
        if engine not in {"easyocr", "tesseract"}:
            engine = "easyocr"

        return cls(
            discord_token=token,
            command_prefix=os.getenv("COMMAND_PREFIX", "!").strip() or "!",
            database_path=db_path,
            ocr_channel_id=_optional_int("OCR_CHANNEL_ID"),
            ocr_engine=engine,
            default_week_start=default_week,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            dev_guild_id=_optional_int("DEV_GUILD_ID"),
            app_id=_optional_int("DISCORD_APP_ID"),
        )


METRIC_TYPES = (
    "VersusPoints",
    "TechContribution",
    "HQLevel",
    "Power",
)

METRIC_ALIASES = {
    "versus": "VersusPoints",
    "versuspoints": "VersusPoints",
    "vp": "VersusPoints",
    "tech": "TechContribution",
    "techcontribution": "TechContribution",
    "hq": "HQLevel",
    "hqlevel": "HQLevel",
    "power": "Power",
}


def resolve_metric(name: str) -> str:
    """Map a user-facing metric name to a canonical MetricType."""
    key = name.strip().lower().replace(" ", "").replace("_", "")
    if key in METRIC_ALIASES:
        return METRIC_ALIASES[key]
    for metric in METRIC_TYPES:
        if metric.lower() == key:
            return metric
    raise ValueError(
        f"Unknown metric '{name}'. Valid: versus, tech, hq, power"
    )
