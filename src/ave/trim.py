"""Trim: find the real start and end of a session (field note N8, ROADMAP W2).

A recording opens with setup talk ("okay, take one... we're recording") and ends with talk about stopping. The planner starts at the first
camera frame, so every episode needs a human-approved in/out. This module only PROPOSES: it scans the first and last minutes of a
session's transcript for phrases listed in config/trim.default.yaml and reports each hit as evidence, with a proposed in/out snapped into
a pause and a confidence. `set_trim` records the human's decision in episode.json; `assemble` honours it. Nothing here edits a cut.

Times are EPISODE time (the recorder's timeline), like the transcript.
"""

from __future__ import annotations

import copy
import time
from pathlib import Path

import yaml

from . import config, sessions
from .episode import Episode, read_json, write_json

DEFAULT_POLICY = config.REPO_ROOT / "config" / "trim.default.yaml"
SUGGESTIONS = "trim_suggestions.json"
MAIN = "main"                      # the trim key of an episode that has no sessions
SENT_END = ".!?"
SENTENCE_GAP_S = 1.0               # silence this long also ends a sentence ("Um ... [6 s] Yeah, so I think" is two sentences)


# ---- policy -----------------------------------------------------------------------------------------------------

def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def load_policy(path: Path | str | None = None) -> dict:
    base = yaml.safe_load(DEFAULT_POLICY.read_text())
    if path:
        base = _merge(base, yaml.safe_load(Path(path).read_text()) or {})
    for key in ("slate",):
        if any(not isinstance(x, str) for x in base[key]):
            raise ValueError(f"trim policy {key}: every phrase must be a string (quote yes/no)")
    if base["scan_minutes"] <= 0:
        raise ValueError("trim policy scan_minutes must be > 0")
    return base


def resolve_policy(ep: Episode, explicit: Path | str | None = None, home: Path | None = None) -> dict:
    """--policy > <ep>/policy/trim.yaml > ~/.config/ave/trim.yaml > default; then episode.json `trim_hints` extend the welcome/sign-off lists."""
    home = Path(home) if home else Path.home()
    pol = None
    if explicit:
        if not Path(explicit).is_file():
            raise FileNotFoundError(f"policy file not found: {explicit}")
        pol = load_policy(explicit)
    else:
        for cand in (ep.root / "policy" / "trim.yaml", home / ".config" / "ave" / "trim.yaml"):
            if cand.is_file():
                pol = load_policy(cand)
                break
    pol = pol or load_policy()
    hints = (read_json(ep.meta_path).get("trim_hints") or {}) if ep.meta_path.exists() else {}
    pol["welcome"]["strong"] = [*pol["welcome"]["strong"], *hints.get("welcome", [])]
    pol["signoff"]["strong"] = [*pol["signoff"]["strong"], *hints.get("signoff", [])]
    return pol


# ---- words and sentences ---------------------------------------------------------------------------------------------

def _norm(token: str) -> str:
    t = token.lower().replace("’", "'").replace("‘", "'")
    return "".join(c for c in t if c.isalnum() or c in "'-").strip("-'")


def _tokens(phrase: str) -> list[str]:
    return [n for n in (_norm(t) for t in phrase.split()) if n]


def sentences(words: list[dict]) -> list[dict]:
    """Sentences of the transcript words (sorted): they end at . ! ?, when the speaker changes, or after a silence of SENTENCE_GAP_S. {i0, i1, e0, e1, text, speaker, n}."""
    out, start = [], 0
    for i, w in enumerate(words):
        last = i + 1 == len(words)
        if (last or w["w"].rstrip().endswith(tuple(SENT_END)) or words[i + 1].get("speaker") != w.get("speaker")
                or words[i + 1]["start"] - w["end"] > SENTENCE_GAP_S):
            ws = words[start:i + 1]
            out.append({"i0": start, "i1": i, "e0": ws[0]["start"], "e1": ws[-1]["end"], "text": " ".join(x["w"] for x in ws),
                        "speaker": ws[0].get("speaker"), "n": len(ws)})
            start = i + 1
    return out


