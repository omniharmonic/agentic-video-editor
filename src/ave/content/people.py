"""Who is speaking, and how a graphic names them: the byline (name, then title beneath it).

`episode.json` `speakers` carry, per speaker id (`S1`):
  name          the name used in the transcript and by default on graphics ("Maren")
  display_name  optional: the name to print on graphics when it differs from `name`
  full_name     optional: only ever used to MATCH an attribution ("Maren Okafor" finds Maren); never printed unless it is also
                set as display_name. Nothing here invents a surname.
  title         optional: printed beneath the name. Absent = "CO-HOST" for a host, nothing for a guest.
  guest         optional: true marks a guest (no default title)

A graphic prints the show name ONCE (the show mark at the top), then the speaker's name, then their title beneath the name.
"""

from __future__ import annotations

from typing import Any

from ..episode import Episode, read_json

DEFAULT_HOST_TITLE = "CO-HOST"


def _fold(s: Any) -> str:
    return " ".join(str(s or "").casefold().split())


def load_speakers(ep: Episode | None) -> list[dict]:
    """[{id, name, display_name, aliases, title}] from episode.json; [] when there is no episode or no speakers."""
    if ep is None:
        return []
    try:
        meta = read_json(ep.meta_path)
    except Exception:
        return []
    out = []
    for sid, s in (meta.get("speakers") or {}).items():
        if not isinstance(s, dict) or not s.get("name"):
            continue
        guest = bool(s.get("guest")) or _fold(s.get("role")) == "guest"
        title = s["title"] if "title" in s else ("" if guest else DEFAULT_HOST_TITLE)
        shown = str(s.get("display_name") or s["name"]).strip()
        out.append({"id": sid, "name": str(s["name"]).strip(), "display_name": shown, "title": str(title or "").strip(),
                    "aliases": {_fold(x) for x in (sid, s["name"], s.get("display_name"), s.get("full_name")) if x},
                    "first_names": {_fold(x).split(" ")[0] for x in (s["name"], s.get("display_name")) if x},
                    "full_name": str(s.get("full_name") or "").strip()})
    return out


def find(speakers: list[dict], attribution: str) -> dict | None:
    """The one speaker an attribution refers to: an exact alias ("Tomas", "maren okafor" when full_name says so) or the
    attribution's first word being a speaker's first name ("Maren Okafor" -> Maren). None when absent or ambiguous."""
    a = _fold(attribution)
    if not a:
        return None
    hits = [s for s in speakers if a in s["aliases"]]
    if not hits:
        hits = [s for s in speakers if a.split(" ")[0] in s["first_names"]]
    return hits[0] if len(hits) == 1 else None


def byline(speakers: list[dict], attribution: str, title: str = "") -> tuple[str, str]:
    """(name, title) to print. A matching speaker gives the display name and, when `title` is empty, the speaker's title.
    A title set by hand wins. No match: both come back unchanged."""
    s = find(speakers, attribution)
    if not s:
        return attribution, title
    return s["display_name"], title or s["title"]
