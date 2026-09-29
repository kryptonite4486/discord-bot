"""OCR extraction and parsing for game screenshots."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import cv2
import numpy as np

from bot.utils.parsing import parse_numeric_value

log = logging.getLogger(__name__)

DatasetKind = Literal["versus", "tech", "general", "power", "auto"]

# Noise tokens commonly read from UI chrome
NOISE_TOKENS = {
    "online",
    "offline",
    "ago",
    "min",
    "mins",
    "hour",
    "hours",
    "day",
    "days",
    "rank",
    "power",
    "hq",
    "vs",
    "points",
    "contribution",
    "alliance",
    "members",
    "r1",
    "r2",
    "r3",
    "r4",
    "r5",
}

ALLIANCE_TAG_RE = re.compile(r"^\[.+\]")
POWER_VALUE_RE = re.compile(r"^[\d.,]+\s*[KkMmBb]?$")
PLAIN_NUMBER_RE = re.compile(r"^[\d,]+(?:\.\d+)?$")
TIME_AGO_RE = re.compile(
    r"^\d+\s*(min|mins|m|h|hr|hrs|hour|hours|d|day|days)\s*(ago)?$",
    re.IGNORECASE,
)


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


class OCREngine:
    """Lazy-loading OCR backend (EasyOCR or Tesseract)."""

    def __init__(self, engine: str = "easyocr") -> None:
        self.engine_name = engine
        self._easyocr_reader = None

    def _get_easyocr(self):
        if self._easyocr_reader is None:
            import easyocr

            log.info("Initializing EasyOCR reader (first run may download models)...")
            self._easyocr_reader = easyocr.Reader(["en"], gpu=False)
        return self._easyocr_reader

    def read_text(self, image_path: Path | str) -> list[tuple[str, float, tuple]]:
        """
        Return list of (text, confidence, bbox_center) sorted top-to-bottom, left-to-right.
        """
        path = Path(image_path)
        image = cv2.imread(str(path))
        if image is None:
            raise ValueError(f"Could not read image: {path}")

        if self.engine_name == "tesseract":
            return self._read_tesseract(image)
        return self._read_easyocr(image)

    def _read_easyocr(self, image: np.ndarray) -> list[tuple[str, float, tuple]]:
        reader = self._get_easyocr()
        results = reader.readtext(image)
        items: list[tuple[str, float, tuple]] = []
        for bbox, text, conf in results:
            xs = [p[0] for p in bbox]
            ys = [p[1] for p in bbox]
            cx, cy = (sum(xs) / 4, sum(ys) / 4)
            items.append((text.strip(), float(conf), (cx, cy, min(ys), max(ys))))
        items.sort(key=lambda t: (round(t[2][1] / 12), t[2][0]))
        return items

    def _read_tesseract(self, image: np.ndarray) -> list[tuple[str, float, tuple]]:
        import pytesseract
        from pytesseract import Output

        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        # Light adaptive threshold helps stylized leaderboard digits
        processed = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
        data = pytesseract.image_to_data(processed, output_type=Output.DICT)
        items: list[tuple[str, float, tuple]] = []
        n = len(data["text"])
        for i in range(n):
            text = (data["text"][i] or "").strip()
            conf = float(data["conf"][i])
            if not text or conf < 0:
                continue
            x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            cx, cy = x + w / 2, y + h / 2
            items.append((text, conf / 100.0, (cx, cy, y, y + h)))
        items.sort(key=lambda t: (round(t[2][1] / 12), t[2][0]))
        return items


def _is_noise(token: str) -> bool:
    t = token.strip().lower()
    if not t:
        return True
    if t in NOISE_TOKENS:
        return True
    if TIME_AGO_RE.match(t):
        return True
    if ALLIANCE_TAG_RE.match(token):
        return True
    if t in {"♂", "♀", "z"}:
        return True
    return False


def _looks_like_name(token: str) -> bool:
    if _is_noise(token):
        return False
    if POWER_VALUE_RE.match(token) and not re.search(r"[A-Za-zÀ-ÿ]", token):
        return False
    # Names usually have at least one letter
    if not re.search(r"[A-Za-zÀ-ÿ]", token):
        return False
    # Reject pure rank badges like R5 when alone
    if re.fullmatch(r"R\d+", token, re.IGNORECASE):
        return False
    return True


def _cluster_rows(
    items: list[tuple[str, float, tuple]],
    y_tol: float = 28.0,
) -> list[list[tuple[str, float, tuple]]]:
    """Group OCR tokens into horizontal rows by Y position."""
    if not items:
        return []
    sorted_items = sorted(items, key=lambda t: t[2][1])
    rows: list[list[tuple[str, float, tuple]]] = []
    current: list[tuple[str, float, tuple]] = [sorted_items[0]]
    current_y = sorted_items[0][2][1]

    for item in sorted_items[1:]:
        y = item[2][1]
        if abs(y - current_y) <= y_tol:
            current.append(item)
            current_y = (current_y * (len(current) - 1) + y) / len(current)
        else:
            rows.append(sorted(current, key=lambda t: t[2][0]))
            current = [item]
            current_y = y
    rows.append(sorted(current, key=lambda t: t[2][0]))
    return rows


def parse_leaderboard_rows(
    items: list[tuple[str, float, tuple]],
    metric_type: str,
) -> list[ExtractedMetric]:
    """
    Parse versus/tech-style leaderboard rows: name + large number on the right.
    """
    metrics: list[ExtractedMetric] = []
    for row in _cluster_rows(items):
        texts = [t[0] for t in row if t[0]]
        if not texts:
            continue

        # Rightmost token that looks numeric is the score
        value_idx = None
        for i in range(len(texts) - 1, -1, -1):
            candidate = texts[i].replace(" ", "")
            if POWER_VALUE_RE.match(candidate) or PLAIN_NUMBER_RE.match(candidate):
                # Prefer larger comma-formatted scores over tiny rank numbers
                try:
                    val = parse_numeric_value(candidate)
                except ValueError:
                    continue
                if val < 10 and i == 0:
                    continue  # likely rank
                value_idx = i
                break
        if value_idx is None:
            continue

        name_parts = [
            t for t in texts[:value_idx]
            if _looks_like_name(t) and not ALLIANCE_TAG_RE.match(t)
        ]
        # Drop leading rank digits
        while name_parts and re.fullmatch(r"\d+", name_parts[0]):
            name_parts = name_parts[1:]

        if not name_parts:
            continue

        player = " ".join(name_parts).strip()
        # Prefer first name token if alliance glued somehow
        player = ALLIANCE_TAG_RE.sub("", player).strip()
        if not player or len(player) < 2:
            continue

        try:
            value = parse_numeric_value(texts[value_idx])
        except ValueError:
            continue

        metrics.append(ExtractedMetric(player, metric_type, value))
    return metrics


def parse_general_rows(
    items: list[tuple[str, float, tuple]],
) -> list[ExtractedMetric]:
    """
    Parse general data cards: PlayerName + HQ level + Power (often with M suffix).

    Handles both single-profile crops and multi-member grids.
    """
    metrics: list[ExtractedMetric] = []
    rows = _cluster_rows(items, y_tol=22.0)

    # Flatten nearby name / stats rows: name on one line, stats on next
    i = 0
    while i < len(rows):
        texts = [t[0] for t in rows[i] if t[0]]
        name_candidates = [t for t in texts if _looks_like_name(t)]
        power_candidates = [
            t for t in texts
            if re.search(r"[Mm]$", t.replace(" ", "")) or (
                POWER_VALUE_RE.match(t.replace(" ", "")) and "M" in t.upper()
            )
        ]
        hq_candidates = [
            t for t in texts
            if re.fullmatch(r"\d{1,2}", t.strip())
        ]

        # If this row is only a name, peek at next row for stats
        if name_candidates and not power_candidates and i + 1 < len(rows):
            next_texts = [t[0] for t in rows[i + 1] if t[0]]
            power_candidates = [
                t for t in next_texts
                if re.search(r"[KkMmBb]$", t.replace(" ", ""))
                or (POWER_VALUE_RE.match(t.replace(" ", "")) and re.search(r"[KkMmBb]", t))
            ]
            hq_candidates = [
                t for t in next_texts if re.fullmatch(r"\d{1,2}", t.strip())
            ]
            # Also absorb numeric power without suffix if clearly large
            if not power_candidates:
                power_candidates = [
                    t for t in next_texts
                    if POWER_VALUE_RE.match(t.replace(" ", ""))
                ]
            i += 1  # consume stats row

        if not name_candidates:
            i += 1
            continue

        # Prefer the leftmost name-like token that isn't a gender symbol
        player = name_candidates[0]
        for cand in name_candidates:
            if cand not in {"♂", "♀"} and not re.fullmatch(r"R\d+", cand, re.I):
                player = cand
                break

        # HQ: smallest 2-digit-ish number that isn't part of power
        hq_value = None
        for hq in hq_candidates:
            try:
                v = int(hq)
            except ValueError:
                continue
            if 1 <= v <= 50:
                hq_value = float(v)
                break

        power_value = None
        for p in power_candidates:
            try:
                v = parse_numeric_value(p)
            except ValueError:
                continue
            # Power is typically large (or has M/K suffix)
            if v >= 1000 or re.search(r"[KkMmBb]", p):
                power_value = v
                break

        # Single-profile fallback: scan all tokens in row group
        if power_value is None:
            for t in texts:
                if re.search(r"[Mm]$", t.replace(" ", "")):
                    try:
                        power_value = parse_numeric_value(t)
                        break
                    except ValueError:
                        pass

        if hq_value is not None:
            metrics.append(ExtractedMetric(player, "HQLevel", hq_value))
        if power_value is not None:
            metrics.append(ExtractedMetric(player, "Power", power_value))

        i += 1

    return metrics


def detect_kind(items: list[tuple[str, float, tuple]], raw_text: str) -> str:
    """Heuristic dataset detection from OCR tokens."""
    joined = raw_text.lower()
    has_power_m = any(re.search(r"\d+\.?\d*\s*[Mm]\b", t[0]) for t in items)
    has_alliance = "[" in raw_text and "]" in raw_text
    large_commas = len(re.findall(r"\d{1,3}(?:,\d{3})+", raw_text))

    if has_power_m or "hq" in joined:
        return "general"
    if has_alliance and large_commas >= 2:
        # Alliance power boards still map via caller metric; treat as leaderboard
        return "versus"
    if large_commas >= 2:
        return "versus"
    return "versus"


def extract_metrics_from_image(
    image_path: Path | str,
    *,
    kind: DatasetKind = "auto",
    engine: OCREngine | None = None,
    metric_type_override: str | None = None,
) -> OCRResult:
    """
    Run OCR and parse into WeeklyMetrics-ready rows.

    kind:
      - versus -> VersusPoints
      - tech -> TechContribution
      - general -> HQLevel + Power
      - power -> Power (leaderboard-style values)
      - auto -> detect
    """
    engine = engine or OCREngine("easyocr")
    items = engine.read_text(image_path)
    raw_text = "\n".join(t[0] for t in items)
    warnings: list[str] = []

    detected = kind if kind != "auto" else detect_kind(items, raw_text)
    log.info("OCR detected kind=%s tokens=%d", detected, len(items))

    metrics: list[ExtractedMetric] = []
    if detected == "general":
        metrics = parse_general_rows(items)
    else:
        metric_type = metric_type_override or {
            "tech": "TechContribution",
            "power": "Power",
            "versus": "VersusPoints",
        }.get(detected, "VersusPoints")
        metrics = parse_leaderboard_rows(items, metric_type)

    # Deduplicate by (player, metric), keep last
    dedup: dict[tuple[str, str], ExtractedMetric] = {}
    for m in metrics:
        dedup[(m.player_name.lower(), m.metric_type)] = m
    metrics = list(dedup.values())

    if not metrics:
        warnings.append(
            "No player metrics parsed. Try a tighter crop or set the dataset type explicitly."
        )

    return OCRResult(kind=detected, metrics=metrics, raw_text=raw_text, warnings=warnings)
