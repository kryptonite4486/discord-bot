"""Gift codes: generating, normalizing and hashing them, and rate-limiting /redeem.

A code is 16 random characters from Crockford's base32 alphabet (80 bits),
shown as ``XXXX-XXXX-XXXX-XXXX``. The alphabet has no I, L, O or U, and
input is normalized (case, dashes, spaces, O→0, I/L→1), so codes are easy
to read out and type. Only a SHA-256 hash of each code is stored. A fast
hash is enough here: with 80 random bits a code can't be guessed from its
hash, and /redeem is rate-limited per user.

See docs/monetization-plan.md, section 6.
"""

from __future__ import annotations

import hashlib
import secrets

from bot.utils.rate_limit import SlidingWindowLimiter

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32
CODE_LENGTH = 16
GROUP = 4
_CONFUSABLE = str.maketrans({"O": "0", "I": "1", "L": "1"})


def generate_code() -> str:
    """A new random code, formatted ``XXXX-XXXX-XXXX-XXXX``."""
    raw = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
    return format_code(raw)


def format_code(raw: str) -> str:
    return "-".join(raw[i:i + GROUP] for i in range(0, len(raw), GROUP))


def normalize_code(text: str) -> str | None:
    """Canonical form of a typed code, or None if it can't be a code."""
    raw = "".join(ch for ch in text.upper() if ch.isalnum()).translate(_CONFUSABLE)
    if len(raw) != CODE_LENGTH or any(ch not in ALPHABET for ch in raw):
        return None
    return raw


def hash_code(normalized: str) -> str:
    return hashlib.sha256(normalized.encode("ascii")).hexdigest()


def code_hint(normalized: str) -> str:
    """Last four characters, so operators can tell codes apart in /ops code list."""
    return normalized[-GROUP:]


class AttemptLimiter(SlidingWindowLimiter):
    """Allows each user ``max_failures`` failed attempts per ``window`` seconds."""

    def __init__(self, max_failures: int = 5, window: float = 900.0) -> None:
        super().__init__(max_failures, window)

    @property
    def max_failures(self) -> int:
        return int(self.limit)

    def record_failure(self, user_id: int, now: float | None = None) -> None:
        self.record(user_id, now=now)
