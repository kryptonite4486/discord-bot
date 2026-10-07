"""Open Trivia DB import (bot/trivia/opentdb.py) and the bundled copy."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.cogs.trivia import question_footer  # noqa: E402
from bot.trivia.opentdb import CATEGORY_NAMES, bank_document, build_entries, convert  # noqa: E402
from bot.trivia.questions import (  # noqa: E402
    OPENTDB_BANK,
    _question_id,
    load_bank,
    parse_bank,
)


def _api(question="What is 2 + 2?", correct="4", incorrect=("3", "5", "22"), **kw) -> dict:
    """One API result, url3986-encoded like the import requests it."""
    raw = {
        "type": "multiple", "difficulty": "easy", "category": "Science: Mathematics",
        "question": question, "correct_answer": correct, "incorrect_answers": list(incorrect),
        **kw,
    }
    enc = lambda v: [quote(x, safe="") for x in v] if isinstance(v, list) else quote(v, safe="")  # noqa: E731
    return {k: enc(v) for k, v in raw.items()}


class ConvertTests(unittest.TestCase):
    def test_decodes_and_maps_to_our_bank_format(self) -> None:
        entry, reason = convert(_api(question="Who wrote &quot;Dune&quot;?  ", correct="Frank Herbert",
                                     incorrect=["Isaac Asimov"], category="Entertainment: Books"))
        self.assertIsNone(reason)
        self.assertEqual(entry, {
            "category": "Books", "difficulty": "easy", "question": 'Who wrote "Dune"?',
            "correct": "Frank Herbert", "incorrect": ["Isaac Asimov"],
            "tier": "full", "source": "opentdb",
        })
        parse_bank([entry])  # valid for our loader

    def test_true_false_questions_convert(self) -> None:
        entry, _ = convert(_api(question="The sun is a star.", correct="True", incorrect=["False"]))
        self.assertEqual((entry["correct"], entry["incorrect"]), ("True", ["False"]))

    def test_leaves_out_questions_that_dont_work_in_discord(self) -> None:
        cases = {
            "answer too long for a button": _api(correct="x" * 77),
            "question too long": _api(question="Q" * 301),
            "answer depends on order": _api(incorrect=["3", "5", "All of the above"]),
            "answer depends on order ": _api(incorrect=["3", "5", "All three options."]),
            "refers to a picture or the answer list": _api(question="Which animal is pictured?"),
            "repeated answers": _api(incorrect=["4", "5", "6"]),
            "unknown category": _api(category="Knitting"),
        }
        for reason, raw in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(convert(raw), (None, reason.strip()))

    def test_every_category_has_a_name(self) -> None:
        self.assertEqual(len(CATEGORY_NAMES), 24)
        self.assertNotIn("Entertainment", " ".join(CATEGORY_NAMES.values()))


class BuildEntriesTests(unittest.TestCase):
    def test_drops_duplicates_and_exclusions_and_sorts(self) -> None:
        raw = [
            _api(question="Zebra?", category="Animals"),
            _api(question="Ours?"),
            _api(question="Repeat?"), _api(question="repeat? "),
            _api(question="Bad?"),
            _api(question="Apple?", category="Art"),
        ]
        entries, skipped = build_entries(
            raw, existing_ids=[_question_id("Ours?")], excluded_ids=[_question_id("Bad?")]
        )
        self.assertEqual([e["question"] for e in entries], ["Zebra?", "Apple?", "Repeat?"])
        self.assertEqual(skipped, {"duplicate": 2, "excluded by hand": 1})

    def test_document_carries_the_credit(self) -> None:
        doc = bank_document([], date(2026, 10, 7))
        self.assertEqual(doc["license"], "CC BY-SA 4.0")
        self.assertIn("opentdb.com", doc["source_url"])
        self.assertEqual(doc["retrieved"], "2026-10-07")


class BundledCopyTests(unittest.TestCase):
    def test_bundled_copy_is_credited_command_only(self) -> None:
        doc = json.loads(OPENTDB_BANK.read_text(encoding="utf-8"))
        self.assertEqual(doc["license"], "CC BY-SA 4.0")
        bank = load_bank(OPENTDB_BANK)
        self.assertGreater(len(bank), 2000)
        self.assertEqual({(q.tier, q.source) for q in bank}, {("full", "opentdb")})

    def test_default_bank_loads_both_files(self) -> None:
        bank = load_bank()
        sources = {q.source for q in bank}
        self.assertEqual(sources, {None, "opentdb"})

    def test_object_format_and_footer_credit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bank.json"
            path.write_text(json.dumps({"license": "x", "questions": [
                {"question": "Q?", "correct": "a", "incorrect": ["b"], "category": "Art",
                 "difficulty": "easy", "source": "opentdb"},
                {"question": "R?", "correct": "a", "incorrect": ["b"], "category": "Art"},
            ]}))
            credited, ours = load_bank(path)
        self.assertEqual(question_footer(credited), "Art · Easy · via Open Trivia DB (CC BY-SA 4.0)")
        self.assertEqual(question_footer(ours), "Art · Medium")


if __name__ == "__main__":
    unittest.main()
