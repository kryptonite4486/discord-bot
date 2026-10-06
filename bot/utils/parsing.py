"""Shared parsing helpers for weeks, values, and pasted CSV/text."""

from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, timedelta
from typing import Iterable

WEEK_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
POWER_SUFFIX_RE = re.compile(
    r"^\s*([\d.,]+)\s*([KkMmBb])?\s*$"
)
NUMBER_IN_TEXT_RE = re.compile(r"[\d,.]+")


def sunday_of(d: date | None = None) -> date:
    """Return the Sunday (week start) for the given date."""
    d = d or date.today()
    return d - timedelta(days=(d.weekday() + 1) % 7)


def parse_week_start(value: str | None) -> str:
    """
    Parse a week identifier into a Sunday week-start date string.

    Accepts:
      - YYYY-MM-DD (normalized to that week's Sunday)
      - 'current' / 'this' / empty -> current week's Sunday
      - 'last' -> previous week's Sunday
    """
    if value is None or not str(value).strip():
        return sunday_of().isoformat()

    raw = str(value).strip().lower()
    if raw in {"current", "this", "now", "today"}:
        return sunday_of().isoformat()
    if raw in {"last", "prev", "previous"}:
        return sunday_of(date.today() - timedelta(days=7)).isoformat()

    if not WEEK_RE.match(raw):
        raise ValueError(
            f"Invalid week '{value}'. Use YYYY-MM-DD, 'current', or 'last'."
        )

    parsed = datetime.strptime(raw, "%Y-%m-%d").date()
    return sunday_of(parsed).isoformat()


def parse_numeric_value(raw: str | int | float) -> float:
    """
    Parse a metric value from OCR/manual input.

    Supports: 167040, 167,040, 65.4M, 1.2K, 2.5B
    """
    if isinstance(raw, (int, float)):
        return float(raw)

    text = str(raw).strip().replace(" ", "")
    if not text:
        raise ValueError("Empty numeric value")

    match = POWER_SUFFIX_RE.match(text)
    if not match:
        # Fallback: strip non-numeric except . and ,
        cleaned = re.sub(r"[^\d.,\-]", "", text)
        if not cleaned:
            raise ValueError(f"Cannot parse numeric value from '{raw}'")
        text = cleaned
        match = POWER_SUFFIX_RE.match(text)
        if not match:
            raise ValueError(f"Cannot parse numeric value from '{raw}'")

    number_part, suffix = match.group(1), match.group(2)
    # Prefer European-style only if comma is decimal (single comma, no dot)
    if "," in number_part and "." in number_part:
        number_part = number_part.replace(",", "")
    elif "," in number_part and number_part.count(",") == 1 and "." not in number_part:
        # Ambiguous: treat as thousands separator if 3 digits after comma
        left, right = number_part.split(",")
        if len(right) == 3:
            number_part = left + right
        else:
            number_part = left + "." + right
    else:
        number_part = number_part.replace(",", "")

    value = float(number_part)
    if suffix:
        mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[suffix.lower()]
        # round() drops float noise such as 8.3M -> 8300000.000000001
        value = round(value * mult, 6)
    return value


def format_value(metric_type: str, value: float) -> str:
    """Human-friendly formatting for display (signed K/M/B where applicable)."""
    if metric_type == "HQLevel":
        return str(int(round(value)))

    # Use magnitude for thresholds so negative deltas stay abbreviated (-2.3M),
    # matching positive shorthand (2.3M) in growth/leaderboard reports.
    if metric_type in {
        "Power",
        "VersusPoints",
        "TechContribution",
        "ArenaPower",
        "Kills",
    }:
        sign = "-" if value < 0 else ""
        mag = abs(float(value))
        if mag >= 1_000_000_000:
            return f"{sign}{mag / 1_000_000_000:.1f}B"
        if mag >= 1_000_000:
            return f"{sign}{mag / 1_000_000:.1f}M"
        if mag >= 1_000:
            return f"{sign}{mag / 1_000:.1f}K"
        return f"{sign}{mag:,.0f}"

    return f"{value:,.0f}"


def parse_pasted_rows(
    text: str,
    *,
    expected_columns: int | None = None,
) -> list[list[str]]:
    """
    Parse CSV or whitespace/tab-delimited pasted text into rows of cells.

    Skips blank lines and simple header rows.
    """
    text = text.strip()
    if not text:
        return []

    # Try CSV first when commas look like delimiters
    sample = text.splitlines()[0]
    if "," in sample and sample.count(",") >= 1:
        reader = csv.reader(io.StringIO(text))
        rows = [row for row in reader if any(cell.strip() for cell in row)]
    else:
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if "\t" in line:
                rows.append([c.strip() for c in line.split("\t")])
            else:
                # Name may contain spaces; split from the right for trailing numbers
                parts = line.split()
                rows.append(parts)

    # Drop obvious header rows
    filtered: list[list[str]] = []
    for row in rows:
        joined = " ".join(row).lower()
        if any(h in joined for h in ("player", "name", "points", "hq", "power", "metric")):
            # Only skip if no numeric-looking cells
            if not any(re.search(r"\d", cell) for cell in row):
                continue
        if expected_columns is not None and len(row) < expected_columns:
            continue
        filtered.append(row)
    return filtered


def chunk_message(content: str, limit: int = 1900) -> Iterable[str]:
    """Split long Discord messages on newlines when possible.

    A split inside a ``` code block closes the block at the end of one
    message and reopens it (same language tag) at the start of the next, so
    every message renders on its own.
    """
    if len(content) <= limit:
        yield content
        return

    close = "\n```"
    buf: list[str] = []
    size = 0
    fence: str | None = None  # opening fence line while inside a code block

    def flush() -> str:
        text = "".join(buf)
        if fence is not None:
            text = text.rstrip("\n") + close
        return text

    def room() -> int:
        # Leave space to close an open code block at the end of this message.
        return limit - (len(close) if fence is not None else 0)

    for line in content.splitlines(keepends=True):
        if size + len(line) > room() and buf:
            yield flush()
            buf = [fence] if fence is not None else []
            size = len(fence) if fence is not None else 0
        # Hard-split oversized single lines so we never exceed the limit
        while size + len(line) > room():
            take = max(1, room() - size)
            buf.append(line[:take])
            yield flush()
            buf = [fence] if fence is not None else []
            size = len(fence) if fence is not None else 0
            line = line[take:]
        buf.append(line)
        size += len(line)
        if line.lstrip().startswith("```"):
            fence = None if fence is not None else line.strip() + "\n"
    if buf:
        yield "".join(buf)


def chunk_fenced_md(content: str, limit: int = 1900, *, lang: str = "md") -> list[str]:
    """
    Split content into Discord messages, each a complete fenced code block.

    Inner backticks in the content are fine because each message opens and
    closes its own fence. Fence overhead is reserved in the chunk size.
    """
    open_fence = f"```{lang}\n"
    close_fence = "\n```"
    overhead = len(open_fence) + len(close_fence)
    inner_limit = max(200, limit - overhead)
    return [
        f"{open_fence}{chunk.rstrip()}{close_fence}"
        for chunk in chunk_message(content, limit=inner_limit)
    ]
