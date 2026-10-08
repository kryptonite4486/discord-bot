"""Discord SKUs for the paid tiers, as listed in bot/utils/skus.json.

Discord's API can list SKUs but not create or edit them: they're made and
published by hand in the Developer Portal (Monetization → Manage SKUs).
skus.json is the record of what the portal should hold, one guild
subscription per paid tier: its SKU ID, name, price, description and
benefits. Edit it first, then copy the change into the portal;
``scripts/sync_skus.py portal`` prints what to type, and
``scripts/sync_skus.py`` checks the portal matches.

validate() keeps the store copy honest: each benefit names the tier limits
and features it promises, and those must match bot/utils/tiers.py, so a
change there that the store copy doesn't follow fails the tests.
"""

from __future__ import annotations

import dataclasses
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from bot.utils.tiers import PAID_TIERS, TIERS, TierPolicy

MANIFEST_PATH = Path(__file__).with_name("skus.json")

# Discord's limits for a subscription SKU's store listing.
MAX_NAME = 80
MAX_DESCRIPTION = 160
MAX_BENEFITS = 6
MAX_BENEFIT_NAME = 80
MAX_BENEFIT_DESCRIPTION = 160

_LIMIT_FIELDS = {
    f.name for f in dataclasses.fields(TierPolicy) if f.name not in {"key", "name", "rank", "features"}
}


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate(manifest: dict[str, Any]) -> list[str]:
    """Problems with the manifest; empty when it's consistent."""
    problems: list[str] = []
    skus = manifest.get("skus", [])
    tiers = [s.get("tier") for s in skus]
    for policy in PAID_TIERS:
        if tiers.count(policy.key) != 1:
            problems.append(f"tier {policy.key!r} needs exactly one SKU (found {tiers.count(policy.key)})")
    ids = [s.get("id") for s in skus if s.get("id")]
    if len(ids) != len(set(ids)):
        problems.append("two SKUs share an id")

    for sku in skus:
        tier = sku.get("tier")
        where = f"SKU {tier!r}"
        policy = TIERS.get(tier)
        if policy is None or policy not in PAID_TIERS:
            problems.append(f"{where}: not a paid tier")
            continue
        sku_id = sku.get("id")
        if sku_id is not None and not str(sku_id).isdigit():
            problems.append(f"{where}: id must be a Discord snowflake")
        # The store name may add a word ("Alliance Tier") but must lead with
        # the tier name /premium shows, so the two read as the same plan.
        if not str(sku.get("name", "")).startswith(policy.name):
            problems.append(f"{where}: name {sku.get('name')!r} should start with the tier name {policy.name!r}")
        if not isinstance(sku.get("price_usd"), (int, float)) or sku["price_usd"] <= 0:
            problems.append(f"{where}: price_usd must be a positive number")
        problems += _check_text(where, "name", sku.get("name"), MAX_NAME)
        problems += _check_text(where, "description", sku.get("description"), MAX_DESCRIPTION)

        benefits = sku.get("benefits", [])
        if not 1 <= len(benefits) <= MAX_BENEFITS:
            problems.append(f"{where}: needs 1 to {MAX_BENEFITS} benefits (has {len(benefits)})")
        for i, benefit in enumerate(benefits, 1):
            bwhere = f"{where} benefit {i}"
            if not benefit.get("emoji"):
                problems.append(f"{bwhere}: needs an emoji")
            problems += _check_text(bwhere, "name", benefit.get("name"), MAX_BENEFIT_NAME)
            problems += _check_text(
                bwhere, "description", benefit.get("description"), MAX_BENEFIT_DESCRIPTION
            )
            for feature in benefit.get("features", []):
                if not policy.allows(feature):
                    problems.append(f"{bwhere}: {policy.name} doesn't include feature {feature!r}")
            for field, value in benefit.get("limits", {}).items():
                if field not in _LIMIT_FIELDS:
                    problems.append(f"{bwhere}: unknown limit {field!r}")
                elif getattr(policy, field) != value:
                    problems.append(
                        f"{bwhere}: promises {field}={value!r} but {policy.name} has "
                        f"{getattr(policy, field)!r} (bot/utils/tiers.py)"
                    )
    return problems


def _check_text(where: str, label: str, text: Any, limit: int) -> list[str]:
    if not isinstance(text, str) or not text.strip():
        return [f"{where}: {label} is empty"]
    if len(text) > limit:
        return [f"{where}: {label} is {len(text)} characters (Discord allows {limit})"]
    return []


@lru_cache(maxsize=1)
def _skus() -> tuple[dict[str, Any], ...]:
    return tuple(load_manifest()["skus"])


def sku_id_for(tier: str) -> str | None:
    """The Discord SKU ID that sells ``tier``, or None."""
    return next((s.get("id") for s in _skus() if s["tier"] == tier), None)


def tier_for_sku(sku_id: str | int) -> str | None:
    """The tier key a Discord SKU grants, or None for an unknown SKU."""
    return next((s["tier"] for s in _skus() if s.get("id") == str(sku_id)), None)
