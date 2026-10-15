"""Authenticity critic: the creator's taste as data (config/assets.critic.yaml), applied to what the agent actually SAW.

Code does only what code can do honestly: match the cliche lists against text, enforce the arithmetic and hard gates,
check pacing rules on an edit. It does NOT guess visual quality: the six criteria are scored 0-2 by the agent from a
contact sheet, and an unscored candidate cannot pass. Every verdict carries reasons and a confidence for the audit trail.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from .. import config as ave_config

DEFAULT_PATH = ave_config.REPO_ROOT / "config" / "assets.critic.yaml"


def load_config(path: Path | str | None = None, ep=None) -> dict:
    """Resolution: explicit path > <episode>/policy/assets.critic.yaml > ~/.config/ave/assets.critic.yaml > default."""
    cands = [Path(path)] if path else []
    if ep is not None:
        root = Path(ep.root) if hasattr(ep, "meta_path") else Path(ep)
        cands.append(root / "policy" / "assets.critic.yaml")
    cands += [Path.home() / ".config" / "ave" / "assets.critic.yaml", DEFAULT_PATH]
    for c in cands:
        if c.is_file():
            cfg = yaml.safe_load(c.read_text())
            cfg["_path"] = str(c)
            return cfg
    raise FileNotFoundError(f"no critic config found (looked for {DEFAULT_PATH})")


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 3 and w.endswith("s") else w


def _tokens(text: str) -> list[str]:
    return [_stem(w) for w in re.findall(r"[a-z0-9]+", str(text).lower())]


def _entry_tokens(entry) -> tuple[list[str], bool]:
    """(tokens, ordered): a string is an ordered phrase, a list means all words in any order."""
    if isinstance(entry, str):
        return _tokens(entry), True
    return [t for w in entry for t in _tokens(w)], False


def _hit(entry, toks: list[str]) -> bool:
    need, ordered = _entry_tokens(entry)
    if not need:
        return False
    if ordered:
        return any(toks[i:i + len(need)] == need for i in range(len(toks) - len(need) + 1))
    return set(need) <= set(toks)


def _label(entry) -> str:
    return entry if isinstance(entry, str) else " + ".join(entry)


def find_cliches(text: str, cfg: dict) -> dict:
    toks = _tokens(text)
    return {"reject": [_label(e) for e in cfg.get("cliche_reject", []) if _hit(e, toks)],
            "discourage": [_label(e) for e in cfg.get("cliche_discourage", []) if _hit(e, toks)]}


def _candidate_texts(c: dict) -> dict[str, str]:
    out = {}
    for k in ("query", "title", "notes", "description"):
        if c.get(k):
            out[k] = str(c[k])
    if c.get("tags"):
        out["tags"] = " , ".join(c["tags"]) if isinstance(c["tags"], list) else str(c["tags"])
    if c.get("filename"):
        out["filename"] = Path(str(c["filename"])).stem.replace("-", " ").replace("_", " ")
    return out


def critique(c: dict, cfg: dict | None = None) -> dict:
    """Pass/fail for one B-roll candidate. See module docstring; `c` shape is documented in the ave-broll-music skill."""
    cfg = cfg or load_config()
    sc = cfg["scoring"]
    crit = cfg["criteria"]
    top = sc["max_per_criterion"]
    hard, warn, reasons = [], [], []
    conf_pen = 0.0

    for field, text in _candidate_texts(c).items():
        hits = find_cliches(text, cfg)
        for h in hits["reject"]:
            hard.append(f"cliche in {field}: '{h}' (auto-reject list)")
        for h in hits["discourage"]:
            warn.append(f"discouraged in {field}: '{h}'; argue for it in notes or pick something more specific")
            conf_pen += 0.1

    scores, evidence = {}, 0
    for k, v in (c.get("scores") or {}).items():
        if k not in crit:
            raise ValueError(f"unknown criterion '{k}' (expected: {', '.join(crit)})")
        ev = None
        if isinstance(v, dict):
            v, ev = v.get("score"), v.get("evidence")
        if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= top:
            raise ValueError(f"score for {k} must be an integer 0-{top}, got {v!r}")
        scores[k] = v
        evidence += bool(ev)

    if c.get("has_audio") is False and scores.get("ambient_sound", 0) > sc["silent_ambient_cap"]:
        scores["ambient_sound"] = sc["silent_ambient_cap"]
        warn.append("silent clip: ambient_sound capped; lay honest room tone or outdoor ambience under it")

    missing = [k for k in crit if k not in scores]
    if missing and sc.get("require_all_scores", True):
        hard.append("not visually judged: score every criterion from the contact sheet (missing: " + ", ".join(missing) + ")"
                    if not scores else "missing scores: " + ", ".join(missing))

    total = sum(scores.values())
    maxtotal = top * len(crit)
    if not missing:
        if total < sc["pass_total"]:
            hard.append(f"total {total}/{maxtotal} is below the pass mark {sc['pass_total']}")
        for g in sc.get("hard_gate_zero", []):
            if scores.get(g) == 0:
                if c.get("release") is True:
                    warn.append(f"{g} scored 0 but a release is documented: verify the paperwork before use")
                else:
                    hard.append("identifiable face or logo with no release on record (hard gate): " + crit[g]["label"])
        zeros = [k for k, v in scores.items() if v == 0 and k not in sc.get("hard_gate_zero", [])]
        if len(zeros) > sc["weak_zero_limit"]:
            hard.append(f"{len(zeros)} criteria scored zero ({', '.join(zeros)}); more than {sc['weak_zero_limit']} weak spot fails")
        for k, v in scores.items():
            reasons.append(f"{crit[k]['label']}: {v}/{top}" + (f" ({c['scores'][k]['evidence']})" if isinstance(c["scores"][k], dict) and c["scores"][k].get("evidence") else ""))

    dur = c.get("duration")
    if dur is not None and dur < sc["min_candidate_duration_s"]:
        hard.append(f"clip is shorter than the shortest legal B-roll hold ({sc['min_candidate_duration_s']}s)")

    cliche = any(h.startswith("cliche") for h in hard)
    if cliche:
        confidence = 0.95
    else:
        confidence = 0.55 + 0.3 * (evidence / max(1, len(crit))) + (0.1 if c.get("looked_at") else 0) - conf_pen
        confidence = round(max(0.2, min(0.95, confidence)), 2)
    ok = not hard
    return {"id": c.get("id"), "pass": ok, "total": total, "max": maxtotal, "scores": scores, "reasons": reasons or
            (["auto-rejected before scoring"] if cliche else []), "hard_fails": hard, "warnings": warn, "confidence": confidence,
            "reason": ("passes the authenticity checklist" if ok else "; ".join(hard))}


# ---- placement and mix rules (pacing) ---------------------------------------------------------------------------

def _overlap(a0, a1, b0, b1) -> bool:
    return a0 < b1 and b0 < a1


def check_placements(placements: list[dict], duration: float, beats: list[dict], cfg: dict | None = None,
                     music: list[dict] | None = None, punchlines: list[dict] | None = None) -> dict:
    """Check B-roll placements and music against config `pacing`. Pure: pass what the EditDoc and the human flagged.

    placements: [{id,o0,o1}] output time. beats: [{t0,t1,label}] emotional moments B-roll must never cover.
    music: [{id,o0,o1,gain_db,...}]. punchlines: [{t}] moments that need silence after them.
    """
    p = (cfg or load_config())["pacing"]
    v = []
    pl = sorted(placements, key=lambda x: x["o0"])
    for i, b in enumerate(pl):
        d = b["o1"] - b["o0"]
        if d < p["broll_min_s"] - 1e-9 or d > p["broll_max_s"] + 1e-9:
            v.append({"rule": "length", "id": b["id"], "why": f"{d:.2f}s is outside {p['broll_min_s']}-{p['broll_max_s']}s"})
        for bt in beats or []:
            if _overlap(b["o0"], b["o1"], bt["t0"], bt["t1"]):
                v.append({"rule": "emotional_beat", "id": b["id"], "why": f"covers an emotional beat ({bt.get('label', 'unlabeled')} at {bt['t0']:.1f}s): let the face carry it"})
        if i and b["o0"] - pl[i - 1]["o1"] < p["min_gap_between_broll_s"] - 1e-9:
            v.append({"rule": "gap", "id": b["id"], "why": f"only {b['o0'] - pl[i - 1]['o1']:.2f}s after the previous insert (min {p['min_gap_between_broll_s']}s)"})
    covered = sum(b["o1"] - b["o0"] for b in pl)
    cov = covered / duration if duration else 0.0
    if cov > p["max_coverage"] + 1e-9:
        v.append({"rule": "coverage", "id": None, "why": f"B-roll covers {cov:.0%} of the programme; max {p['max_coverage']:.0%} (it is voice-led)"})
    lo, hi = p["music_under_dialogue_db"]
    for m in music or []:
        g = m.get("gain_db", 0)
        if g > hi:
            v.append({"rule": "music_level", "id": m["id"], "why": f"bed at {g} dB is too loud under speech (target {lo} to {hi} dB)"})
        elif g < lo:
            v.append({"rule": "music_level", "id": m["id"], "why": f"bed at {g} dB is too quiet to register (target {lo} to {hi} dB)"})
    sil = p["silence_after_punchline_s"]
    for pn in punchlines or []:
        t = pn["t"]
        for m in music or []:
            if m["o0"] <= t and m["o1"] >= t + sil:
                v.append({"rule": "silence_after_punchline", "id": m["id"], "why": f"music plays through the punchline at {t:.1f}s; drop it out for {sil}s"})
        for b in pl:
            if t <= b["o0"] < t + sil:
                v.append({"rule": "silence_after_punchline", "id": b["id"], "why": f"insert starts {b['o0'] - t:.2f}s after the punchline at {t:.1f}s; let it land"})
    return {"ok": not v, "violations": v, "coverage": round(cov, 4), "broll_count": len(pl), "j_l_cuts": p.get("j_l_cuts", True)}
