"""OCR extraction and parsing for game screenshots."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from bot.config import LEADERBOARD_DATASETS, MEMBER_CARD_DATASETS
from bot.utils.parsing import parse_numeric_value

if TYPE_CHECKING:
    import numpy as np

log = logging.getLogger(__name__)

# Keep in sync with bot.config.DATASET_KINDS.
DatasetKind = Literal["versus", "tech", "general", "power", "arena", "kills"]

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
# Large comma-formatted scores (or 6+ plain digits) glued onto a name
TRAILING_MERGED_SCORE_RE = re.compile(
    r"^(?P<prefix>.*?)(?P<score>\d{1,3}(?:,\d{3})+|\d{6,})\s*[=:;]*$"
)
COMMA_SCORE_RE = re.compile(r"^\d{1,3}(?:,\d{3})+$")
# Digits with optional commas / OCR junk around a leaderboard value
SCOREISH_RE = re.compile(r"^[\d,]+$")
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
    """Lazy-loading OCR backend (EasyOCR, Tesseract, or vision/oMLX)."""

    def __init__(
        self,
        engine: str = "easyocr",
        *,
        vision_base_url: str = "http://127.0.0.1:8000/v1",
        vision_model: str = "Qwen2.5-VL-7B-Instruct",
        vision_api_key: str = "",
        vision_timeout: float = 120.0,
    ) -> None:
        name = (engine or "easyocr").strip().lower()
        if name == "omlx":
            name = "vision"
        self.engine_name = name
        self.vision_base_url = vision_base_url
        self.vision_model = vision_model
        self.vision_api_key = vision_api_key
        self.vision_timeout = vision_timeout
        self._easyocr_reader = None

    @property
    def is_vision(self) -> bool:
        return self.engine_name == "vision"

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
        if self.is_vision:
            raise RuntimeError(
                "Vision OCR does not produce token layouts; use extract_metrics_from_image"
            )

        import cv2

        path = Path(image_path)
        image = cv2.imread(str(path))
        if image is None:
            raise ValueError(f"Could not read image: {path}")

        if self.engine_name == "tesseract":
            return self._read_tesseract(image)
        return self._read_easyocr(image)

    def _read_easyocr(self, image: "np.ndarray") -> list[tuple[str, float, tuple]]:
        reader = self._get_easyocr()
        processed = _preprocess_for_ocr(image)
        # Lower width_ths keeps player names from merging into nearby large scores
        results = reader.readtext(processed, width_ths=0.4, height_ths=0.5)
        items: list[tuple[str, float, tuple]] = []
        for bbox, text, conf in results:
            xs = [p[0] for p in bbox]
            ys = [p[1] for p in bbox]
            cx, cy = (sum(xs) / 4, sum(ys) / 4)
            items.append((text.strip(), float(conf), (cx, cy, min(ys), max(ys))))
        items.sort(key=lambda t: (round(t[2][1] / 12), t[2][0]))
        return items

    def _read_tesseract(self, image: "np.ndarray") -> list[tuple[str, float, tuple]]:
        import cv2
        import pytesseract
        from pytesseract import Output

        processed = _preprocess_for_ocr(image)
        gray = cv2.cvtColor(processed, cv2.COLOR_BGR2GRAY)
        # Light adaptive threshold helps stylized leaderboard digits
        binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
        data = pytesseract.image_to_data(binary, output_type=Output.DICT)
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


def _preprocess_for_ocr(image: "np.ndarray") -> "np.ndarray":
    """
    Upscale small boards for digit/comma separation.

    Avoid aggressive CLAHE: it often glues names into scores on outlined fonts.
    Prefer ~2x over extreme upscales — 3x+ tends to glue name+score again.
    """
    import cv2

    out = image
    h, w = out.shape[:2]
    if w < 900:
        scale = min(2.0, 1200 / w)
        out = cv2.resize(out, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    elif w < 1200:
        scale = 1200 / w
        out = cv2.resize(out, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return out


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
    cleaned = token.replace(" ", "")
    if POWER_VALUE_RE.match(cleaned) and not re.search(r"[A-Za-zÀ-ÿ]", cleaned):
        return False
    # Names usually have at least one letter
    if not re.search(r"[A-Za-zÀ-ÿ]", token):
        return False
    # Reject pure rank badges like R5 when alone
    if re.fullmatch(r"R\d+", token, re.IGNORECASE):
        return False
    # Reject tiny OCR fragments (medal debris, etc.)
    if len(cleaned) < 3 and not re.search(r"[A-Za-zÀ-ÿ]{2,}", cleaned):
        return False
    if re.fullmatch(r"\d+[oOa-z]?", cleaned) and len(cleaned) <= 3:
        return False
    return True


def _clean_player_name(name: str) -> str:
    """Strip alliance tags and OCR junk from a player name."""
    name = ALLIANCE_TAG_RE.sub("", name).strip()
    name = re.sub(r"[=:;]+", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r"[^A-Za-z0-9_\-.'À-ÿ]+$", "", name)
    name = re.sub(r"^[^A-Za-z0-9_\-.'À-ÿ]+", "", name)
    return name.strip()


def _insert_commas_from_right(digits: str) -> str:
    """Format a plain digit string with thousand separators."""
    if not digits:
        return digits
    groups: list[str] = []
    while len(digits) > 3:
        groups.append(digits[-3:])
        digits = digits[:-3]
    groups.append(digits)
    return ",".join(reversed(groups))


def _recover_false_five_commas(digits: str) -> str | None:
    """
    Recover scores where OCR read commas as the digit ``5``.

    Examples:
      ``1275560`` -> ``127,560``
      ``3056585131`` -> ``30,658,131``
      ``1675040`` -> ``167,040``
    """
    if not digits.isdigit() or len(digits) < 5:
        return None

    groups: list[str] = []
    i = len(digits)
    consumed_five = False
    while i > 0:
        # Leading remnant (1–3 digits) after peeling thousand-groups
        if i <= 3:
            if i == 0:
                break
            groups.append(digits[:i])
            break
        groups.append(digits[i - 3 : i])
        i -= 3
        if i == 0:
            break
        # Thousand-separator misread as ``5``
        if digits[i - 1] == "5":
            i -= 1
            consumed_five = True
    if not consumed_five:
        return None
    groups.reverse()
    if not (1 <= len(groups[0]) <= 3):
        return None
    if any(len(g) != 3 for g in groups[1:]):
        return None
    return ",".join(groups)


def _drop_junk_separator_digit(chunk: str) -> str | None:
    """If a 4-digit chunk has a ``5``/``9`` junk separator, drop one to make 3 digits."""
    if len(chunk) != 4:
        return None
    # Prefer trailing junk (comma misread between groups when peeling from the right)
    for i in (3, 0, 1, 2):
        if chunk[i] in "59":
            return chunk[:i] + chunk[i + 1 :]
    return None


def _recover_len7_junk_separator(digits: str) -> str | None:
    """
    7-digit strings are often a 6-digit score (``XXX,YYY``) plus one junk separator.

    e.g. ``1673040`` (comma→``3``) -> ``167,040``; ``1675040`` (comma→``5``) -> ``167,040``.
    """
    if not digits.isdigit() or len(digits) != 7:
        return None
    junk = set("03589")
    # Try removing a junk-looking digit near the thousand boundary
    for i in (3, 2, 4, 1):
        if digits[i] not in junk:
            continue
        trial = digits[:i] + digits[i + 1 :]
        if len(trial) == 6:
            return _insert_commas_from_right(trial)
    return None


def _fix_malformed_comma_score(token: str) -> str | None:
    """
    Fix scores that have commas but wrong grouping.

    Examples:
      ``112,2579938`` -> ``112,257,938``
      ``56,7055138``  -> ``56,705,138``
      ``335644,447``  -> ``33,644,447``
      ``35644,447``   -> ``35,644,447``
    """
    if "," not in token or not SCOREISH_RE.match(token):
        return None
    if COMMA_SCORE_RE.match(token):
        return token

    digits = token.replace(",", "")
    if len(digits) < 4:
        return None

    # Preserve a well-formed left prefix; repair an overlong final segment.
    # e.g. ``112,2579938`` (comma misread as ``9``) -> ``112,257,938``
    m = re.fullmatch(r"(\d{1,3}(?:,\d{3})*),(\d{4,})", token)
    if m:
        prefix, tail = m.group(1), m.group(2)
        groups: list[str] = []
        # Peel complete extra thousand-groups; leave a 4–5 digit remnant for junk drop
        while len(tail) >= 6:
            groups.append(tail[-3:])
            tail = tail[:-3]
        if len(tail) == 4:
            dropped = _drop_junk_separator_digit(tail)
            if dropped is not None:
                tail = dropped
        elif len(tail) == 5:
            # Two junk-ish digits possible; try dropping one 5/9
            for i, ch in enumerate(tail):
                if ch in "59":
                    trial = tail[:i] + tail[i + 1 :]
                    if len(trial) == 4:
                        dropped = _drop_junk_separator_digit(trial)
                        tail = dropped if dropped is not None else trial
                        break
                    if len(trial) == 3:
                        tail = trial
                        break
        if 1 <= len(tail) <= 3:
            groups.append(tail)
            groups.reverse()
            if prefix:
                return f"{prefix},{','.join(groups)}"
            return ",".join(groups)

    # Leading chunk too long (e.g. ``335644,447`` / ``35644,447``).
    # Regroup digits; drop-junk / shrink variants are added by callers.
    return _insert_commas_from_right(digits)


def _shrink_repeated_runs(digits: str) -> list[str]:
    """
    OCR on outlined digits sometimes duplicates strokes (``4444`` vs ``44``).

    Return variants with each run of 3+ identical digits shortened by one.
    """
    if not digits.isdigit():
        return []
    out: list[str] = []
    for m in re.finditer(r"(\d)\1{2,}", digits):
        start, end = m.span()
        trial = digits[:start] + digits[start : end - 1] + digits[end:]
        if trial and trial not in out:
            out.append(trial)
    return out


def _score_string_candidates(token: str) -> list[str]:
    """
    Produce plausible comma-formatted interpretations of a score token.

    Order matters: earlier candidates are preferred by ``_pick_best_score``.
    """
    cleaned = token.replace(" ", "").strip("=;:'\"")
    cleaned = re.sub(r"[^\d,]", "", cleaned)
    if not cleaned or not re.search(r"\d", cleaned):
        return []

    candidates: list[str] = []

    def add(s: str | None) -> None:
        if s and s not in candidates:
            candidates.append(s)

    def add_from_digits(d: str) -> None:
        add(_recover_false_five_commas(d))
        add(_recover_len7_junk_separator(d))
        if len(d) >= 4:
            add(_insert_commas_from_right(d))

    # 1. Already well-formed
    add(cleaned if COMMA_SCORE_RE.match(cleaned) else None)
    # 2. Repair malformed comma placement / glued tails
    add(_fix_malformed_comma_score(cleaned))

    digits = cleaned.replace(",", "")
    if digits.isdigit():
        # 3. Primary recoveries on the raw digit string
        add(_recover_false_five_commas(digits))
        add(_recover_len7_junk_separator(digits))
        # Prefer shrink-then-format before raw regroup (outlined digit duplication)
        for shrunk in _shrink_repeated_runs(digits):
            add(_recover_false_five_commas(shrunk))
            add(_recover_len7_junk_separator(shrunk))
            for i, ch in enumerate(shrunk):
                if ch not in "59":
                    continue
                trial = shrunk[:i] + shrunk[i + 1 :]
                if len(trial) >= 4:
                    add_from_digits(trial)
            add(_insert_commas_from_right(shrunk))
        # 4. Drop a single junk 5/9 (false comma), shrink duplicates, then recover
        for i, ch in enumerate(digits):
            if ch not in "59":
                continue
            trial = digits[:i] + digits[i + 1 :]
            if len(trial) < 4:
                continue
            for shrunk in _shrink_repeated_runs(trial):
                add_from_digits(shrunk)
            add_from_digits(trial)
        # 5. Plain regroup last
        if len(digits) >= 4:
            add(_insert_commas_from_right(digits))
        elif digits:
            add(digits)

    return candidates


def _pick_best_score(candidates: list[str]) -> tuple[float, str] | None:
    """Prefer earlier well-formed candidates (see ``_score_string_candidates`` order)."""
    fallback: tuple[float, str] | None = None
    for raw in candidates:
        value = _parse_score_value(raw)
        if value is None:
            continue
        if COMMA_SCORE_RE.match(raw):
            return value, raw
        if fallback is None:
            fallback = (value, raw)
    return fallback


def _split_merged_name_score(token: str) -> tuple[str | None, str | None]:
    """
    Split tokens like ``EnemyHelicopter112,257,938`` or ``EnemyHlelicopiej12,257=``.

    Returns (name, score) when a letter-bearing prefix is glued to a large score.
    Names that merely end in a few digits (e.g. ``zena75``) are left alone.
    """
    cleaned = token.strip().replace(" ", "")
    match = TRAILING_MERGED_SCORE_RE.match(cleaned)
    if not match:
        return None, None
    prefix = match.group("prefix")
    score = match.group("score")
    if not prefix or not re.search(r"[A-Za-zÀ-ÿ]", prefix):
        return None, None

    # OCR often turns a leading ``1`` into ``i``/``j``/``l`` and glues it to the name,
    # e.g. Helicopter + 112,257 -> ``...pj12,257``. Recover when the score looks
    # like it is missing a hundreds-of-millions digit (exactly ``DD,DDD``).
    if re.search(r"[ijlI]$", prefix) and re.fullmatch(r"\d{2},\d{3}", score):
        prefix = prefix[:-1]
        score = "1" + score

    # Also handle ``...j12,257,938`` where the leading 1 became j but the rest
    # of a full 3-group score is present.
    if re.search(r"[ijlI]$", prefix) and re.fullmatch(r"\d{2}(?:,\d{3}){2}", score):
        prefix = prefix[:-1]
        score = "1" + score

    # Normalize score formatting (false commas etc.)
    best = _pick_best_score(_score_string_candidates(score))
    if best:
        score = best[1]

    name = _clean_player_name(prefix)
    if not name or len(name) < 2:
        return None, None
    return name, score


def _normalize_score_token(token: str) -> str | None:
    """Return a cleaned numeric token, or None if it is not score-like."""
    candidate = token.replace(" ", "").strip("=;:")
    if not candidate:
        return None

    # Letter-bearing tokens are handled via merged-name split, not as pure scores
    if re.search(r"[A-Za-zÀ-ÿ]", candidate):
        return None

    digits_only = re.sub(r"[^\d]", "", candidate)
    # 4–5 digit plain runs are often trailing score fragments (``73938``) or
    # ranks — keep them raw so join logic can append the last 3 digits.
    if candidate.isdigit() and 4 <= len(digits_only) <= 5:
        return candidate

    best = _pick_best_score(_score_string_candidates(candidate))
    if best:
        return best[1]

    if POWER_VALUE_RE.match(candidate) or PLAIN_NUMBER_RE.match(candidate):
        return candidate
    return None


def _try_join_score_parts(left: str, right: str) -> str | None:
    """
    Reassemble a comma score split across tokens.

    e.g. ``112,257`` + ``938`` -> ``112,257,938``
    OCR may prepend junk digits to the last group (``73938`` / ``25938`` -> ``938``).
    """
    left_candidates = _score_string_candidates(left) or [left.replace(" ", "").strip("=;:")]
    right_digits = re.sub(r"[^\d]", "", right)
    if not right_digits:
        return None

    for left_clean in left_candidates:
        if not COMMA_SCORE_RE.match(left_clean):
            continue
        # Already a complete 3-group (or more) score — don't append
        if left_clean.count(",") >= 2:
            continue
        if len(right_digits) == 3:
            return f"{left_clean},{right_digits}"
        if len(right_digits) >= 3:
            return f"{left_clean},{right_digits[-3:]}"
    return None


def _parse_score_value(raw: str) -> float | None:
    try:
        return parse_numeric_value(raw)
    except ValueError:
        return None


def _adaptive_y_tol(items: list[tuple[str, float, tuple]]) -> float:
    """
    Choose a Y tolerance that keeps name + score + alliance tag in one row.

    Alliance tags sit below names (~40–80px after upscale); a fixed 28px tol
    splits those boards into orphan name/score rows (zero metrics).
    """
    if len(items) < 2:
        return 48.0
    ys = sorted(t[2][1] for t in items)
    gaps = [ys[i + 1] - ys[i] for i in range(len(ys) - 1) if ys[i + 1] - ys[i] > 0.5]
    heights = [max(8.0, float(t[2][3]) - float(t[2][2])) for t in items]
    heights_sorted = sorted(heights)
    median_h = heights_sorted[len(heights_sorted) // 2]

    # Height-based floor: tokens on the same row usually overlap in Y within ~1.5x height.
    # This keeps clean versus rows (same-baseline name+score) from merging across ranks
    # when gap statistics alone are sparse.
    height_tol = max(40.0, median_h * 1.85)

    if not gaps:
        return height_tol

    gaps_sorted = sorted(gaps)
    median_gap = gaps_sorted[len(gaps_sorted) // 2]
    max_gap = gaps_sorted[-1]

    if max_gap >= 2.5 * max(median_gap, 1.0):
        # Clear split between intra-row and inter-row gaps (alliance boards)
        gap_tol = max(height_tol, median_gap * 2.2)
        gap_tol = min(gap_tol, max_gap * 0.55)
    else:
        # Gaps look similar (often only inter-row gaps survived filtering) — trust height
        gap_tol = height_tol

    return max(height_tol, gap_tol) if max_gap >= 2.5 * max(median_gap, 1.0) else height_tol


def _cluster_rows(
    items: list[tuple[str, float, tuple]],
    y_tol: float | None = None,
) -> list[list[tuple[str, float, tuple]]]:
    """Group OCR tokens into horizontal rows by Y position."""
    if not items:
        return []
    sorted_items = sorted(items, key=lambda t: t[2][1])
    if y_tol is None:
        y_tol = _adaptive_y_tol(sorted_items)

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


def _leaderboard_score_candidates(
    texts: list[str],
) -> list[tuple[float, str, int, str | None]]:
    """
    Collect (value, raw_score, token_index, split_name) candidates for a row.

    Prefers large / comma-formatted scores. Merged name+score tokens contribute
    both a score and a cleaned player name.
    """
    candidates: list[tuple[float, str, int, str | None]] = []

    normalized: list[tuple[int, str]] = []
    merged_names: dict[int, str] = {}

    for i, text in enumerate(texts):
        pure = _normalize_score_token(text)
        if pure is not None:
            normalized.append((i, pure))
            continue
        name, score = _split_merged_name_score(text)
        if name and score:
            # Re-normalize the split score (false-5 / glued tails)
            best = _pick_best_score(_score_string_candidates(score))
            normalized.append((i, best[1] if best else score))
            merged_names[i] = name

    # Also try joining adjacent score fragments (split commas / OCR leftovers)
    joined: list[tuple[int, str]] = []
    skip: set[int] = set()
    for idx, (i, score) in enumerate(normalized):
        if i in skip:
            continue
        if idx + 1 < len(normalized):
            j, right = normalized[idx + 1]
            combo = _try_join_score_parts(score, right)
            if combo is not None:
                joined.append((i, combo))
                skip.add(j)
                continue
            # Also try joining using the raw right token text (may have junk)
            if j < len(texts):
                combo = _try_join_score_parts(score, texts[j])
                if combo is not None:
                    joined.append((i, combo))
                    skip.add(j)
                    continue
        joined.append((i, score))

    for i, score in joined:
        best = _pick_best_score(_score_string_candidates(score))
        if best:
            value, score = best
        else:
            value = _parse_score_value(score)
            if value is None:
                continue
        if value < 10 and i == 0:
            continue  # likely rank badge
        candidates.append((value, score, i, merged_names.get(i)))

    return candidates


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

        candidates = _leaderboard_score_candidates(texts)
        if not candidates:
            continue

        # Prefer the largest plausible score; break ties with rightmost token.
        # Leaderboard values are typically thousands+ (often millions).
        def _rank(c: tuple[float, str, int, str | None]) -> tuple:
            value, raw, idx, _name = c
            comma_groups = raw.count(",")
            well = 1 if COMMA_SCORE_RE.match(raw) else 0
            return (well, comma_groups, value, idx)

        value, _raw, value_idx, merged_name = max(candidates, key=_rank)

        if merged_name:
            player = merged_name
        else:
            name_parts: list[str] = []
            for t in texts[:value_idx]:
                if ALLIANCE_TAG_RE.match(t) or _is_noise(t):
                    continue
                split_name, _split_score = _split_merged_name_score(t)
                if split_name:
                    name_parts.append(split_name)
                    continue
                if _looks_like_name(t):
                    name_parts.append(_clean_player_name(t))
            # Drop leading rank digits
            while name_parts and re.fullmatch(r"\d+", name_parts[0]):
                name_parts = name_parts[1:]
            player = " ".join(p for p in name_parts if p).strip()

        player = _clean_player_name(player)
        if not player or len(player) < 2:
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
    # General cards stack name then stats with a larger gap than leaderboard
    # alliance tags; use a tighter tol so "65.4M" is not treated as a name.
    heights = [max(8.0, float(t[2][3]) - float(t[2][2])) for t in items] if items else [16.0]
    median_h = sorted(heights)[len(heights) // 2]
    rows = _cluster_rows(items, y_tol=max(22.0, median_h * 0.95))

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
    engine: OCREngine | None = None,
) -> OCRResult:
    """
    Run OCR and parse into WeeklyMetrics-ready rows.

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
    engine = engine or OCREngine("easyocr")

    if engine.is_vision:
        # Lazy import avoids circular dependency with bot.ocr.vision
        from bot.ocr.vision import extract_metrics_via_vision

        log.info(
            "Using vision OCR (no EasyOCR fallback) model=%s",
            engine.vision_model,
        )
        try:
            return extract_metrics_via_vision(
                image_path,
                kind=kind,
                base_url=engine.vision_base_url,
                model=engine.vision_model,
                api_key=engine.vision_api_key,
                timeout=engine.vision_timeout,
            )
        except Exception as exc:
            log.exception(
                "Vision OCR failed (refusing EasyOCR fallback): %s",
                exc,
            )
            raise

    log.info("Using classical OCR engine=%s", engine.engine_name)
    items = engine.read_text(image_path)
    raw_text = "\n".join(t[0] for t in items)
    warnings: list[str] = []

    log.info("OCR kind=%s tokens=%d", kind, len(items))

    metrics: list[ExtractedMetric] = []
    if kind in MEMBER_CARD_DATASETS:
        metrics = relabel_member_cards(parse_general_rows(items), kind)
    else:
        metrics = parse_leaderboard_rows(items, LEADERBOARD_DATASETS[kind])

    # Deduplicate by (player, metric), keep last
    dedup: dict[tuple[str, str], ExtractedMetric] = {}
    for m in metrics:
        dedup[(m.player_name.lower(), m.metric_type)] = m
    metrics = list(dedup.values())

    if not metrics:
        warnings.append(
            "No player metrics parsed. Try a tighter crop or check the dataset type."
        )

    return OCRResult(kind=kind, metrics=metrics, raw_text=raw_text, warnings=warnings)
