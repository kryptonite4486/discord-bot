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


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


_OCR_ENGINES = frozenset({"easyocr", "tesseract", "vision", "omlx"})


@dataclass(frozen=True)
class Settings:
    discord_token: str
    command_prefix: str
    database_path: Path
    ocr_channel_id: int | None
    ocr_engine: str
    ocr_vision_base_url: str
    ocr_vision_model: str
    ocr_vision_api_key: str
    ocr_vision_timeout: float
    default_week_start: str | None
    log_level: str
    dev_guild_id: int | None
    legacy_guild_id: str | None
    app_id: int | None
    message_content_intent: bool

    @classmethod
    def from_env(cls) -> Settings:
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token or token == "your_bot_token_here":
            raise RuntimeError(
                "DISCORD_TOKEN is required. Copy .env.example to .env and set your bot token."
            )

        db_path = Path(os.getenv("DATABASE_PATH", "/app/data/weekly.db"))
        default_week = os.getenv("DEFAULT_WEEK_START", "").strip() or None
        raw_engine = os.getenv("OCR_ENGINE", "easyocr").strip().lower() or "easyocr"
        engine = "vision" if raw_engine == "omlx" else raw_engine
        # Never silently remap unknown engines (e.g. vision→easyocr hid misconfigured deploys).
        if engine not in _OCR_ENGINES:
            raise RuntimeError(
                f"Invalid OCR_ENGINE={raw_engine!r}. "
                f"Expected one of: easyocr, tesseract, vision (alias: omlx)."
            )

        timeout_raw = os.getenv("OCR_VISION_TIMEOUT", "120").strip() or "120"
        try:
            vision_timeout = float(timeout_raw)
        except ValueError:
            vision_timeout = 120.0

        vision_base = (
            os.getenv("OCR_VISION_BASE_URL", "http://127.0.0.1:8000/v1").strip()
            or "http://127.0.0.1:8000/v1"
        )
        vision_model = (
            os.getenv("OCR_VISION_MODEL", "Qwen2.5-VL-7B-Instruct-4bit").strip()
            or "Qwen2.5-VL-7B-Instruct-4bit"
        )

        return cls(
            discord_token=token,
            command_prefix=os.getenv("COMMAND_PREFIX", "!").strip() or "!",
            database_path=db_path,
            ocr_channel_id=_optional_int("OCR_CHANNEL_ID"),
            ocr_engine=engine,
            ocr_vision_base_url=vision_base,
            ocr_vision_model=vision_model,
            ocr_vision_api_key=os.getenv("OCR_VISION_API_KEY", "").strip(),
            ocr_vision_timeout=vision_timeout,
            default_week_start=default_week,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            dev_guild_id=_optional_int("DEV_GUILD_ID"),
            legacy_guild_id=(
                os.getenv("LEGACY_GUILD_ID", "").strip() or None
            ),
            app_id=_optional_int("DISCORD_APP_ID"),
            # Requires Privileged Gateway Intent in the Discord Developer Portal
            message_content_intent=_env_bool("MESSAGE_CONTENT_INTENT", True),
        )


METRIC_TYPES = (
    "VersusPoints",
    "TechContribution",
    "HQLevel",
    "Power",
    "ArenaPower",
    "Kills",
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
    "arena": "ArenaPower",
    "arenapower": "ArenaPower",
    "ap": "ArenaPower",
    "kills": "Kills",
    "kill": "Kills",
    "k": "Kills",
}

# Screenshot dataset kinds. Leaderboards are ranked "name + value" lists;
# member cards are the grid with an HQ level and a power figure per player.
LEADERBOARD_DATASETS = {
    "versus": "VersusPoints",
    "tech": "TechContribution",
    "power": "Power",
    "kills": "Kills",
}
# Member-card kind -> metric for the card's power figure. "general" also keeps
# HQLevel; "arena" cards show the same HQ level, so only ArenaPower is stored.
MEMBER_CARD_DATASETS = {
    "general": "Power",
    "arena": "ArenaPower",
}
DATASET_KINDS = (*LEADERBOARD_DATASETS, *MEMBER_CARD_DATASETS)


def resolve_metric(name: str) -> str:
    """Map a user-facing metric name to a canonical MetricType."""
    key = name.strip().lower().replace(" ", "").replace("_", "")
    if key in METRIC_ALIASES:
        return METRIC_ALIASES[key]
    for metric in METRIC_TYPES:
        if metric.lower() == key:
            return metric
    raise ValueError(
        f"Unknown metric '{name}'. Valid: versus, tech, hq, power, arena, kills"
    )
