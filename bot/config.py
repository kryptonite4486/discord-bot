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


def _int_or(name: str, default: int) -> int:
    """A non-negative int; unset means ``default`` (0 is kept: it turns a limit off)."""
    value = _optional_int(name)
    return default if value is None else max(0, value)


def _int_set(name: str) -> frozenset[int]:
    """Comma- or space-separated Discord IDs."""
    raw = os.getenv(name, "").replace(",", " ").split()
    return frozenset(int(part) for part in raw)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _check_ocr_engine() -> None:
    """OCR is vision-only; reject stale OCR_ENGINE values instead of ignoring them."""
    raw = os.getenv("OCR_ENGINE", "").strip().lower()
    if raw and raw not in {"vision", "omlx"}:
        raise RuntimeError(
            f"OCR_ENGINE={raw!r} is no longer supported; OCR always uses the vision "
            "model (OCR_VISION_*). Remove OCR_ENGINE from .env or set it to 'vision'."
        )


def load_vision_env() -> tuple[str, str, str, float]:
    """(base_url, model, api_key, timeout) from OCR_VISION_* variables."""
    timeout_raw = os.getenv("OCR_VISION_TIMEOUT", "120").strip() or "120"
    try:
        timeout = float(timeout_raw)
    except ValueError:
        timeout = 120.0
    base_url = (
        os.getenv("OCR_VISION_BASE_URL", "http://127.0.0.1:8000/v1").strip()
        or "http://127.0.0.1:8000/v1"
    )
    model = (
        os.getenv("OCR_VISION_MODEL", "Qwen2.5-VL-7B-Instruct-4bit").strip()
        or "Qwen2.5-VL-7B-Instruct-4bit"
    )
    api_key = os.getenv("OCR_VISION_API_KEY", "").strip()
    return base_url, model, api_key, timeout


def _heartbeat_url() -> str | None:
    raw = os.getenv("HEARTBEAT_URL", "").strip()
    if raw and not raw.startswith(("https://", "http://")):
        raise RuntimeError("HEARTBEAT_URL must start with https:// (or leave it empty)")
    return raw or None


DEFAULT_PLANNER_URL = "https://lastz-territory-planner.pages.dev"


@dataclass(frozen=True)
class Settings:
    discord_token: str
    database_path: Path
    ocr_vision_base_url: str
    ocr_vision_model: str
    ocr_vision_api_key: str
    ocr_vision_timeout: float
    ocr_max_concurrency: int
    default_week_start: str | None
    log_level: str
    dev_guild_id: int | None
    control_guild_id: int | None
    bot_owner_ids: frozenset[int]
    legacy_guild_id: str | None
    app_id: int | None
    backup_dir: Path | None
    backup_keep: int
    backup_max_age_days: int
    data_retention_days: int
    tiers_enforced: bool
    allow_unmounted_data: bool
    planner_url: str = DEFAULT_PLANNER_URL
    # Per-user OCR ingest limit, per server (bot/utils/abuse.py). 0 turns a cap off.
    ingest_rate_window_minutes: int = 10
    ingest_rate_requests: int = 6
    ingest_rate_images: int = 60
    ingest_rate_admin_multiplier: int = 3
    # Free servers one person (owner or OCR user) can use OCR in. 0: no cap.
    free_servers_per_owner: int = 3
    # None: the question bank bundled with the bot.
    trivia_questions_path: Path | None = None
    # Dead-man's-switch URL (healthchecks.io, Better Stack) pinged while healthy.
    heartbeat_url: str | None = None
    # Second folder that gets a copy of each daily backup (iCloud, Dropbox, NAS).
    offsite_backup_dir: Path | None = None

    @classmethod
    def from_env(cls) -> Settings:
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token or token == "your_bot_token_here":
            raise RuntimeError(
                "DISCORD_TOKEN is required. Copy .env.example to .env and set your bot token."
            )

        db_path = Path(os.getenv("DATABASE_PATH", "/app/data/weekly.db"))
        default_week = os.getenv("DEFAULT_WEEK_START", "").strip() or None
        _check_ocr_engine()
        vision_base, vision_model, vision_api_key, vision_timeout = load_vision_env()

        return cls(
            discord_token=token,
            database_path=db_path,
            ocr_vision_base_url=vision_base,
            ocr_vision_model=vision_model,
            ocr_vision_api_key=vision_api_key,
            ocr_vision_timeout=vision_timeout,
            # oMLX garbles this vision model's replies when it batches several
            # requests together, so OCR runs one image at a time by default.
            ocr_max_concurrency=max(1, _optional_int("OCR_MAX_CONCURRENCY") or 1),
            default_week_start=default_week,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            dev_guild_id=_optional_int("DEV_GUILD_ID"),
            control_guild_id=_optional_int("CONTROL_GUILD_ID"),
            bot_owner_ids=_int_set("BOT_OWNER_IDS"),
            legacy_guild_id=(
                os.getenv("LEGACY_GUILD_ID", "").strip() or None
            ),
            app_id=_optional_int("DISCORD_APP_ID"),
            backup_dir=(
                Path(os.environ["BACKUP_DIR"])
                if os.getenv("BACKUP_DIR", "").strip()
                else None
            ),
            backup_keep=max(1, _optional_int("BACKUP_KEEP") or 14),
            # Oldest a backup of any kind may get (the newest one is always kept).
            backup_max_age_days=max(1, _optional_int("BACKUP_MAX_AGE_DAYS") or 30),
            # Days a removed server's data is kept in case the bot is re-added.
            data_retention_days=max(1, _optional_int("DATA_RETENTION_DAYS") or 30),
            # Off: tier limits are only logged, nothing is blocked.
            tiers_enforced=_env_bool("TIERS_ENFORCED", False),
            allow_unmounted_data=_env_bool("ALLOW_UNMOUNTED_DATA", False),
            planner_url=(
                os.getenv("PLANNER_URL", "").strip().rstrip("/") or DEFAULT_PLANNER_URL
            ),
            ingest_rate_window_minutes=max(1, _optional_int("INGEST_RATE_WINDOW_MINUTES") or 10),
            ingest_rate_requests=_int_or("INGEST_RATE_REQUESTS", 6),
            ingest_rate_images=_int_or("INGEST_RATE_IMAGES", 60),
            ingest_rate_admin_multiplier=max(1, _optional_int("INGEST_RATE_ADMIN_MULTIPLIER") or 3),
            free_servers_per_owner=_int_or("FREE_SERVERS_PER_OWNER", 3),
            trivia_questions_path=(
                Path(os.environ["TRIVIA_QUESTIONS_PATH"])
                if os.getenv("TRIVIA_QUESTIONS_PATH", "").strip()
                else None
            ),
            heartbeat_url=_heartbeat_url(),
            offsite_backup_dir=(
                Path(os.environ["OFFSITE_BACKUP_DIR"].strip())
                if os.getenv("OFFSITE_BACKUP_DIR", "").strip()
                else None
            ),
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
