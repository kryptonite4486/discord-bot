"""Refresh bot/trivia/questions_opentdb.json from Open Trivia DB.

Fetches every verified question (about 5,300) through the public API, 50 at a
time with a session token so none repeat, waiting between requests as the API
asks (one request per 5 seconds per IP address). That takes around ten
minutes. Then it filters and converts them (bot/trivia/opentdb.py) and
writes the bank file, with the credit and license at the top.

    .venv/bin/python scripts/import_opentdb.py --save-raw /tmp/opentdb_raw.json
    .venv/bin/python scripts/import_opentdb.py --from-raw /tmp/opentdb_raw.json

``--from-raw`` reuses a saved download, so changing the filters doesn't mean
fetching again. Review the diff before committing; to drop a question for
good, add it to bot/trivia/opentdb_exclude.json and run again.

The questions are CC BY-SA 4.0: keep the credit in the file, the question
footers and /help (see README, Trivia).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.trivia.opentdb import bank_document, build_entries  # noqa: E402
from bot.trivia.questions import (  # noqa: E402
    BUNDLED_BANK,
    OPENTDB_BANK,
    load_bank,
)

API = "https://opentdb.com"
EXCLUDE_FILE = ROOT / "bot" / "trivia" / "opentdb_exclude.json"
PAGE = 50  # the API's maximum per request
DELAY = 5.5  # seconds between requests


def _get(path: str) -> dict:
    req = urllib.request.Request(f"{API}/{path}", headers={"User-Agent": "lastz-assistant-import"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_all(delay: float = DELAY) -> list[dict]:
    """Every verified question, category by category."""
    token = _get("api_token.php?command=request")["token"]
    categories = _get("api_category.php")["trivia_categories"]
    questions: list[dict] = []
    for cat in categories:
        time.sleep(delay)
        count = _get(f"api_count.php?category={cat['id']}")["category_question_count"]
        remaining = int(count["total_question_count"])
        got = 0
        while remaining > 0:
            time.sleep(delay)
            amount = min(PAGE, remaining)
            data = _get(
                f"api.php?amount={amount}&category={cat['id']}&token={token}&encode=url3986"
            )
            code = data["response_code"]
            if code == 0:
                questions.extend(data["results"])
                got += len(data["results"])
                remaining -= len(data["results"])
            elif code == 5:  # rate limited: back off and retry
                time.sleep(delay * 2)
            elif code in (1, 4):  # fewer left than the count said, or all seen
                break
            elif code in (2, 3):  # bad or expired token (6 hours idle)
                raise RuntimeError(f"token rejected (code {code}) in {cat['name']}; run again")
            else:
                raise RuntimeError(f"unexpected response code {code} in {cat['name']}")
        print(f"{cat['name']}: {got}", file=sys.stderr)
    return questions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--from-raw", type=Path, help="convert a saved download instead of fetching")
    source.add_argument("--save-raw", type=Path, help="also save the download here")
    parser.add_argument("--out", type=Path, default=OPENTDB_BANK)
    args = parser.parse_args()

    if args.from_raw:
        raw = json.loads(args.from_raw.read_text(encoding="utf-8"))
    else:
        raw = fetch_all()
        if args.save_raw:
            args.save_raw.write_text(json.dumps(raw), encoding="utf-8")
    excluded = [e["id"] for e in json.loads(EXCLUDE_FILE.read_text(encoding="utf-8"))]
    existing = [q.id for q in load_bank(BUNDLED_BANK)]
    entries, skipped = build_entries(raw, existing_ids=existing, excluded_ids=excluded)

    doc = bank_document(entries, date.today())
    questions = doc.pop("questions")
    head = json.dumps(doc, ensure_ascii=False, indent=2)[:-2]  # drop the closing "\n}"
    lines = ",\n    ".join(json.dumps(q, ensure_ascii=False) for q in questions)
    args.out.write_text(f'{head},\n  "questions": [\n    {lines}\n  ]\n}}\n', encoding="utf-8")

    print(f"{len(raw)} fetched, {len(entries)} written to {args.out}", file=sys.stderr)
    for reason, n in skipped.most_common():
        print(f"  left out, {reason}: {n}", file=sys.stderr)


if __name__ == "__main__":
    main()
