"""The show's clip taste: a human-readable profile, exemplars for the judging agent, a hit-rate report, and (human-run) weight changes.

The profile is `taste/clips.yaml` (per show, meant to live in git):

    version: 1
    weights: {hook: 30, ...}      # overrides config/clips.rubric.yaml weights; ONLY `ave learn taste apply` writes this
    exemplars: {keep: [...], skip: [...]}   # hand-curated, listed before the recent ledger ones
    exemplar_cap: 5               # per verdict
    min_decisions: 40             # `suggest` refuses to speak with fewer decisions than this
    notes: ""

Where it lives: AVE_TASTE_DIR, else `<workspace>/taste/` when the episode sits directly in the workspace (the workspace IS the show),
else `~/.config/ave/taste/`. Nothing here ever changes weights on its own: `report` and `suggest` only read.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from . import learning, workspace
from .episode import Episode, read_json

PROFILE = "clips.yaml"
MIN_DECISIONS = 40
EXEMPLAR_CAP = 5
HIGH_LOW_MIN_GROUP = 8        # a criterion needs this many decided clips on each side of its median to say anything
NOISE_LIFT = 0.10             # a keep-rate difference smaller than this is treated as noise
MAX_STEP = 0.25               # a weight moves by at most +-25% per apply


# ---- profile -----------------------------------------------------------------------------------------------

def taste_dir(ep: Episode | None = None) -> Path:
    if os.environ.get("AVE_TASTE_DIR"):
        return Path(os.environ["AVE_TASTE_DIR"]).expanduser()
    ws = workspace.get()
    if ws and ep is not None and ep.root.resolve().parent == ws.resolve():
        return ws / "taste"
    return Path(os.environ.get("AVE_CONFIG_DIR", Path.home() / ".config/ave")) / "taste"


def taste_path(ep: Episode | None = None, explicit: str | Path | None = None) -> Path:
    return Path(explicit).expanduser() if explicit else taste_dir(ep) / PROFILE


def load_taste(ep: Episode | None = None, explicit: str | Path | None = None) -> dict:
    import yaml
    p = taste_path(ep, explicit)
    if not p.is_file():
        return {}
    data = yaml.safe_load(p.read_text()) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"{p}: the taste profile must be a mapping (weights, exemplars, notes).")
    return data


def apply_to_rubric(ep: Episode, rubric: dict) -> None:
    """Overlay the profile's weights on a loaded rubric (only criteria the rubric already has; positive numbers only)."""
    t = load_taste(ep)
    w = t.get("weights") or {}
    used = {k: float(v) for k, v in w.items() if k in rubric["weights"] and isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0}
    if used:
        rubric["weights"] = {**rubric["weights"], **used}
        rubric["_taste"] = {"path": str(taste_path(ep)), "weights": used}


# ---- the ledgers in scope ----------------------------------------------------------------------------------

def episodes_in_scope(ep: Episode, also: list[str | Path] | None = None) -> list[Episode]:
    """This episode, its siblings in the workspace (the show), and any `also`. Each once."""
    out, seen = [], set()

    def add(e: Episode) -> None:
        key = str(e.root.resolve())
        if key not in seen:
            seen.add(key)
            out.append(e)
    add(ep)
    ws = workspace.get()
    if ws and ep.root.resolve().parent == ws.resolve():
        for d in workspace.episodes():
            add(Episode(d))
    for a in also or []:
        add(Episode(workspace.resolve_episode(a)))
    return out


def decisions(eps: list[Episode]) -> list[dict]:
    """Latest decision per (episode, clip), oldest first."""
    rows = []
    for e in eps:
        for rec in learning.latest_decisions(e).values():
            rows.append({**rec, "_ep": e.root.name, "_root": str(e.root)})
    rows.sort(key=lambda r: (r.get("ts") or "", r["clip_id"]))
    return rows


def _quote(root: str, clip_id: str) -> str:
    try:
        from . import clips as C
        data = C.load_clips(Episode(root)) or {}
        c = next((c for c in data.get("clips", []) if c["id"] == clip_id), None)
        return (c["text"][:160] + ("..." if len(c["text"]) > 160 else "")) if c else ""
    except Exception:
        return ""


def exemplars(ep: Episode, cap: int | None = None) -> dict:
    """Recent keeps and skips (with reasons) for the judging agent, hand-curated ones first, `cap` per verdict."""
    t = load_taste(ep)
    cap = int(cap or t.get("exemplar_cap") or EXEMPLAR_CAP)
    out: dict[str, list] = {"keep": [], "skip": []}
    for v in ("keep", "skip"):
        out[v] = [dict(e) for e in ((t.get("exemplars") or {}).get(v) or []) if isinstance(e, dict)][:cap]
    rows = decisions(episodes_in_scope(ep))
    for v in ("keep", "skip"):
        for r in reversed([r for r in rows if r["verdict"] == v]):
            if len(out[v]) >= cap:
                break
            f = r.get("features") or {}
            out[v].append({"episode": r["_ep"], "clip_id": r["clip_id"], "score": f.get("score"), "tags": f.get("tags") or [],
                           "reasons": r.get("reasons") or [], "note": r.get("note"), "quote": _quote(r["_root"], r["clip_id"])})
    return out


