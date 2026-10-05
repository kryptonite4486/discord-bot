"""Vision-model OCR via OpenAI-compatible chat/completions (e.g. oMLX)."""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
import time
from pathlib import Path
from typing import Any

from bot.config import LEADERBOARD_DATASETS, MEMBER_CARD_DATASETS
from bot.ocr.pipeline import (
    DatasetKind,
    ExtractedMetric,
    OCRResult,
    relabel_member_cards,
)
from bot.utils.parsing import parse_numeric_value

log = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)

# A full screenshot's JSON is ~100-350 tokens. The cap stops runaway
# (repeating) generations in seconds; oMLX's keep-alive chunks mean the HTTP
# timeout alone never fires while a runaway is still generating.
MAX_TOKENS = 1024
# Pauses before each retry of a transient failure (connection dropped, server
# error, unreadable reply).
RETRY_DELAYS = (2.0, 5.0)


class VisionOCRError(RuntimeError):
    """Vision OCR failed in a way retrying won't fix (e.g. auth, unknown model)."""


class VisionTransientError(VisionOCRError):
    """Connection dropped, server error, or empty reply; worth retrying."""


class VisionReplyError(VisionTransientError):
    """The model replied with something that isn't usable JSON; worth retrying."""


def _mime_for(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    if guessed and guessed.startswith("image/"):
        return guessed
    suffix = path.suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
    }.get(suffix, "image/png")


def _image_data_url(path: Path) -> str:
    raw = path.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{_mime_for(path)};base64,{b64}"


def _prompt_for_kind(kind: DatasetKind) -> str:
    if kind in MEMBER_CARD_DATASETS:
        # Ask for the power text verbatim: models converting "8.3M" themselves
        # tend to drop the decimal (83000000). parse_numeric_value converts.
        return (
            "You are extracting structured data from a mobile game member-list screenshot.\n"
            "Read every visible player card.\n"
            "Return ONLY a JSON array (no markdown fences, no commentary) of objects with keys:\n"
            '  "player" (string name, strip alliance tags like [TAG] if present),\n'
            '  "hq" (integer HQ level),\n'
            '  "power" (the power figure copied exactly as displayed, as a string,\n'
            '           including any decimal point and K/M/B suffix, e.g. "8.3M").\n'
            'Example: [{"player":"Alice","hq":30,"power":"65.4M"}]\n'
            "If a field is unreadable, omit that object. Do not invent players."
        )
    metric_hint = {
        "versus": "versus / VS points",
        "tech": "tech contribution points",
        "power": "power values",
        "kills": "total kill counts",
    }[kind]
    return (
        "You are extracting structured data from a mobile game leaderboard screenshot.\n"
        f"Read every visible ranked player row and their {metric_hint}.\n"
        "Return ONLY a JSON array (no markdown fences, no commentary) of objects with keys:\n"
        '  "player" (string name, strip alliance tags like [TAG] if present),\n'
        '  "value" (numeric score; strip commas; accept K/M/B suffixes).\n'
        "Example: [{\"player\":\"Alice\",\"value\":167040}]\n"
        "If a row is unreadable, skip it. Do not invent players."
    )


def _strip_json_payload(text: str) -> str:
    text = text.strip()
    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()
    # Truncate to outermost JSON array/object if model added prose
    start_arr = text.find("[")
    start_obj = text.find("{")
    if start_arr >= 0 and (start_obj < 0 or start_arr < start_obj):
        end = text.rfind("]")
        if end > start_arr:
            return text[start_arr : end + 1]
    if start_obj >= 0:
        end = text.rfind("}")
        if end > start_obj:
            return text[start_obj : end + 1]
    return text