def _phrase_hits(words: list[dict], sents: list[dict], lo: float, hi: float, kind: str, phrases: list[str], short_only: bool = False,
                 short_words: int = 3) -> list[dict]:
    """Every place `phrases` occur as consecutive words (any speaker, words less than 2 s apart) inside [lo, hi]."""
    toks = [_norm(w["w"]) for w in words]
    sent_of = {}
    for si, s in enumerate(sents):
        for i in range(s["i0"], s["i1"] + 1):
            sent_of[i] = si
    out = []
    for ph in phrases:
        pt = _tokens(ph)
        if not pt:
            continue
        for i in range(len(words) - len(pt) + 1):
            if toks[i:i + len(pt)] != pt or not (lo <= words[i]["start"] <= hi):
                continue
            if any(words[k + 1]["start"] - words[k]["end"] > 2.0 for k in range(i, i + len(pt) - 1)):
                continue
            s = sents[sent_of[i]]
            if short_only and s["n"] > short_words:
                continue
            out.append({"kind": kind, "phrase": ph, "e0": round(words[i]["start"], 2), "e1": round(words[i + len(pt) - 1]["end"], 2),
                        "text": s["text"], "speaker": words[i].get("speaker") or "?", "sentence": sent_of[i]})
    return sorted(out, key=lambda h: (h["e0"], h["phrase"]))


def _gap_before(words: list[dict], i: int) -> float:
    """Silence between word i and everything just before it (the previous few words, any speaker); huge for the first word."""
    return words[i]["start"] - max(w["end"] for w in words[max(0, i - 6):i]) if i else 1e9


def _in_point(words: list[dict], i: int, pol: dict) -> float:
    """A cut `in` placed in the pause before word i: up to lead_s before it, never more than half the gap, never inside the previous word."""
    gap = _gap_before(words, i)
    return round(words[i]["start"] - min(pol["lead_s"], max(gap, 0.0) / 2.0), 3)


def _out_point(words: list[dict], i: int, pol: dict) -> float:
    """A cut `out` placed in the pause after word i: up to tail_s after it, never more than half the gap to the next word."""
    nxt = words[i + 1]["start"] if i + 1 < len(words) else words[i]["end"] + 2 * pol["tail_s"]
    return round(words[i]["end"] + min(pol["tail_s"], max(nxt - words[i]["end"], 0.0) / 2.0), 3)


# ---- the session span ----------------------------------------------------------------------------------------------

def session_key(ep: Episode, session: str | None) -> str:
    """The key trim is stored under: the session id, or `main` for an episode that is not split into sessions."""
    if session:
        return session
    if len(sessions.session_ids(ep)) > 1:
        raise SystemExit(f"This episode has sessions {sessions.session_ids(ep)}: say which with --session <id> (trim is per session).")
    return MAIN


def span(ep: Episode, session: str | None, words: list[dict]) -> tuple[float, float]:
    """Episode-time extent of the session: the cut list's regions when planned, else the transcript."""
    key = session_key(ep, session)
    p = ep.artifact(sessions.cutlist_name(None if key == MAIN else key))
    if p.exists():
        regs = read_json(p).get("regions") or []
        if regs:
            return float(min(a for a, _ in regs)), float(max(b for _, b in regs))
    return float(words[0]["start"]), float(words[-1]["end"])


# ---- suggest ------------------------------------------------------------------------------------------------------