# ---- report ------------------------------------------------------------------------------------------------

def _rate(k: int, n: int) -> float | None:
    return round(k / n, 3) if n else None


def build_report(rows: list[dict], top_n: int = 10) -> dict:
    """Hit rate of the proposer's ranking against the human's decisions. Pure: `rows` are latest decisions.

    precision_at_n = keeps among the N highest-scored DECIDED clips / N (maybe and skip both count as not kept).
    recall_at_n    = of everything the human kept, the share that was in my top N (the rest were buried below it).
    keep_rate      = keep / (keep + maybe + skip). Ties in score break by clip id so the report is reproducible."""
    counts = {"keep": 0, "maybe": 0, "skip": 0}
    for r in rows:
        counts[r["verdict"]] += 1
    decided = len(rows)
    ranked = sorted((r for r in rows if (r.get("features") or {}).get("score") is not None),
                    key=lambda r: (-r["features"]["score"], r["_ep"], r["clip_id"]))
    top = ranked[:top_n]
    tc = {v: sum(1 for r in top if r["verdict"] == v) for v in counts}
    keeps = [r for r in ranked if r["verdict"] == "keep"]
    keeps_in_top = sum(1 for r in top if r["verdict"] == "keep")
    ids = {id(r): n + 1 for n, r in enumerate(ranked)}
    reasons: dict[str, dict] = {}
    for r in rows:
        for why in r.get("reasons") or []:
            d = reasons.setdefault(why, {"keep": 0, "maybe": 0, "skip": 0, "total": 0})
            d[r["verdict"]] += 1
            d["total"] += 1
    tags: dict[str, dict] = {}
    for r in rows:
        for t in (r.get("features") or {}).get("tags") or []:
            d = tags.setdefault(t, {"n": 0, "keep": 0, "maybe": 0, "skip": 0})
            d["n"] += 1
            d[r["verdict"]] += 1
    for d in tags.values():
        d["keep_rate"] = _rate(d["keep"], d["n"])
    return {"decided": decided, "counts": counts, "keep_rate": _rate(counts["keep"], decided),
            "top_n": {"n": len(top), "requested": top_n, **tc, "precision": _rate(tc["keep"], len(top)),
                      "recall": _rate(keeps_in_top, len(keeps)), "kept_total": len(keeps)},
            "buried_keeps": [{"episode": r["_ep"], "clip_id": r["clip_id"], "score": r["features"]["score"], "rank": ids[id(r)]}
                             for r in keeps if ids[id(r)] > top_n],
            "high_scored_skips": [{"episode": r["_ep"], "clip_id": r["clip_id"], "score": r["features"]["score"], "rank": ids[id(r)],
                                   "reasons": r.get("reasons") or []} for r in top if r["verdict"] == "skip"],
            "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1]["total"])),
            "tags": dict(sorted(tags.items(), key=lambda kv: -kv[1]["n"]))}


def format_report(rep: dict) -> str:
    c = rep["counts"]
    if not rep["decided"]:
        return "No decisions yet. Decide clips (`ave clips decide` or the review page) and run this again."
    pct = lambda x: "--" if x is None else f"{x * 100:.0f}%"      # noqa: E731
    t = rep["top_n"]
    lines = [f"Decisions: {rep['decided']}  (keep {c['keep']}, maybe {c['maybe']}, skip {c['skip']})  keep rate {pct(rep['keep_rate'])}",
             f"Top {t['n']} by score: keep {t['keep']}, maybe {t['maybe']}, skip {t['skip']}  ->  precision {pct(t['precision'])}; "
             f"of the {t['kept_total']} clips you kept, {pct(t['recall'])} were in my top {t['requested']}"]
    if rep["buried_keeps"]:
        lines.append("Kept but ranked below the top: " + ", ".join(f"{b['clip_id']} (rank {b['rank']}, score {b['score']:g})" for b in rep["buried_keeps"][:10]))
    if rep["high_scored_skips"]:
        lines.append("Top-ranked but skipped: " + ", ".join(f"{b['clip_id']} (rank {b['rank']}, {'/'.join(b['reasons']) or 'no reason'})" for b in rep["high_scored_skips"]))
    if rep["reasons"]:
        lines += ["", "Reasons given:"] + [f"  {k:<16} {v['total']:>3}  (keep {v['keep']}, maybe {v['maybe']}, skip {v['skip']})" for k, v in rep["reasons"].items()]
    if rep["tags"]:
        lines += ["", "Keep rate by tag:"] + [f"  {k:<10} {pct(v['keep_rate']):>4}  ({v['keep']} of {v['n']})" for k, v in rep["tags"].items()]
    return "\n".join(lines)


def report(ep: Episode, top_n: int = 10, also: list[str | Path] | None = None) -> dict:
    return build_report(decisions(episodes_in_scope(ep, also)), top_n)


# ---- suggest / apply ---------------------------------------------------------------------------------------