def _parse_json_rows(text: str) -> list[dict[str, Any]]:
    payload = _strip_json_payload(text)
    data = json.loads(payload)
    if isinstance(data, dict):
        for key in ("rows", "players", "data", "results", "items"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    if not isinstance(data, list):
        raise ValueError("Vision model response is not a JSON array")
    return [row for row in data if isinstance(row, dict)]


def _player_name(row: dict[str, Any]) -> str | None:
    for key in ("player", "player_name", "name", "NickName", "nickname"):
        val = row.get(key)
        if val is None:
            continue
        name = str(val).strip()
        if name:
            return name
    return None


def _rows_to_metrics(
    rows: list[dict[str, Any]],
    *,
    kind: str,
) -> tuple[list[ExtractedMetric], list[str]]:
    metrics: list[ExtractedMetric] = []
    warnings: list[str] = []

    if kind in MEMBER_CARD_DATASETS:
        for row in rows:
            player = _player_name(row)
            if not player:
                continue
            hq_raw = row.get("hq", row.get("hq_level", row.get("HQLevel")))
            power_raw = row.get("power", row.get("Power"))
            if hq_raw is not None:
                try:
                    metrics.append(
                        ExtractedMetric(player, "HQLevel", parse_numeric_value(hq_raw))
                    )
                except ValueError as exc:
                    warnings.append(f"Bad HQ for {player}: {exc}")
            if power_raw is not None:
                try:
                    metrics.append(
                        ExtractedMetric(player, "Power", parse_numeric_value(power_raw))
                    )
                except ValueError as exc:
                    warnings.append(f"Bad Power for {player}: {exc}")
        return relabel_member_cards(metrics, kind), warnings

    metric_type = LEADERBOARD_DATASETS[kind]

    for row in rows:
        player = _player_name(row)
        if not player:
            continue
        value_raw = row.get("value", row.get("score", row.get("points")))
        if value_raw is None:
            continue
        try:
            metrics.append(
                ExtractedMetric(player, metric_type, parse_numeric_value(value_raw))
            )
        except ValueError as exc:
            warnings.append(f"Bad value for {player}: {exc}")
    return metrics, warnings


def _chat_completions(
    *,
    base_url: str,
    model: str,
    api_key: str,
    timeout: float,
    prompt: str,
    data_url: str,
) -> str:
    import httpx

    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    # oMLX may accept empty/any key; still send Authorization when present
    headers["Authorization"] = f"Bearer {api_key}" if api_key else "Bearer "

    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    },
                ],
            }
        ],
    }

    with httpx.Client(timeout=timeout) as client:
        try:
            resp = client.post(url, headers=headers, json=body)
        except httpx.RequestError as exc:
            raise VisionTransientError(
                f"Vision OCR request failed contacting {url}: {exc}"
            ) from exc
        if resp.status_code >= 400:
            # Keep body short; never log Authorization / full keys
            detail = (resp.text or "")[:300].replace("\n", " ")
            error = VisionTransientError if resp.status_code >= 500 else VisionOCRError
            raise error(
                f"Vision OCR HTTP {resp.status_code} from {url} "
                f"(model={model}): {detail}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise VisionTransientError(f"Vision API returned non-JSON body: {exc}") from exc

    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise VisionTransientError(
            f"Unexpected vision API response shape: {exc}"
        ) from exc

    if isinstance(content, list):
        # Some servers return multimodal content parts
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
            elif isinstance(part, str):
                parts.append(part)
        content = "\n".join(parts)
    if not isinstance(content, str) or not content.strip():
        raise VisionTransientError("Vision API returned empty content")
    return content


def extract_metrics_via_vision(
    image_path: Path | str,
    *,
    kind: DatasetKind,
    base_url: str,
    model: str,
    api_key: str = "",
    timeout: float = 120.0,
) -> OCRResult:
    """
    Call a local OpenAI-compatible vision model and map rows to ExtractedMetric.
    """
    path = Path(image_path)
    if not path.is_file():
        raise ValueError(f"Could not read image: {path}")

    prompt = _prompt_for_kind(kind)
    data_url = _image_data_url(path)

    attempts = 1 + len(RETRY_DELAYS)
    for attempt in range(1, attempts + 1):
        log.info(
            "Vision OCR request model=%s kind=%s attempt=%d/%d url=%s",
            model,
            kind,
            attempt,
            attempts,
            base_url.rstrip("/") + "/chat/completions",
        )
        try:
            raw_text = _chat_completions(
                base_url=base_url,
                model=model,
                api_key=api_key,
                timeout=timeout,
                prompt=prompt,
                data_url=data_url,
            )
            try:
                rows = _parse_json_rows(raw_text)
            except (json.JSONDecodeError, ValueError) as exc:
                # Keep the start of the reply so bad output can be diagnosed.
                log.warning(
                    "Unreadable vision reply for %s (attempt %d/%d): %s | reply: %r",
                    path.name,
                    attempt,
                    attempts,
                    exc,
                    raw_text[:500],
                )
                raise VisionReplyError(
                    f"model reply was not valid JSON ({exc})"
                ) from exc
            break
        except VisionTransientError as exc:
            if attempt == attempts:
                raise
            delay = RETRY_DELAYS[attempt - 1]
            log.warning(
                "Vision OCR attempt %d/%d failed for %s: %s; retrying in %.0fs",
                attempt,
                attempts,
                path.name,
                exc,
                delay,
            )
            time.sleep(delay)

    warnings: list[str] = []
    metrics, row_warnings = _rows_to_metrics(
        rows, kind=kind
    )
    warnings.extend(row_warnings)

    dedup: dict[tuple[str, str], ExtractedMetric] = {}
    for m in metrics:
        dedup[(m.player_name.lower(), m.metric_type)] = m
    metrics = list(dedup.values())

    if not metrics:
        warnings.append(
            "No player metrics parsed from vision model. "
            "Try a tighter crop or check the dataset type."
        )

    log.info("Vision OCR kind=%s rows=%d metrics=%d", kind, len(rows), len(metrics))
    return OCRResult(kind=kind, metrics=metrics, raw_text=raw_text, warnings=warnings)
