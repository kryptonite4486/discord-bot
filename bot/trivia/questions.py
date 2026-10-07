"""Trivia question bank: loading, validation and picking questions for a match.

The bank is bundled with the bot, so trivia needs no third-party service:
our own questions (``questions.json``) and an imported, CC BY-SA 4.0 copy of
Open Trivia DB (``questions_opentdb.json``, written by
scripts/import_opentdb.py). ``TRIVIA_QUESTIONS_PATH`` points at a file that
replaces both. A bank file is a JSON list of questions, or an object holding
one under ``"questions"`` next to its credit and license:

    {"category": "Science", "difficulty": "easy", "question": "...",
     "correct": "...", "incorrect": ["...", "...", "..."], "tier": "mid"}

``incorrect`` holds one to three wrong answers, so true/false questions work.

``tier`` is the cheapest plan that gets the question (``free``, ``mid`` for
Alliance, ``full`` for Command; default ``mid``), and each plan also gets the
questions of the plans below it: Free gets only the Last Z questions, Alliance
adds the standard bank, Command adds the extended one. See pool_for.

``source`` (optional) names where a question comes from; SOURCE_CREDITS
turns it into the credit shown under the question.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

BUNDLED_BANK = Path(__file__).with_name("questions.json")
OPENTDB_BANK = Path(__file__).with_name("questions_opentdb.json")
BUNDLED_BANKS = (BUNDLED_BANK, OPENTDB_BANK)
# Shown in the footer of questions from a source that must be credited.
SOURCE_CREDITS = {"opentdb": "Open Trivia DB (CC BY-SA 4.0)"}
DIFFICULTIES = ("easy", "medium", "hard")
# Plan keys, cheapest first (TierPolicy.key in bot/utils/tiers.py).
TIER_KEYS = ("free", "mid", "full")
DEFAULT_TIER = "mid"
# Discord button labels max out at 80 characters, with room for "A. ".
MAX_CHOICE_LEN = 76
MAX_QUESTION_LEN = 300


@dataclass(frozen=True)
class Question:
    id: str
    category: str
    difficulty: str
    text: str
    correct: str
    incorrect: tuple[str, ...]
    tier: str = DEFAULT_TIER
    source: str | None = None

    @property
    def credit(self) -> str | None:
        return SOURCE_CREDITS.get(self.source) if self.source else None


@dataclass(frozen=True)
class AskedQuestion:
    """A question as shown in one match: choices in a fixed, shuffled order."""

    question: Question
    choices: tuple[str, ...]
    answer_index: int

    @property
    def correct(self) -> str:
        return self.choices[self.answer_index]


def _question_id(text: str) -> str:
    # Stable across bank edits that only reorder entries.
    return hashlib.sha1(text.strip().lower().encode()).hexdigest()[:12]


def parse_bank(entries: Iterable[dict]) -> list[Question]:
    """Validate raw entries; raise ValueError naming the first bad one."""
    questions: list[Question] = []
    seen: set[str] = set()
    for n, raw in enumerate(entries, start=1):
        try:
            text = str(raw["question"]).strip()
            correct = str(raw["correct"]).strip()
            incorrect = tuple(str(x).strip() for x in raw["incorrect"])
            category = str(raw.get("category") or "General Knowledge").strip()
            difficulty = str(raw.get("difficulty") or "medium").strip().lower()
            tier = str(raw.get("tier") or DEFAULT_TIER).strip().lower()
            source = str(raw.get("source") or "").strip().lower() or None
        except (KeyError, TypeError) as exc:
            raise ValueError(f"Question {n}: missing or invalid field ({exc})") from exc
        problem = None
        if not text or len(text) > MAX_QUESTION_LEN:
            problem = f"question text must be 1-{MAX_QUESTION_LEN} characters"
        elif not 1 <= len(incorrect) <= 3:
            problem = "needs one to three incorrect answers"
        elif any(not c or len(c) > MAX_CHOICE_LEN for c in (correct, *incorrect)):
            problem = f"answers must be 1-{MAX_CHOICE_LEN} characters"
        elif len({c.lower() for c in (correct, *incorrect)}) != len(incorrect) + 1:
            problem = "answers must all be different"
        elif difficulty not in DIFFICULTIES:
            problem = f"difficulty must be one of {', '.join(DIFFICULTIES)}"
        elif tier not in TIER_KEYS:
            problem = f"tier must be one of {', '.join(TIER_KEYS)}"
        if problem:
            raise ValueError(f"Question {n} ({text[:40]!r}): {problem}")
        qid = _question_id(text)
        if qid in seen:
            raise ValueError(f"Question {n} ({text[:40]!r}) is a duplicate")
        seen.add(qid)
        questions.append(Question(qid, category, difficulty, text, correct, incorrect, tier, source)
        )
    if not questions:
        raise ValueError("The question bank is empty")
    return questions


def _read_entries(path: Path) -> list:
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get("questions")
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON list of questions, or an object with one")
    return data


def load_bank(path: Path | None = None) -> list[Question]:
    """One bank file, or with no path every bundled one (duplicates across them rejected)."""
    paths = (path,) if path is not None else BUNDLED_BANKS
    return parse_bank([entry for p in paths for entry in _read_entries(p)])


def pool_for(bank: Sequence[Question], tier: str) -> list[Question]:
    """The questions a server on ``tier`` gets: its own and every cheaper plan's.

    A bank with none of them (a custom bank that tags nothing ``free``, say)
    is used whole, so no plan is ever left without questions.
    """
    allowed = TIER_KEYS[: TIER_KEYS.index(tier) + 1]
    return [q for q in bank if q.tier in allowed] or list(bank)


def categories(bank: Sequence[Question]) -> list[str]:
    return sorted({q.category for q in bank}, key=str.lower)


def pick_questions(
    bank: Sequence[Question],
    count: int,
    *,
    category: str | None = None,
    difficulty: str | None = None,
    avoid: Iterable[str] = (),
    rng: random.Random | None = None,
) -> list[AskedQuestion]:
    """Up to ``count`` questions with shuffled choices.

    ``avoid`` lists recently asked question IDs, oldest first. Fresh questions
    come first; once they run out, the ones asked longest ago fill the rest,
    so a small bank cycles through everything before repeating.
    """
    rng = rng or random.Random()
    pool = [
        q
        for q in bank
        if (category is None or q.category.lower() == category.lower())
        and (difficulty is None or q.difficulty == difficulty)
    ]
    # Position of each ID's latest use: lower = asked longer ago.
    last_asked = {qid: n for n, qid in enumerate(avoid)}
    fresh = [q for q in pool if q.id not in last_asked]
    rng.shuffle(fresh)
    stale = sorted(
        (q for q in pool if q.id in last_asked), key=lambda q: last_asked[q.id]
    )
    picked = (fresh + stale)[:count]
    rng.shuffle(picked)  # don't ask the reused ones in a predictable order
    asked = []
    for q in picked:
        choices = [q.correct, *q.incorrect]
        rng.shuffle(choices)
        asked.append(AskedQuestion(q, tuple(choices), choices.index(q.correct)))
    return asked