def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def build_suggestion(rows: list[dict], weights: dict[str, float], min_decisions: int = MIN_DECISIONS,
                     reasons: dict[str, dict] | None = None) -> dict:
    """Explainable weight changes from per-feature keep-rate lift.

    For each rubric criterion the decided clips are split at the criterion's median score (high = at or above, low = below);
    lift = keep rate of the high half minus keep rate of the low half. A criterion moves only if both halves hold at least
    HIGH_LOW_MIN_GROUP clips and |lift| >= NOISE_LIFT: new weight = old * (1 + 0.5 * clamp(lift, -0.5, 0.5)), so at most +-25% per apply.
    Fewer than `min_decisions` decisions and it declines to guess."""
    n = len(rows)
    if n < min_decisions:
        return {"enough_data": False, "decided": n, "needed": min_decisions,
                "message": f"Not enough data yet: {n} of {min_decisions} decisions. Keep deciding clips; nothing is suggested before that."}
    overall = _rate(sum(1 for r in rows if r["verdict"] == "keep"), n)
    changes, features, notes = {}, {}, []
    for crit, w in weights.items():
        have = [(r["features"]["rubric"][crit], r["verdict"] == "keep") for r in rows if crit in ((r.get("features") or {}).get("rubric") or {})]
        if len(have) < 2 * HIGH_LOW_MIN_GROUP:
            features[crit] = {"status": "too few decisions carry this criterion", "n": len(have)}
            continue
        med = _median([v for v, _ in have])
        hi, lo = [k for v, k in have if v >= med], [k for v, k in have if v < med]
        if len(hi) < HIGH_LOW_MIN_GROUP or len(lo) < HIGH_LOW_MIN_GROUP:
            features[crit] = {"status": "scores too uniform to compare", "median": med, "high_n": len(hi), "low_n": len(lo)}
            continue
        hr, lr = sum(hi) / len(hi), sum(lo) / len(lo)
        lift = round(hr - lr, 3)
        entry = {"median": round(med, 3), "high_n": len(hi), "low_n": len(lo), "keep_rate_high": round(hr, 3), "keep_rate_low": round(lr, 3), "lift": lift}
        if abs(lift) >= NOISE_LIFT:
            new = max(1, round(w * (1 + 0.5 * max(-0.5, min(0.5, lift)))))
            if new != round(w):
                entry["weight"] = {"from": w, "to": new}
                changes[crit] = new
                entry["why"] = (f"clips scoring high on {crit} are kept {hr * 100:.0f}% of the time vs {lr * 100:.0f}% when low "
                                f"({'raise' if new > w else 'lower'} the weight)")
        features[crit] = entry
    if reasons:
        vocab = learning.load_reasons()
        nonkeep = sum(1 for r in rows if r["verdict"] != "keep")
        for why, d in reasons.items():
            feat = (vocab.get(why) or {}).get("feature")
            nk = d["maybe"] + d["skip"]
            if feat and feat in weights and nonkeep and nk >= 5 and nk / nonkeep >= 0.2:
                notes.append(f"{why} explains {nk} of {nonkeep} non-keeps; it points at the `{feat}` criterion (not changed automatically).")
    return {"enough_data": True, "decided": n, "keep_rate": overall, "features": features, "weights": {**weights, **changes},
            "changes": {k: {"from": weights[k], "to": v} for k, v in changes.items()}, "notes": notes,
            "next": "ave learn taste apply <episode>   # human only: writes these weights to the taste profile" if changes else "No weight clears the noise floor."}


def suggest(ep: Episode, also: list[str | Path] | None = None, taste: str | Path | None = None) -> dict:
    from . import clips as C
    rows = decisions(episodes_in_scope(ep, also))
    rub = C.load_rubric(ep)
    t = load_taste(ep, taste)
    return build_suggestion(rows, dict(rub["weights"]), int(t.get("min_decisions") or MIN_DECISIONS), build_report(rows)["reasons"])


def apply_suggestion(ep: Episode, also: list[str | Path] | None = None, taste: str | Path | None = None, dry_run: bool = False) -> dict:
    """Write the suggested weights to the taste profile. Human-run only (never called by the pipeline); refuses without enough data."""
    import yaml
    s = suggest(ep, also, taste)
    if not s["enough_data"]:
        raise SystemExit(s["message"])
    if not s["changes"]:
        return {"applied": False, "message": "No weight clears the noise floor; the profile is unchanged.", "suggestion": s}
    p = taste_path(ep, taste)
    data = load_taste(ep, taste) or {"version": 1}
    data["version"] = data.get("version", 1)
    data["weights"] = s["weights"]
    moved = ", ".join("%s %g->%g" % (k, v["from"], v["to"]) for k, v in s["changes"].items())
    note = f"{time.strftime('%Y-%m-%d')}: weights applied from {s['decided']} decisions ({moved})"
    data["notes"] = ((data.get("notes") or "").rstrip() + "\n" + note).strip()
    if not dry_run:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# Clip taste for this show. `weights` is written only by `ave learn taste apply`; edit exemplars/notes freely.\n"
                     + yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return {"applied": not dry_run, "dry_run": dry_run, "path": str(p), "changes": s["changes"], "weights": s["weights"]}
