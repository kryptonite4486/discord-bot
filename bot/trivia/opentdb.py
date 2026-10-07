"""Turn Open Trivia DB questions into entries for our question bank.

Open Trivia DB (https://opentdb.com) publishes its questions under CC BY-SA
4.0. scripts/import_opentdb.py fetches them; this module holds the pure
part, so it can be tested without the network: decoding, the checks a
question must pass to work in Discord, our category names, and removing
duplicates and excluded questions.

Every imported question is tagged ``tier: full`` (Command's extended bank)
and ``source: opentdb``, which puts the credit in its footer.
"""

from __future__ import annotations

import html
import re
from collections import Counter
from datetime import date
from typing import Iterable
from urllib.parse import unquote

from bot.trivia.questions import (
    DIFFICULTIES,
    MAX_CHOICE_LEN,
    MAX_QUESTION_LEN,
    _question_id,
)

SOURCE = "opentdb"
SITE_URL = "https://opentdb.com"
LICENSE = "CC BY-SA 4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by-sa/4.0/"
CHANGES = (
    "Decoded HTML entities, trimmed whitespace, renamed categories, and left out "
    "questions that don't fit Discord buttons, refer to answer positions or "
    "pictures, repeat a question already in our bank, or were excluded by hand "
    "(opentdb_exclude.json)."
)

# Open Trivia DB category -> ours. Some merge with categories the standard
# bank already uses (Gaming, Science, General Knowledge, ...).
CATEGORY_NAMES = {
    "General Knowledge": "General Knowledge",
    "Entertainment: Books": "Books",
    "Entertainment: Film": "Film",
    "Entertainment: Music": "Music",
    "Entertainment: Musicals & Theatres": "Musicals & Theatre",
    "Entertainment: Television": "Television",
    "Entertainment: Video Games": "Gaming",
    "Entertainment: Board Games": "Board Games",
    "Science & Nature": "Science",
    "Science: Computers": "Computers",
    "Science: Mathematics": "Mathematics",
    "Mythology": "Mythology",
    "Sports": "Sports",
    "Geography": "Geography",
    "History": "History",
    "Politics": "Politics",
    "Art": "Art",
    "Celebrities": "Celebrities",
    "Animals": "Animals",
    "Vehicles": "Vehicles",
    "Entertainment: Comics": "Comics",
    "Science: Gadgets": "Gadgets",
    "Entertainment: Japanese Anime & Manga": "Anime & Manga",
    "Entertainment: Cartoon & Animations": "Cartoons & Animation",
}

# Answers that only make sense in a fixed order, which shuffling breaks.
_POSITIONAL_ANSWER = re.compile(
    r"\b(all|none|both|neither) of (the )?(above|these|them|those|the answers)\b"
    r"|\b(all|none of the) (two|three|other) (options|answers|choices)\b"
    r"|^(answers? )?[a-d] (and|&) [a-d]$",
    re.IGNORECASE,
)
# Questions that lean on something we can't show or on the answer order.
_UNSHOWABLE_QUESTION = re.compile(
    r"\b(pictured|in this (image|picture|photo)|shown (above|below|here)"
    r"|of the above|of the following answers)\b",
    re.IGNORECASE,
)


def decode(text: str) -> str:
    """Text from the API's url3986 encoding, with any HTML entities resolved."""
    return " ".join(html.unescape(unquote(text)).split())


def convert(raw: dict) -> tuple[dict | None, str | None]:
    """(bank entry, None), or (None, why it was left out), for one API result."""
    try:
        text = decode(raw["question"])
        correct = decode(raw["correct_answer"])
        incorrect = [decode(a) for a in raw["incorrect_answers"]]
        category = decode(raw["category"])
        difficulty = decode(raw["difficulty"]).lower()
    except (KeyError, TypeError):
        return None, "malformed"
    answers = [correct, *incorrect]
    if category not in CATEGORY_NAMES:
        return None, "unknown category"
    if difficulty not in DIFFICULTIES:
        return None, "unknown difficulty"
    if not text or len(text) > MAX_QUESTION_LEN:
        return None, "question too long"
    if not 1 <= len(incorrect) <= 3:
        return None, "wrong number of answers"
    if any(not a or len(a) > MAX_CHOICE_LEN for a in answers):
        return None, "answer too long for a button"
    if len({a.lower() for a in answers}) != len(answers):
        return None, "repeated answers"
    if any(_POSITIONAL_ANSWER.search(a) for a in answers):
        return None, "answer depends on order"
    if _UNSHOWABLE_QUESTION.search(text):
        return None, "refers to a picture or the answer list"
    return {
        "category": CATEGORY_NAMES[category],
        "difficulty": difficulty,
        "question": text,
        "correct": correct,
        "incorrect": incorrect,
        "tier": "full",
        "source": SOURCE,
    }, None


def build_entries(
    raw: Iterable[dict],
    *,
    existing_ids: Iterable[str] = (),
    excluded_ids: Iterable[str] = (),
) -> tuple[list[dict], Counter]:
    """Bank entries for ``raw`` and a count of why the others were left out.

    Questions already in our bank (``existing_ids``), excluded by hand, or
    repeated within ``raw`` are dropped. Entries are sorted by category and
    question, so a refresh only shows real changes in a diff.
    """
    seen = set(existing_ids)
    excluded = set(excluded_ids)
    entries: list[dict] = []
    skipped: Counter = Counter()
    for item in raw:
        entry, reason = convert(item)
        if entry is None:
            skipped[reason] += 1
            continue
        qid = _question_id(entry["question"])
        if qid in excluded:
            skipped["excluded by hand"] += 1
        elif qid in seen:
            skipped["duplicate"] += 1
        else:
            seen.add(qid)
            entries.append(entry)
    entries.sort(key=lambda e: (e["category"].lower(), e["question"].lower()))
    return entries, skipped


def bank_document(entries: list[dict], retrieved: date) -> dict:
    """The file written to bot/trivia/questions_opentdb.json: credit, then questions."""
    return {
        "source": "Open Trivia DB",
        "source_url": SITE_URL,
        "license": LICENSE,
        "license_url": LICENSE_URL,
        "retrieved": retrieved.isoformat(),
        "changes": CHANGES,
        "questions": entries,
    }
