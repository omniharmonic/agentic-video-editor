"""Append-only license log: the legal record of every asset that enters an episode.

WHY a hash chain: the log is the evidence if a licence is ever questioned. Each line stores the hash of the previous
line, so editing or deleting history is detectable by `verify()` (deleting only the newest line is not; keep the file
in version control or back it up). Entries are refused unless complete: an asset without a licence trail must not exist.

`PROVIDER_TERMS` are our paraphrased snapshots of each provider's terms, taken 2026-10-13 from memory of the public
pages. They are marked `verified: False`: re-read the live terms before relying on them and update the snapshot.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
from pathlib import Path

GENESIS = "0" * 64
REQUIRED = ("source", "url", "license", "license_url", "license_text", "sha256", "approved_by", "file")

PROVIDER_TERMS = {
    "pexels": {
        "license": "Pexels License",
        "license_url": "https://www.pexels.com/license/",
        "snapshot_date": "2026-10-13",
        "verified": False,
        "text": ("Free to use for commercial and non-commercial projects; attribution is not required but appreciated; "
                 "may be modified. Not allowed: selling unaltered copies, implying endorsement by depicted people or "
                 "brands, showing identifiable people in a bad light or in a way that offends, or using trademarks/logos "
                 "visible in the clip as if they endorsed you. API terms: credit Pexels and the creator where the API is "
                 "used; do not systematically download the library."),
        "attribution_required": False,
        "restrictions": [
            "Identifiable people: do not portray them in a sensitive or negative context; no model release is guaranteed.",
            "Visible logos or trademarks are not cleared for commercial endorsement use.",
            "Do not resell or redistribute the unaltered file as a stock asset.",
        ],
    },
    "pixabay": {
        "license": "Pixabay Content License",
        "license_url": "https://pixabay.com/service/license-summary/",
        "snapshot_date": "2026-10-13",
        "verified": False,
        "text": ("Free for commercial and non-commercial use without attribution; may be modified. Not allowed: selling or "
                 "distributing the unaltered content, implying endorsement by depicted people or brands, depicting "
                 "identifiable people in a bad light, and using content with visible logos/trademarks/recognizable "
                 "people/property without the needed releases. API terms: do not hotlink (download and host files "
                 "yourself), cache search results for 24 hours, show Pixabay as the source where results are displayed."),
        "attribution_required": False,
        "restrictions": [
            "Identifiable people, logos and property are not guaranteed to be released.",
            "Do not hotlink; files must be downloaded (we do).",
            "Do not resell the unaltered file.",
        ],
    },
}

_SECRET = re.compile(r"([?&](?:key|api_key|apikey|token|access_token)=)[^&\s\"']+", re.I)


def redact(value):
    """Strip API keys from any URL-bearing string, recursively. Keys must never reach the log or cache."""
    if isinstance(value, str):
        return _SECRET.sub(r"\1REDACTED", value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def sha256_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def entry_hash(entry: dict) -> str:
    return hashlib.sha256(json.dumps(entry, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class LicenseLog:
    """`<episode>/work/assets/licenses.jsonl`. Only `append` writes; there is no update or delete."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text().splitlines() if x.strip()]

    def append(self, entry: dict) -> dict:
        e = redact(dict(entry))
        missing = [k for k in REQUIRED if not str(e.get(k) or "").strip()]
        if missing:
            raise ValueError(f"license entry incomplete, missing: {', '.join(missing)}")
        e.setdefault("at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)  # two agents appending at once must not fork the chain
            f.seek(0)
            lines = [x for x in f.read().splitlines() if x.strip()]
            e["prev"] = entry_hash(json.loads(lines[-1])) if lines else GENESIS
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return e

    def verify(self) -> list[str]:
        """Problems found in the chain and in each entry's completeness (empty list = intact)."""
        problems, prev = [], GENESIS
        for i, e in enumerate(self.entries(), 1):
            if e.get("prev") != prev:
                problems.append(f"line {i}: chain broken (history edited or a line was removed)")
            miss = [k for k in REQUIRED if not str(e.get(k) or "").strip()]
            if miss:
                problems.append(f"line {i}: incomplete entry, missing {', '.join(miss)}")
            prev = entry_hash(e)
        return problems

    def find(self, **match) -> dict | None:
        for e in reversed(self.entries()):
            if all(e.get(k) == v for k, v in match.items()):
                return e
        return None


def ep_root(ep) -> Path:
    """Accept an Episode or a path. (Path.root exists and is '/', so test for Episode explicitly.)"""
    return Path(ep.root) if hasattr(ep, "meta_path") else Path(ep)


def work_dir(ep) -> Path:
    """The work folder of an Episode (layout-aware); a bare path means the legacy flat layout."""
    return ep.work if hasattr(ep, "meta_path") else Path(ep) / "work"


def log_for(ep) -> LicenseLog:
    return LicenseLog(work_dir(ep) / "assets" / "licenses.jsonl")


def attribution_lines(entries: list[dict], include_optional: bool = False) -> list[str]:
    """Credit lines for the episode description. Required ones always; courtesy credits when asked."""
    out, seen = [], set()
    for e in entries:
        a = e.get("attribution") or {}
        if a.get("text") and (a.get("required") or include_optional) and a["text"] not in seen:
            seen.add(a["text"])
            out.append(a["text"])
    return out


def coverage(ep, files: list[str]) -> dict:
    """For each file an edit uses, is there a log entry for exactly these bytes? Returns {covered, missing, unreadable}."""
    root = ep_root(ep)
    log = log_for(ep)
    covered, missing, unreadable = [], [], []
    for rel in files:
        p = Path(rel) if Path(rel).is_absolute() else root / rel
        if not p.is_file():
            unreadable.append(rel)
            continue
        e = log.find(sha256=sha256_file(p))
        (covered if e else missing).append({"file": rel, **({"license": e["license"], "source": e["source"], "entry": e} if e else {})})
    return {"covered": covered, "missing": missing, "unreadable": unreadable}
