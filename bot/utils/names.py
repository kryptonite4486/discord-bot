"""Match OCR-read player names to players already stored in a channel.

The vision model sometimes misreads a name's capitalisation (``Kyokilla`` for
``KyoKilla``), swaps look-alike characters (``S0FIA``/``SOFIA``) or invents a
stray symbol (``KryOGE\\N`` for ``KryOGeN``). Saving those as-is splits one
player's history across several names. Matching is deliberately narrow: only
capitalisation, look-alike characters and stray symbols are ignored, so
genuinely different players such as ``Player1`` and ``Player2`` never merge.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping

# Characters the vision model confuses, mapped to one representative.
_LOOKALIKES = str.maketrans({"0": "o", "1": "i", "l": "i", "5": "s"})
_SYMBOLS = re.compile(r"[^\w ]+")
_SPACES = re.compile(r"\s+")


def lookalike_key(name: str) -> str:
    """Comparison key ignoring case, look-alike characters and stray symbols."""
    key = unicodedata.normalize("NFKC", name).casefold().translate(_LOOKALIKES)
    key = _SYMBOLS.sub("", key.replace("_", ""))
    return _SPACES.sub(" ", key).strip()


def group_variants(
    summary: Iterable[Mapping[str, object]],
) -> list[list[Mapping[str, object]]]:
    """
    Group per-channel name rows (``ChannelId``, ``PlayerName``, ``Rows``...) whose
    names share a look-alike key within the same channel. Only groups with more
    than one spelling are returned, each sorted by row count (most first).
    """
    groups: dict[tuple[object, str], list[Mapping[str, object]]] = defaultdict(list)
    for row in summary:
        groups[(row["ChannelId"], lookalike_key(str(row["PlayerName"])))].append(row)
    variants = [
        sorted(rows, key=lambda r: (-int(r["Rows"]), str(r["PlayerName"])))  # type: ignore[call-overload]
        for rows in groups.values()
        if len(rows) > 1
    ]
    return sorted(variants, key=lambda g: (str(g[0]["ChannelId"]), str(g[0]["PlayerName"]).casefold()))


def reconcile_names(
    read_names: Iterable[str],
    known: Mapping[str, int],
) -> dict[str, str]:
    """
    Map OCR-read names to stored spellings; names left unmapped are kept as read.

    ``known`` maps each stored name to its row count. A read name maps when it
    matches a stored name ignoring case (the most-used spelling wins if several
    variants are stored), or failing that, when exactly one stored name shares
    its look-alike key. A stored name claimed by two different read names (or
    already read exactly on the same screenshot) is ambiguous, so it isn't used.
    """
    by_case: dict[str, list[str]] = defaultdict(list)
    by_key: dict[str, list[str]] = defaultdict(list)
    for name in known:
        by_case[name.casefold()].append(name)
        by_key[lookalike_key(name)].append(name)

    reads = list(dict.fromkeys(read_names))
    proposed: dict[str, str] = {}
    targets: dict[str, int] = defaultdict(int)
    for read in reads:
        if read in known:
            targets[read] += 1
            continue
        candidates = by_case.get(read.casefold())
        if not candidates:
            candidates = by_key.get(lookalike_key(read), [])
            if len(candidates) != 1:
                continue
        proposed[read] = max(candidates, key=lambda n: (known[n], n))

    for target in proposed.values():
        targets[target] += 1
    return {read: target for read, target in proposed.items() if targets[target] == 1}
