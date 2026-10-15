"""The decision ledger: every human verdict on a clip, append-only, with the clip's features at that moment.

Contract (docs/superpowers/specs/2026-10-14-clip-learning-and-ui-design.md): one JSON object per line in
`<work>/learning/clip_decisions.jsonl`; the latest line per `clip_id` wins, earlier lines are history. Nothing here edits
clips.json: `ave.clips.decide` is the single code path that writes the status AND calls `record_decision`.
"""

from __future__ import annotations

import getpass
import json
import os
import time
from pathlib import Path
from typing import Any, Iterator

from .config import REPO_ROOT
from .episode import Episode

LEDGER = "learning/clip_decisions.jsonl"
VERDICTS = ("keep", "maybe", "skip")
SOURCES = ("ui", "cli")
SCHEMA_VERSION = 1
REASONS_FILE = "clip_reasons.yaml"


def ledger_path(ep: Episode) -> Path:
    return ep.artifact(LEDGER)


# ---- reasons vocabulary ------------------------------------------------------------------------------------

def load_reasons(ep: Episode | None = None) -> dict[str, dict]:
    """id -> {id, label, feature}. config/clip_reasons.yaml, then ~/.config/ave, then <episode>/policy merged over it by id."""
    import yaml
    layers = [REPO_ROOT / "config" / REASONS_FILE,
              Path(os.environ.get("AVE_CONFIG_DIR", Path.home() / ".config/ave")) / REASONS_FILE]
    if ep is not None:
        layers.append(ep.root / "policy" / REASONS_FILE)
    out: dict[str, dict] = {}
    for p in layers:
        if not p.is_file():
            continue
        for r in (yaml.safe_load(p.read_text()) or {}).get("reasons") or []:
            if isinstance(r, dict) and r.get("id"):
                out[str(r["id"])] = {"id": str(r["id"]), "label": r.get("label") or str(r["id"]).replace("_", " "),
                                     "feature": r.get("feature")}
    return out


def _by() -> str:
    for k in ("AVE_USER", "USER", "LOGNAME"):
        if os.environ.get(k):
            return os.environ[k]
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


# ---- write -------------------------------------------------------------------------------------------------

def record_decision(ep: Episode, clip_id: str, verdict: str, reasons: Any = (), note: str | None = None, by: str | None = None,
                    source: str = "cli", *, session: str | None = None, edit: str | None = None, features: dict | None = None) -> dict:
    """Append one decision. `session`, `edit` and `features` default to what clips.json holds for `clip_id`.

    Raises ValueError for an unknown verdict/source or a reason that is not in the vocabulary (the message lists the valid ones)."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(VERDICTS)}, got {verdict!r}")
    if source not in SOURCES:
        raise ValueError(f"source must be one of {', '.join(SOURCES)}, got {source!r}")
    if isinstance(reasons, str):
        reasons = [reasons]
    reasons = list(dict.fromkeys(str(r) for r in (reasons or ())))
    vocab = load_reasons(ep)
    bad = [r for r in reasons if r not in vocab]
    if bad:
        raise ValueError(f"unknown reason(s) {', '.join(bad)}; valid: {', '.join(vocab)} (extend config/clip_reasons.yaml)")
    data, clip = _find_clip(ep, clip_id)
    if clip is not None:
        session = session if session is not None else clip.get("session")
        edit = edit if edit is not None else edit_label(data, clip)
        features = features if features is not None else clip_features(clip)
    rec = {"v": SCHEMA_VERSION, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "episode": ep.root.name, "clip_id": clip_id,
           "session": session, "edit": edit, "verdict": verdict, "reasons": reasons, "note": (note or None),
           "by": by or _by(), "source": source, "features": features or {}}
    p = ledger_path(ep)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def _find_clip(ep: Episode, clip_id: str) -> tuple[dict, dict | None]:
    from . import clips as C
    data = C.load_clips(ep) or {}
    return data, next((c for c in data.get("clips", []) if c["id"] == clip_id), None)


def edit_label(data: dict, clip: dict) -> str | None:
    """`rough.s1.v2`-style label of the edit a clip was picked from (session clips record it; unnamed ones use the file's source)."""
    src = clip.get("source") or data.get("source")
    if not src:
        return None
    return f"{src['stage']}{'.' + src['name'] if src.get('name') else ''}.v{src['version']}"


def clip_features(clip: dict) -> dict:
    """The ledger's `features` for a clip as it is NOW (after any nudge)."""
    scores = (clip.get("inputs") or {}).get("scores") or {}
    return {"score": clip.get("total"), "rubric": {k: round(v / 10.0, 3) for k, v in scores.items()},
            "duration_s": clip.get("duration"), "tags": list(clip.get("tags") or []), "speaker_balance": clip.get("speaker_balance"),
            "hook_pos_s": clip.get("hook_pos_s"), "ends_on_sentence": clip.get("ends_on_sentence"), "text_sha": clip.get("text_sha")}


# ---- read --------------------------------------------------------------------------------------------------

def iter_decisions(ep: Episode) -> Iterator[dict]:
    """Every ledger line in order (history included). A damaged line is skipped, never fatal: the ledger is append-only."""
    p = ledger_path(ep)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("clip_id") and rec.get("verdict") in VERDICTS:
            yield rec


def latest_decisions(ep: Episode) -> dict[str, dict]:
    """clip_id -> its latest decision (the last line wins)."""
    out: dict[str, dict] = {}
    for rec in iter_decisions(ep):
        out[rec["clip_id"]] = rec
    return out
