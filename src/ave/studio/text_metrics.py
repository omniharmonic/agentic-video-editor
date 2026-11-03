"""Platform text metrics (spec 4.4). The browser twin is `studio/ui/static/text_metrics.js`; both are checked against
`tests/data_studio/text_metrics_fixtures.json` (same strings, same expected numbers), so the editor counter and the server preflight cannot drift.

    graphemes(text)        user-perceived characters (ZWJ emoji, flags, skin tones, keycaps, combining marks): Bluesky/Threads count these
    x_weighted_len(text)   X's weighted length: URLs 23, emoji and wide scripts (CJK) 2, the rest 1
    ig_visible_fold(text)  Instagram's caption cut ("... more"); `visible_fold` is the same for any platform rule in config/previews.yaml
    counter(text, unit)    one number in the platform's unit; `with_margin(limit)` is the 2 % server-side safety margin

The Python grapheme splitter is an approximation of UAX #29 (the cases that matter for posts); the server always states "approximate".
"""

from __future__ import annotations

import re
import unicodedata

MARGIN = 0.02
X_URL_WEIGHT = 23
IG_FOLD_CHARS = 125

_URL = re.compile(r"(?:https?://|www\.)[^\s]+", re.IGNORECASE)
_URL_TRAIL = ".,;:!?)]}'\""


def _is_extend(cp: int) -> bool:
    if cp == 0x200D:                                   # ZWJ is handled by the caller
        return False
    if 0xFE00 <= cp <= 0xFE0F or 0x1F3FB <= cp <= 0x1F3FF or 0xE0020 <= cp <= 0xE007F or cp == 0x20E3:
        return True
    return unicodedata.category(chr(cp)) in ("Mn", "Me", "Mc")


def _is_pictographic(cp: int) -> bool:
    return (0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF or 0x2300 <= cp <= 0x23FF or 0x2B00 <= cp <= 0x2BFF
            or cp in (0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139, 0x3030, 0x303D, 0x3297, 0x3299) or 0x2190 <= cp <= 0x21FF
            or 0x25A0 <= cp <= 0x25FF)


def _is_ri(cp: int) -> bool:
    return 0x1F1E6 <= cp <= 0x1F1FF


def graphemes(text: str) -> list[str]:
    """Split into user-perceived characters (an approximation of UAX #29 extended grapheme clusters)."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        cp = ord(text[i])
        j = i + 1
        if cp == 0x0D and j < n and text[j] == "\n":
            j += 1
        elif _is_ri(cp) and j < n and _is_ri(ord(text[j])):
            j += 1
            while j < n and _is_extend(ord(text[j])):
                j += 1
        elif cp not in (0x0A, 0x0D, 0x09):
            while j < n:
                c = ord(text[j])
                if _is_extend(c):
                    j += 1
                elif c == 0x200D:
                    j += 1
                    if j < n and _is_pictographic(ord(text[j])):
                        j += 1
                else:
                    break
        out.append(text[i:j])
        i = j
    return out


def grapheme_len(text: str) -> int:
    return len(graphemes(text))


def _is_wide(cp: int) -> bool:
    """twitter-text weight 1 ranges; everything else (CJK, emoji, ...) weighs 2."""
    return not (cp <= 4351 or 8192 <= cp <= 8205 or 8208 <= cp <= 8223 or 8242 <= cp <= 8247)


def _urls(text: str) -> list[tuple[int, int]]:
    spans = []
    for m in _URL.finditer(text):
        end = m.end()
        while end > m.start() and text[end - 1] in _URL_TRAIL:
            end -= 1
        if end > m.start():
            spans.append((m.start(), end))
    return spans


def x_weighted_len(text: str) -> int:
    """X's weighted length (spec 4.4): each URL counts 23, an emoji cluster 2, a wide-script character 2, the rest 1."""
    total, pos = 0, 0
    for a, b in _urls(text):
        total += _weigh(text[pos:a]) + X_URL_WEIGHT
        pos = b
    return total + _weigh(text[pos:])


def _weigh(chunk: str) -> int:
    total = 0
    for g in graphemes(chunk):
        cps = [ord(c) for c in g]
        if any(_is_pictographic(c) or _is_ri(c) for c in cps) and any(c > 0x2000 for c in cps):
            total += 2
        else:
            total += sum(2 if _is_wide(c) else 1 for c in cps)
    return total


def counter(text: str, unit: str = "chars") -> int:
    if unit == "graphemes":
        return grapheme_len(text)
    if unit == "x_weighted":
        return x_weighted_len(text)
    if unit == "bytes":
        return len(text.encode("utf-8"))
    return len(text)                                    # chars = code points


def with_margin(limit: int, margin: float = MARGIN) -> int:
    """The server-side effective limit: `margin` below the platform's number, because ours is an approximation."""
    return int(limit * (1 - margin))


def over_limit(text: str, unit: str, limit: int, *, margin: float = MARGIN) -> bool:
    return counter(text, unit) > with_margin(limit, margin)


def _wrap_lines(text: str, per_line: int) -> list[list[str]]:
    """Greedy word wrap into lines of graphemes; hard newlines are kept."""
    lines: list[list[str]] = []
    for para in text.split("\n"):
        g = graphemes(para)
        cur: list[str] = []
        word: list[str] = []

        def flush_word():
            nonlocal cur, word
            if not word:
                return
            if len(cur) + len(word) > per_line and cur:
                lines.append(cur)
                cur = []
            while len(word) > per_line:
                lines.append(word[:per_line])
                word = word[per_line:]
            cur = cur + word
            word = []

        for ch in g:
            word.append(ch)
            if ch == " ":
                flush_word()
        flush_word()
        lines.append(cur)
    return lines


def fold_index(text: str, fold: dict) -> int | None:
    """Grapheme index where the feed cuts the caption behind "more", or None when it all shows.
    fold = {"unit": "chars", "at": 125} | {"unit": "lines", "at": 3, "chars_per_line": 52}."""
    g = graphemes(text)
    at = int(fold.get("at", 0))
    if fold.get("unit") == "lines":
        lines = _wrap_lines(text, int(fold.get("chars_per_line", 50)))
        if len(lines) <= at:
            return None
        idx = 0                                         # walk the grapheme list: lines drop their hard newline
        for ln in lines[:at]:
            idx += len(ln)
            if idx < len(g) and g[idx] == "\n":
                idx += 1
        return idx if idx < len(g) else None
    return at if len(g) > at else None


def visible_fold(text: str, fold: dict) -> dict:
    """{"visible", "hidden", "folded"}: what shows before "more" and what is behind it."""
    cut = fold_index(text, fold)
    g = graphemes(text)
    if cut is None:
        return {"visible": text, "hidden": "", "folded": False}
    return {"visible": "".join(g[:cut]).rstrip(), "hidden": "".join(g[cut:]), "folded": True}


def ig_visible_fold(text: str, at: int = IG_FOLD_CHARS) -> dict:
    return visible_fold(text, {"unit": "chars", "at": at})
