"""Check the app's Discord SKUs against bot/utils/skus.json, and manage test entitlements.

Discord's API can't create or edit SKUs; that's done by hand in the Developer
Portal (Monetization → Manage SKUs). skus.json records what each SKU should
be, and this script keeps the two in step:

    .venv/bin/python scripts/sync_skus.py              # check the portal matches skus.json
    .venv/bin/python scripts/sync_skus.py --require-published
    .venv/bin/python scripts/sync_skus.py portal       # what to type into the portal
    .venv/bin/python scripts/sync_skus.py entitlements list
    .venv/bin/python scripts/sync_skus.py entitlements grant --guild 123 --tier mid
    .venv/bin/python scripts/sync_skus.py entitlements remove --id 456

The API only shows each SKU's name, type and flags (published or not), so
check compares those; price, description and benefits can only be compared
by eye against ``portal``.

Test entitlements give a server a SKU without paying, until removed. They're
for trying the purchase flow; the bot doesn't act on Discord entitlements yet
(docs/monetization-plan.md, Phase 2).

Uses DISCORD_TOKEN from the environment or .env, and refuses to run with a
different app's token than skus.json names (the testing bot has no SKUs).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.utils.skus import MANIFEST_PATH, load_manifest, validate  # noqa: E402

API = "https://discord.com/api/v10"

# SKU types and flags (Discord API docs, SKU resource).
SUBSCRIPTION = 5
SUBSCRIPTION_GROUP = 6
AVAILABLE = 1 << 2
GUILD_SUBSCRIPTION = 1 << 7
USER_SUBSCRIPTION = 1 << 8
OWNER_GUILD = 1


def request(client: httpx.Client, method: str, path: str, **kw) -> httpx.Response:
    """Send a request, waiting out 429 rate limits."""
    while True:
        r = client.request(method, API + path, **kw)
        if r.status_code != 429:
            if r.is_error:
                print(f"{method} {path}: {r.status_code} {r.text}", file=sys.stderr)
            r.raise_for_status()
            return r
        wait = float(r.json().get("retry_after", 1.0))
        print(f"  rate limited, waiting {wait:.1f}s")
        time.sleep(wait + 0.25)


def check(client: httpx.Client, app_id: str, manifest: dict, require_published: bool) -> int:
    live = {s["id"]: s for s in request(client, "GET", f"/applications/{app_id}/skus").json()}
    problems: list[str] = []
    unpublished: list[str] = []

    group = live.get(manifest.get("group_id") or "")
    if group is None or group["type"] != SUBSCRIPTION_GROUP:
        problems.append(f"subscription group {manifest.get('group_id')} not found")

    for sku in manifest["skus"]:
        label = f"{sku['name']} ({sku['tier']})"
        got = live.get(sku.get("id") or "")
        if got is None:
            problems.append(f"{label}: SKU {sku.get('id')} not found; create it in the portal "
                            "and put its ID in skus.json")
            continue
        if got["type"] != SUBSCRIPTION:
            problems.append(f"{label}: type {got['type']}, expected a subscription ({SUBSCRIPTION})")
        if not got["flags"] & GUILD_SUBSCRIPTION:
            problems.append(f"{label}: not a guild (server) subscription")
        if got["name"] != sku["name"]:
            problems.append(f"{label}: portal name is {got['name']!r}, skus.json says {sku['name']!r}")
        published = bool(got["flags"] & AVAILABLE)
        if not published:
            unpublished.append(label)
        print(f"{label}: SKU {got['id']}, {'published' if published else 'unpublished'}")

    known = {s.get("id") for s in manifest["skus"]} | {manifest.get("group_id")}
    for sku in live.values():
        if sku["id"] not in known and sku["type"] == SUBSCRIPTION:
            state = "published" if sku["flags"] & AVAILABLE else "unpublished"
            problems.append(f"SKU {sku['id']} {sku['name']!r} ({state}) isn't in skus.json")

    if unpublished:
        msg = f"unpublished: {', '.join(unpublished)}"
        (problems.append if require_published else print)(msg)
    for p in problems:
        print(f"PROBLEM: {p}")
    if not problems:
        print("Portal matches skus.json (name, type and flags; check price, description and "
              "benefits by eye against `sync_skus.py portal`).")
    return 1 if problems else 0


def portal(manifest: dict) -> int:
    """Print each SKU's store listing in the order the portal asks for it."""
    for sku in manifest["skus"]:
        print(f"=== {sku['name']}  (SKU {sku.get('id')}) ===")
        print("Type:        Guild Subscription")
        print(f"Name:        {sku['name']}")
        print(f"Price:       ${sku['price_usd']:.2f} / month")
        print(f"Description: {sku['description']}")
        for i, b in enumerate(sku["benefits"], 1):
            print(f"Benefit {i}:   {b['emoji']}  {b['name']}")
            print(f"             {b['description']}")
        print()
    return 0