def _propose_in(words, sents, hits, pol) -> tuple[float | None, float, str]:
    slate = [h for h in hits if h["kind"] == "slate"]
    strong = [h for h in hits if h["kind"] == "welcome"]
    weak = [h for h in hits if h["kind"] == "welcome_weak"]
    last_slate = max((h["e1"] for h in slate), default=None)
    after = [h for h in strong if last_slate is None or h["e0"] >= last_slate]
    if after:
        h = after[0]
        conf = 0.85 if slate else 0.7
        return _in_point(words, sents[h["sentence"]]["i0"], pol), conf, (
            f'the show opens at "{h["phrase"]}" ({h["e0"]:.1f}s)' + (f', after setup talk that ends at "{slate[-1]["phrase"]}" ({slate[-1]["e0"]:.1f}s)' if slate else ""))
    if slate:
        h = max(slate, key=lambda x: x["e1"])
        nxt = next((s for s in sents[h["sentence"] + 1:] if s["n"] >= pol["settle_min_words"]), None)
        if nxt:
            why = (f'first sentence of {pol["settle_min_words"]}+ words after the last slate phrase "{h["phrase"]}" ({h["e0"]:.1f}s)'
                   + (f'; a "welcome" at {weak[0]["e0"]:.1f}s sits inside the setup talk, so it is not taken as the start' if weak and weak[0]["e0"] < h["e0"] else ""))
            return _in_point(words, nxt["i0"], pol), 0.55, why
        return None, 0.0, f'setup phrase "{h["phrase"]}" at {h["e0"]:.1f}s but no long sentence follows it in the scanned minutes'
    if weak:
        h = weak[0]
        return _in_point(words, sents[h["sentence"]]["i0"], pol), 0.4, f'a bare "welcome" at {h["e0"]:.1f}s (no setup talk found around it)'
    return None, 0.0, "no slate or welcome phrase in the scanned minutes: the session may already start where you want"


def _propose_out(words, sents, hits, pol) -> tuple[float | None, float, str]:
    strong = [h for h in hits if h["kind"] == "signoff"]
    chatter = [h for h in hits if h["kind"] in ("stop", "slate")]
    if strong:
        h = max(strong, key=lambda x: x["e0"])
        return _out_point(words, sents[h["sentence"]]["i1"], pol), 0.85, f'the show ends with "{h["phrase"]}" ({h["e0"]:.1f}s); out is just after that sentence'
    if chatter:
        cl = [max(chatter, key=lambda x: x["e0"])]
        for h in sorted(chatter, key=lambda x: -x["e0"])[1:]:
            if cl[-1]["e0"] - h["e0"] <= pol["cluster_s"]:
                cl.append(h)
        first = min(cl, key=lambda x: x["e0"])
        i0 = sents[first["sentence"]]["i0"]
        return (round(words[i0 - 1]["end"] + min(pol["tail_s"], max(words[i0]["start"] - words[i0 - 1]["end"], 0.0) / 2.0), 3) if i0 else None,
                0.5, f'talk about stopping starts at "{first["phrase"]}" ({first["e0"]:.1f}s); out is the pause before it')
    return None, 0.0, "no sign-off or stop-recording phrase in the scanned minutes"


