"""Upload assets/emojis/*.png to the bot as application emojis.

Application emojis belong to the bot itself (up to 2000), so it can use them
in every server without needing emoji slots or permissions there. The bot
looks them up by name at startup (bot/utils/emojis.py), so the IDs never
need copying anywhere.

Uses DISCORD_TOKEN from the environment or .env. Emojis the app already has
(same name) are skipped, so it's safe to rerun after adding new ones.

    .venv/bin/python scripts/upload_emojis.py --dry-run
    .venv/bin/python scripts/upload_emojis.py
    .venv/bin/python scripts/upload_emojis.py --replace zombie,kills
    .venv/bin/python scripts/upload_emojis.py --env-file .env.testing

``--replace`` deletes and re-uploads the named emojis (Discord can't change
an emoji's image in place), for after redrawing them. Their IDs change; the
bot picks up the new ones on its next restart.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
EMOJI_DIR = ROOT / "assets" / "emojis"
API = "https://discord.com/api/v10"


def request(client: httpx.Client, method: str, path: str, **kw) -> httpx.Response:
    """Send a request, waiting out 429 rate limits."""
    while True:
        r = client.request(method, API + path, **kw)
        if r.status_code != 429:
            r.raise_for_status()
            return r
        wait = float(r.json().get("retry_after", 1.0))
        print(f"  rate limited, waiting {wait:.1f}s")
        time.sleep(wait + 0.25)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--env-file", default=str(ROOT / ".env"))
    ap.add_argument("--dry-run", action="store_true", help="list what would change")
    ap.add_argument("--replace", default="", help="comma-separated names to re-upload")
    args = ap.parse_args()

    load_dotenv(args.env_file)
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print(f"DISCORD_TOKEN not set (looked in the environment and {args.env_file})",
              file=sys.stderr)
        return 1

    files = {p.stem: p for p in sorted(EMOJI_DIR.glob("*.png")) if not p.name.startswith("_")}
    replace = {n for n in args.replace.split(",") if n}
    unknown = replace - files.keys()
    if unknown:
        print(f"no such emoji file(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        return 1

    headers = {"Authorization": f"Bot {token}", "User-Agent": "LastZAssistant emoji upload"}
    with httpx.Client(headers=headers, timeout=30) as client:
        app = request(client, "GET", "/applications/@me").json()
        app_id = app["id"]
        existing = {
            e["name"]: e["id"]
            for e in request(client, "GET", f"/applications/{app_id}/emojis").json()["items"]
        }
        print(f"{app['name']}: {len(existing)} emoji(s) uploaded, {len(files)} in {EMOJI_DIR.name}/")

        todo = [n for n in files if n not in existing or n in replace]
        if len(existing) - len(replace & existing.keys()) + len(todo) > 2000:
            print("that would exceed Discord's 2000 application emoji limit", file=sys.stderr)
            return 1
        if args.dry_run:
            for n in todo:
                print(f"  would {'replace' if n in existing else 'upload'} {n}")
            print(f"{len(todo)} to upload")
            return 0

        for i, name in enumerate(todo, 1):
            if name in existing:
                request(client, "DELETE", f"/applications/{app_id}/emojis/{existing[name]}")
            data = base64.b64encode(files[name].read_bytes()).decode()
            request(client, "POST", f"/applications/{app_id}/emojis",
                    json={"name": name, "image": f"data:image/png;base64,{data}"})
            print(f"  [{i}/{len(todo)}] {name}")

        extra = sorted(existing.keys() - files.keys())
        print(f"done: {len(todo)} uploaded")
        if extra:
            print(f"on the app but not in {EMOJI_DIR.name}/ (left alone): {', '.join(extra)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