def entitlements(client: httpx.Client, app_id: str, manifest: dict, args) -> int:
    by_tier = {s["tier"]: s for s in manifest["skus"]}
    names = {s.get("id"): s["name"] for s in manifest["skus"]}
    if args.action == "list":
        params = {"exclude_ended": "true", "limit": 100}
        if args.guild:
            params["guild_id"] = args.guild
        rows = request(client, "GET", f"/applications/{app_id}/entitlements", params=params).json()
        for e in rows:
            kind = "test" if e.get("type") == 4 else f"type {e.get('type')}"
            print(f"{e['id']}  guild {e.get('guild_id')}  {names.get(e['sku_id'], e['sku_id'])}  "
                  f"{kind}  ends {e.get('ends_at') or '-'}")
        if not rows:
            print("No active entitlements.")
    elif args.action == "grant":
        if not args.guild or args.tier not in by_tier:
            print("grant needs --guild and --tier (mid or full)", file=sys.stderr)
            return 2
        body = {"sku_id": by_tier[args.tier]["id"], "owner_id": args.guild, "owner_type": OWNER_GUILD}
        e = request(client, "POST", f"/applications/{app_id}/entitlements", json=body).json()
        print(f"Granted test entitlement {e['id']}: {by_tier[args.tier]['name']} for guild {args.guild}")
    elif args.action == "remove":
        if not args.id:
            print("remove needs --id (from `entitlements list`)", file=sys.stderr)
            return 2
        request(client, "DELETE", f"/applications/{app_id}/entitlements/{args.id}")
        print(f"Removed test entitlement {args.id}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--env-file", default=str(ROOT / ".env"))
    ap.add_argument("--require-published", action="store_true",
                    help="treat unpublished SKUs as a problem")
    sub = ap.add_subparsers(dest="command")
    sub.add_parser("check", help="compare the portal with skus.json (default)")
    sub.add_parser("portal", help="print what to type into the portal")
    ent = sub.add_parser("entitlements", help="list, grant or remove test entitlements")
    ent.add_argument("action", choices=("list", "grant", "remove"))
    ent.add_argument("--guild", help="server ID")
    ent.add_argument("--tier", choices=("mid", "full"))
    ent.add_argument("--id", help="entitlement ID to remove")
    args = ap.parse_args()

    manifest = load_manifest()
    problems = validate(manifest)
    if problems:
        print(f"{MANIFEST_PATH.relative_to(ROOT)} is inconsistent:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1
    if args.command == "portal":
        return portal(manifest)

    load_dotenv(args.env_file)
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print(f"DISCORD_TOKEN not set (looked in the environment and {args.env_file})",
              file=sys.stderr)
        return 1
    headers = {"Authorization": f"Bot {token}", "User-Agent": "LastZAssistant SKU sync"}
    with httpx.Client(headers=headers, timeout=30) as client:
        app = request(client, "GET", "/applications/@me").json()
        if app["id"] != manifest["application_id"]:
            print(f"This token is for {app['name']} ({app['id']}); skus.json is for app "
                  f"{manifest['application_id']}. Use the production bot's .env.", file=sys.stderr)
            return 1
        if args.command == "entitlements":
            return entitlements(client, app["id"], manifest, args)
        return check(client, app["id"], manifest, args.require_published)


if __name__ == "__main__":
    sys.exit(main())
