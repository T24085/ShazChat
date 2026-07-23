"""Server-side text moderation for ShazChat.

Only severe identity-based slurs are blocked by default. Normal profanity and
gameplay trash talk are intentionally left alone. The server can extend this
list with one additional word per line via SHAZCHAT_BLOCKED_WORDS_FILE.
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path


# Keep this deliberately narrow: this is intended to block severe slurs, not
# ordinary player banter. Do not show these terms back to clients or log them.
DEFAULT_BLOCKED_TERMS = frozenset(
    {
        "beaner",
        "chink",
        "faggot",
        "gook",
        "kike",
        "nigger",
        "nigga",
        "raghead",
        "spic",
        "towelhead",
        "tranny",
        "wetback",
    }
)

_LEET_MAP = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})
_TOKEN_RE = re.compile(r"[a-z0-9@$]+")
_REPEATED_CHAR_RE = re.compile(r"([a-z0-9])\1{2,}")


def _normalize_token(token: str) -> str:
    """Normalize ordinary evasion without changing what users see."""
    normalized = unicodedata.normalize("NFKD", str(token).casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    normalized = normalized.translate(_LEET_MAP)
    # Reduce exaggerated runs (for example, a repeated-letter evasion) while
    # preserving genuine double letters that appear in ordinary words.
    return _REPEATED_CHAR_RE.sub(r"\1\1", normalized)


def load_blocked_terms(path: str | os.PathLike[str] | None = None) -> frozenset[str]:
    """Return built-in terms plus optional, local server-owner additions."""
    terms = set(DEFAULT_BLOCKED_TERMS)
    configured_path = path or os.environ.get("SHAZCHAT_BLOCKED_WORDS_FILE")
    if not configured_path:
        return frozenset(terms)

    try:
        lines = Path(configured_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return frozenset(terms)

    for line in lines:
        candidate = _normalize_token(line.strip())
        if candidate and candidate.isalnum() and not line.lstrip().startswith("#"):
            terms.add(candidate)
    return frozenset(terms)


def contains_blocked_term(value: str, terms: frozenset[str] | None = None) -> bool:
    """Detect direct, basic leetspeak, and punctuation-separated blocked terms."""
    blocked = terms or DEFAULT_BLOCKED_TERMS
    tokens = [_normalize_token(token) for token in _TOKEN_RE.findall(str(value or "").casefold())]
    if any(token in blocked for token in tokens):
        return True

    # Catch forms such as s.p.i.c while avoiding false positives such as "spicy".
    single_letter_run = ""
    for token in tokens:
        if len(token) == 1:
            single_letter_run += token
            if single_letter_run in blocked:
                return True
        else:
            single_letter_run = ""
    return False