def suggest(ep: Episode, session: str | None = None, minutes: float | None = None, home: Path | None = None, save: bool = True) -> dict:
    """Scan the first and last `minutes` of the session and PROPOSE an in/out. Never applies it. Writes work/trim_suggestions.json (a record, not a decision)."""
    pol = resolve_policy(ep, home=home)
    tp = ep.artifact("transcript.json")
    if not tp.exists():
        raise SystemExit(f"No transcript. Run: ave transcribe {ep.root}")
    words = sorted(read_json(tp)["words"], key=lambda w: (w["start"], w["end"]))
    if not words:
        raise SystemExit("The transcript has no words.")
    key = session_key(ep, session)
    lo, hi = span(ep, session, words)
    inside = [w for w in words if lo - 1e-6 <= w["start"] and w["end"] <= hi + 1e-6]
    sents = sentences(inside)
    n = (minutes if minutes is not None else pol["scan_minutes"]) * 60.0
    sw = pol["short_sentence_words"]

    def scan(a, b, groups):
        hits = []
        for kind, phrases, short in groups:
            hits += _phrase_hits(inside, sents, a, b, kind, phrases, short, sw)
        # "i think we're done" already contains "we're done": keep the longest phrase of a kind at a place
        hits = [h for h in hits if not any(o is not h and o["kind"] == h["kind"] and o["e0"] <= h["e0"] and h["e1"] <= o["e1"]
                                           and (o["e1"] - o["e0"], o["phrase"]) > (h["e1"] - h["e0"], h["phrase"]) for o in hits)]
        return sorted({(h["e0"], h["phrase"], h["kind"]): h for h in hits}.values(), key=lambda h: (h["e0"], h["phrase"]))

    head_hits = scan(lo, lo + n, [("slate", pol["slate"], False), ("welcome", pol["welcome"]["strong"], False), ("welcome_weak", pol["welcome"]["weak"], False)])
    # a weak "welcome" that is part of a strong one ("welcome to") is one hit
    strong_at = {h["e0"] for h in head_hits if h["kind"] == "welcome"}
    head_hits = [h for h in head_hits if not (h["kind"] == "welcome_weak" and h["e0"] in strong_at)]
    tail_hits = scan(hi - n, hi, [("slate", pol["slate"], False), ("signoff", pol["signoff"]["strong"], False), ("stop", pol["signoff"]["stop"], False),
                                  ("stop", pol["signoff"]["short_only"], True)])
    p_in, c_in, why_in = _propose_in(inside, sents, head_hits, pol)
    p_out, c_out, why_out = _propose_out(inside, sents, tail_hits, pol)
    if p_in is not None:
        p_in = max(p_in, lo)
    if p_out is not None:
        p_out = min(p_out, hi)
    out = {"session": key, "span": [round(lo, 3), round(hi, 3)], "scan_minutes": n / 60.0, "applied": False,
           "head": {"evidence": head_hits, "proposed_in": p_in, "confidence": c_in, "reason": why_in},
           "tail": {"evidence": tail_hits, "proposed_out": p_out, "confidence": c_out, "reason": why_out},
           "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    if save:
        path = ep.artifact(SUGGESTIONS)
        allsug = read_json(path) if path.exists() else {}
        allsug[key] = out
        write_json(path, allsug)
    return out


def report(s: dict) -> str:
    """Human-readable suggestion: evidence lines first, then the proposal. Says plainly that nothing was applied."""
    def ts(t):
        return f"{int(t // 60)}:{t % 60:04.1f}"
    L = [f"Trim suggestions for {s['session']}  (session span {ts(s['span'][0])}-{ts(s['span'][1])}, scanning {s['scan_minutes']:g} min at each end)", ""]
    for name, part in (("START", s["head"]), ("END", s["tail"])):
        L.append(f"{name}: evidence")
        L += [f'  {ts(h["e0"])}  {h["kind"]:<12} "{h["phrase"]}"  {h["speaker"]}: {h["text"][:90]}' for h in part["evidence"]] or ["  (none)"]
        key, label = ("proposed_in", "proposed in") if name == "START" else ("proposed_out", "proposed out")
        if part[key] is None:
            L.append(f"  {label}: none ({part['reason']})")
        else:
            L.append(f"  {label}: {part[key]:.2f}s ({ts(part[key])})  confidence {part['confidence']:.2f}: {part['reason']}")
        L.append("")
    L.append("NOT applied. Listen at those times, then record your decision:")
    ins = s["head"]["proposed_in"]
    outs = s["tail"]["proposed_out"]
    flag = " ".join(x for x in (f"--in {ins:.2f}" if ins is not None else "", f"--out {outs:.2f}" if outs is not None else "") if x)
    L.append(f"  ave trim set <project> {s['session']} {flag or '--in SECONDS --out SECONDS'}     (then re-run: ave assemble ... and ave cleanup ...)")
    return "\n".join(L)


# ---- set / read ---------------------------------------------------------------------------------------------------------

def get(ep: Episode, meta: dict, session: str | None) -> dict | None:
    """The recorded trim ({in?, out?, by, at}) that applies to an assembly of `session` (None = the unnamed edit), or None.
    An unnamed edit of an episode with at most one session honours `main` or that session's own trim."""
    book = meta.get("trim") or {}
    if session:
        return book.get(session) or None
    ids = sessions.session_ids(ep, meta)
    for key in (MAIN, *(ids if len(ids) == 1 else [])):
        if book.get(key):
            return book[key]
    return None


def set_trim(ep: Episode, session: str, in_s: float | None = None, out_s: float | None = None, by: str = "human") -> dict:
    """Record a human-approved trim in episode.json (episode time). Giving only one end keeps the other as recorded."""
    if in_s is None and out_s is None:
        raise SystemExit("give --in and/or --out (episode seconds)")
    meta = read_json(ep.meta_path)
    ids = sessions.session_ids(ep, meta)
    if session not in ids and not (session == MAIN and len(ids) <= 1):
        raise SystemExit(f"unknown session {session!r}: " + (f"this episode has sessions {ids}" if ids else f"this episode has no sessions, use `{MAIN}`")
                         + f" (e.g. ave trim set <project> {ids[0] if ids else MAIN} --in 12.5)")
    tp = ep.artifact("transcript.json")
    words = read_json(tp)["words"] if tp.exists() else []
    lo, hi = span(ep, None if session == MAIN else session, sorted(words, key=lambda w: w["start"])) if words else (0.0, float("inf"))
    cur = (meta.get("trim") or {}).get(session) or {}
    a = float(in_s) if in_s is not None else cur.get("in")
    b = float(out_s) if out_s is not None else cur.get("out")
    for name, v in (("in", a), ("out", b)):
        if v is not None and not (lo - 1e-6 <= v <= hi + 1e-6):
            raise SystemExit(f"--{name} {v:g}s is outside the session ({lo:.1f}-{hi:.1f}s of episode time)")
    if a is not None and b is not None and not a < b:
        raise SystemExit(f"in must be before out (got in {a:g}s, out {b:g}s)")
    rec = {k: v for k, v in (("in", a), ("out", b)) if v is not None}
    rec.update(by=by, at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    meta.setdefault("trim", {})[session] = rec
    write_json(ep.meta_path, meta)
    from . import progress
    progress.log(ep, f"trim {session} set by {by}: in {a if a is not None else 'unchanged'}s, out {b if b is not None else 'unchanged'}s "
                     f"(episode time). Re-run `ave assemble` and `ave cleanup` for this session.", kind="decision", step="trim")
    return meta["trim"]


def clear_trim(ep: Episode, session: str) -> dict:
    meta = read_json(ep.meta_path)
    if (meta.get("trim") or {}).pop(session, None) is None:
        raise SystemExit(f"no trim recorded for {session!r}")
    write_json(ep.meta_path, meta)
    from . import progress
    progress.log(ep, f"trim {session} cleared", kind="decision", step="trim")
    return meta.get("trim") or {}


def pending_questions(ep: Episode) -> list[str]:
    """Open questions derived from disk: a suggestion that proposes something and has not been approved (no trim recorded for that session)."""
    p = ep.artifact(SUGGESTIONS)
    if not p.exists() or not ep.meta_path.exists():
        return []
    try:
        sug, done = read_json(p), (read_json(ep.meta_path).get("trim") or {})
    except (OSError, ValueError):
        return []
    out = []
    for key, s in sorted(sug.items()):
        ins, outs = s["head"]["proposed_in"], s["tail"]["proposed_out"]
        if (ins is None and outs is None) or key in done:
            continue
        bits = ", ".join(x for x in (f"in {ins:.1f}s" if ins is not None else "", f"out {outs:.1f}s" if outs is not None else "") if x)
        out.append(f"Trim for {key}: suggested {bits} but not approved. Review `ave trim suggest {ep.root} --session {key}`; "
                   f"approve or adjust with `ave trim set {ep.root} {key} --in S --out S` (before the rough cut).")
    return out
