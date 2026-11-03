"""Text-length metrics the adapters need before the studio's `text_metrics.py` exists (spec 4.4).

APPROXIMATIONS, stated as such in preflight messages: `graphemes` joins combining marks, ZWJ sequences, variation selectors, skin tones,
tag sequences and regional-indicator pairs (no external `regex` module); `x_weighted_len` follows X's documented weighting (URL = 23,
emoji and CJK = 2) (U). Adapters apply a 2 % safety margin to the limit they compare against.
"""

from __future__ import annotations

import re
import unicodedata

_URL = re.compile(r"https?://[^\s]+")
ZWJ = "‍"


def _is_regional(ch: str) -> bool:
    return 0x1F1E6 <= ord(ch) <= 0x1F1FF


def graphemes(text: str) -> int:
    """Approximate count of user-perceived characters."""
    n, i, L = 0, 0, len(text)
    while i < L:
        ch = text[i]
        i += 1
        if _is_regional(ch) and i < L and _is_regional(text[i]):
            i += 1
        while i < L:
            c = text[i]
            cat = unicodedata.category(c)
            if c == ZWJ and i + 1 < L:
                i += 2                                     # joiner and the character it joins
            elif cat in ("Mn", "Me", "Mc") or c in ("️", "︎") or 0x1F3FB <= ord(c) <= 0x1F3FF or 0xE0020 <= ord(c) <= 0xE007F:
                i += 1
            else:
                break
        n += 1
    return n


def _is_wide(ch: str) -> bool:
    o = ord(ch)
    return (0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0xA4CF or 0xAC00 <= o <= 0xD7A3 or 0xF900 <= o <= 0xFAFF or 0xFE30 <= o <= 0xFE6F
            or 0xFF00 <= o <= 0xFF60 or 0xFFE0 <= o <= 0xFFE6 or 0x1F300 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF or 0x20000 <= o <= 0x3FFFD)


def x_weighted_len(text: str) -> int:
    """X's weighted length: every URL counts 23, wide characters (CJK, emoji) 2, everything else 1."""
    total, pos = 0, 0
    for m in _URL.finditer(text):
        total += _plain_weight(text[pos:m.start()]) + 23
        pos = m.end()
    return total + _plain_weight(text[pos:])


def _plain_weight(s: str) -> int:
    return sum(2 if _is_wide(c) else 1 for c in s if c not in ("️", "︎", ZWJ) and not unicodedata.category(c).startswith("M"))


def with_margin(limit: int, pct: float = 0.02) -> int:
    """The limit the server compares against: 2 % tighter than the platform's published number (the browser uses Intl.Segmenter)."""
    return max(1, int(limit * (1 - pct)))


def url_spans(text: str) -> list[tuple[int, int, str]]:
    """(utf8 byte start, byte end, url) of each http(s) link, trailing punctuation excluded: what Bluesky facets need."""
    out = []
    for m in _URL.finditer(text):
        url = m.group(0).rstrip(".,;:!?)\"'")
        start = len(text[:m.start()].encode())
        out.append((start, start + len(url.encode()), url))
    return out
