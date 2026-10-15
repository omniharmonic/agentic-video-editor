"""Thin adapter over the clip-decision ledger (spec: `<ep>/work/learning/clip_decisions.jsonl`).

Workstream L owns `ave.learning` (`record_decision`, `latest_decisions`, `iter_decisions`). The UI codes against those names;
if the module is not importable yet, a minimal local implementation writes the SAME jsonl schema (v1), so the files are
interchangeable. Latest line per clip_id wins.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from ..episode import Episode

VERDICTS = ("keep", "maybe", "skip")
DEFAULT_REASONS = ["weak_ending", "hook_late", "off_topic", "too_long", "too_short", "needs_context", "great_but_long", "other"]

try:                                     # pragma: no cover - exercised once workstream L is merged
    from .. import learning as _learning
except Exception:
    _learning = None


def backend() -> str:
    return "ave.learning" if _learning is not None else "local fallback"


def ledger_path(ep: Episode) -> Path:
    return ep.work / "learning" / "clip_decisions.jsonl"


def reasons_vocabulary() -> list[str]:
    try:
        import yaml
        from .. import config
        data = yaml.safe_load((config.REPO_ROOT / "config" / "clip_reasons.yaml").read_text()) or {}
        raw = data.get("reasons", data) if isinstance(data, dict) else data
        items = list(raw.keys()) if isinstance(raw, dict) else list(raw)
        names = [r if isinstance(r, str) else (r.get("id") if isinstance(r, dict) else None) for r in items]
        names = [n for n in names if isinstance(n, str)]
        return names or DEFAULT_REASONS
    except Exception:
        return DEFAULT_REASONS


def reason_labels() -> dict[str, dict]:
    """id -> {label, verdicts} for the chips. Falls back to a humanised id when the yaml has no label."""
    out: dict[str, dict] = {}
    try:
        import yaml
        from .. import config
        data = yaml.safe_load((config.REPO_ROOT / "config" / "clip_reasons.yaml").read_text()) or {}
        raw = data.get("reasons", data) if isinstance(data, dict) else data
        for r in raw if isinstance(raw, list) else []:
            if isinstance(r, dict) and isinstance(r.get("id"), str):
                out[r["id"]] = {"label": r.get("label") or r["id"].replace("_", " "), "verdicts": r.get("verdicts") or list(VERDICTS)}
    except Exception:
        pass
    for rid in reasons_vocabulary():
        out.setdefault(rid, {"label": rid.replace("_", " ").capitalize(), "verdicts": list(VERDICTS)})
    return out


def _features(clip: dict) -> dict:
    text = clip.get("text") or ""
    return {"score": clip.get("total"), "rubric": clip.get("weighted") or {}, "duration_s": clip.get("duration"),
            "tags": clip.get("tags") or [], "speaker_balance": None, "hook_pos_s": None, "ends_on_sentence": None,
            "text_sha": hashlib.sha256(text.encode()).hexdigest()[:16]}


def _read_lines(ep: Episode) -> list[dict]:
    p = ledger_path(ep)
    out = []
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue                      # a torn line must not hide the rest of the history
    return out


def iter_decisions(ep: Episode) -> list[dict]:
    if _learning is not None:
        return list(_learning.iter_decisions(ep))
    return _read_lines(ep)


def latest_decisions(ep: Episode) -> dict[str, dict]:
    if _learning is not None:
        return dict(_learning.latest_decisions(ep))
    return {d["clip_id"]: d for d in _read_lines(ep) if "clip_id" in d}


def _edit_label(clip: dict) -> str | None:
    s = clip.get("source")
    if not s:
        return None
    return f"{s['stage']}{'.' + s['name'] if s.get('name') else ''}.v{s['version']}"


def record_decision(ep: Episode, clip: dict, verdict: str, reasons=(), note: str | None = None, by: str = "", source: str = "ui") -> dict | None:
    """Append one decision. `clip` is the clips.json entry (the fallback snapshots its features from it)."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}")
    reasons = [r for r in reasons if isinstance(r, str)]
    if _learning is not None:
        return _learning.record_decision(ep, clip["id"], verdict, reasons=tuple(reasons), note=note, by=by, source=source)
    rec = {"v": 1, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "episode": ep.root.name, "clip_id": clip["id"],
           "session": clip.get("session"), "edit": _edit_label(clip), "verdict": verdict, "reasons": reasons,
           "note": note or None, "by": by or None, "source": source, "features": _features(clip)}
    p = ledger_path(ep)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec
