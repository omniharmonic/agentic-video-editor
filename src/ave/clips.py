"""Clip finding: the code prepares evidence and VALIDATES; the agent (and then the human) judges.

Design rule: an LLM may only refer to the transcript through ids this module issued (`s000`, `s001`, ...), and every
quote it claims must exist verbatim inside the clip it picked. Nothing the agent says can put a word, a time, or a clip
into the timeline that the transcript does not contain, so hallucination cannot reach the EditDoc.

Pipeline (all times are OUTPUT seconds of the chosen source stage, so a clip cut from the rough edit never contains
the dead air or fillers the cleanup already removed):

    prepare  stage EditDoc + transcript -> sentences (+features) and EVERY clip-worthy section above a low floor
             (scored, tagged, explained, spread over the whole edit; alternates under each best-of-cluster)  -> work/clips_candidates.json
    propose  agent picks (sentence ids) -> validated, snapped, scored clips, plus (recall fill) every other surfaced section -> work/clips.json
    build    clips -> one `select.<slug>` EditDoc each + a combined `select.selects`     -> edits/
    decide   human decisions (keep|maybe|skip + reasons, or approve / reject / nudge by N sentences) -> clips.json
             and the learning ledger work/learning/clip_decisions.jsonl (ave.learning); the old log is work/clips_decisions.jsonl

Complete statements (field note N23): a clip must begin where a thought begins and end where one ends. `segment.Segmenter` judges
that from speaker turns, pauses, punctuation, capitalisation, discourse openers ("and", "so", "but"...) and dangling endings;
windows and picks are snapped OUT to the nearest complete thought (within max-s) and report `complete_statement` / `completeness_note`
(a clip that cannot be completed is kept and penalised). Each clip also carries `tighten` suggestions (`tighten.py`, never applied),
`context_before/after`, `rank`, and the top-N / min-score cut-off is only a VIEW over `rank`/`total` (nothing is deleted).

Sessions (field note N16): on a multi-session episode every command takes `--name <session>` and works on that session's
named edit (`rough.s1`). Candidates are per session (`clips_candidates.s1.json`); `clips.json` holds ALL sessions' clips with
ids prefixed by the session (`s1-c01`) and a per-clip `source` {stage, name, version, fingerprint}, so build/shorts/content
always cut the exact edit a clip was picked from. A re-prepare against a changed rough marks that session's old picks `stale`
(kept, never rewritten). Without sessions nothing changes: unnamed edits, `c01` ids, one `clips_candidates.json`.

The source stage is pinned by a fingerprint of the sentence table. If the edit or transcript changes underneath a
clip list, `propose`/`decide`/`build` refuse with "stale" instead of silently cutting the wrong words.
"""

from __future__ import annotations

import bisect
import copy
import hashlib
import json
import math
import re
import time
from pathlib import Path
from typing import Any

from . import editdoc, recall, segment, sessions, tighten
from .config import REPO_ROOT
from .episode import Episode, read_json, write_json

EPS = editdoc.EPS
DEFAULT_RUBRIC = REPO_ROOT / "config" / "clips.rubric.yaml"
CANDIDATES = "clips_candidates.json"
CLIPS = "clips.json"
TRANSCRIPT_TXT = "clips_transcript.txt"
DECISIONS_LOG = "clips_decisions.jsonl"
STATUSES = ("proposed", "approved", "rejected")
_TERMINAL = (".", "?", "!", "…")
_CLOSERS = "\"')]}”’"
DEFAULT_PENALTIES = {"start_incomplete": 12.0, "end_incomplete": 8.0}   # an older policy file without them still gets a penalty


# ---- sessions ----------------------------------------------------------------------------------------------

def resolve_name(ep: Episode, name: str | None) -> str | None:
    """The session a command works on. Several sessions and no name is an error (never silently pick one); a name on an
    episode without that session is an error too. No sessions and no name: None, the legacy unnamed behaviour."""
    try:
        ids = sessions.session_ids(ep)
    except (OSError, ValueError):
        ids = []
    if name is None:
        if len(ids) > 1:
            raise SystemExit(f"This episode has sessions {', '.join(ids)}: pass --name <session> (each session is its own edit, "
                             f"e.g. `--name {ids[0]}`).")
        return None
    if name not in ids and not any(editdoc.list_versions(ep, st, name) for st in ("assembly", "rough", "final")):
        raise SystemExit(f"No session or named edit {name!r}. " + (f"Sessions: {', '.join(ids)}." if ids else
                                                                    "This episode has no sessions: drop --name."))
    return name


def candidates_file(name: str | None) -> str:
    return f"clips_candidates.{name}.json" if name else CANDIDATES


def transcript_file(name: str | None) -> str:
    return f"clips_transcript.{name}.txt" if name else TRANSCRIPT_TXT


def _label(stage: str, name: str | None) -> str:
    return f"{stage}.{name}" if name else stage


def session_clips(data: dict | None, name: str | None) -> list[dict]:
    """The clips a command may touch: one session's when named, else all of them (the single unnamed list)."""
    if not data:
        return []
    return [c for c in data["clips"] if c.get("session") == name] if name else list(data["clips"])


# ---- rubric ------------------------------------------------------------------------------------------------

def rubric_path(ep: Episode, explicit: str | Path | None = None) -> Path:
    """--rubric > <episode>/policy/clips.rubric.yaml > config/clips.rubric.yaml."""
    for cand in (explicit, ep.root / "policy" / "clips.rubric.yaml", DEFAULT_RUBRIC):
        if cand and Path(cand).expanduser().is_file():
            return Path(cand).expanduser()
    raise SystemExit(f"No clips rubric found (looked for {explicit or '--rubric'}, policy/clips.rubric.yaml, {DEFAULT_RUBRIC}).")


def load_rubric(ep: Episode, explicit: str | Path | None = None) -> dict:
    import yaml
    path = rubric_path(ep, explicit)
    r = yaml.safe_load(path.read_text()) or {}
    w = r.get("weights") or {}
    if not w or any((not isinstance(v, (int, float))) or v <= 0 for v in w.values()):
        raise SystemExit(f"{path}: `weights` must map each criterion to a positive number.")
    for key in ("penalties", "limits", "snap", "sentences", "heuristics", "selects"):
        if key not in r:
            raise SystemExit(f"{path}: missing section `{key}`.")
    r["_path"] = str(path)
    if not explicit:                       # an explicit --rubric is taken as written; otherwise the show's taste profile may re-weight
        from . import taste
        taste.apply_to_rubric(ep, r)
    return r


# ---- stage doc + sentences ---------------------------------------------------------------------------------

def load_stage_doc(ep: Episode, stage: str | None = None, version: int | None = None,
                   name: str | None = None) -> tuple[dict, str, int]:
    """Latest doc of `stage` (default: rough, else assembly) of the named edit (`rough.s1`) or the unnamed one. Returns (doc, stage, version)."""
    if stage is None:
        stage = "rough" if editdoc.list_versions(ep, "rough", name) else "assembly"
    versions = editdoc.list_versions(ep, stage, name)
    what = _label(stage, name)
    if not versions:
        raise SystemExit(f"No {what} edit for {ep.root.name}. Run the earlier pipeline stage first (see `ave resume`)." + sessions.hint(ep, name))
    v = version or versions[-1]
    if v not in versions:
        raise SystemExit(f"{what} v{v} does not exist (have {versions}).")
    return editdoc.load(editdoc.path_for(ep, stage, v, name)), stage, v


def norm_token(w: str) -> str:
    return re.sub(r"[^a-z0-9']", "", w.lower().replace("’", "'"))


def norm_tokens(text: str) -> list[str]:
    return [t for t in (norm_token(x) for x in text.split()) if t]


def _terminal(token: str, abbreviations: set[str]) -> bool:
    return segment.is_terminal(token, abbreviations)


def mapped_words(ep: Episode, doc: dict) -> list[dict]:
    """Transcript words that survive in `doc`, with OUTPUT times. Words in removed sections, in `fill` segments, or
    cut in half by a segment boundary are dropped (they are not audible in this stage)."""
    tr = read_json(ep.artifact("transcript.json"))
    segs = sorted((s for s in doc["tracks"]["dialogue"] if not s.get("fill")), key=lambda s: s["e0"])
    starts = [s["e0"] for s in segs]
    out = []
    for w in tr["words"]:
        a, b = float(w["start"]), float(w["end"])
        k = bisect.bisect_right(starts, a + EPS) - 1
        if k < 0:
            continue
        for s in segs[max(k - 1, 0):k + 1]:   # the previous segment too: crossfaded neighbours overlap
            if s["e0"] - EPS <= a and b <= s["e0"] + (s["o1"] - s["o0"]) + EPS:
                o = s["o0"] + (a - s["e0"])
                out.append({"w": w["w"], "start": round(o, 3), "end": round(o + (b - a), 3), "e0": a, "speaker": w.get("speaker")})
                break
    out.sort(key=lambda x: (x["start"], x["end"]))
    return out


def _split_long(words: list[dict], max_words: int, dangling: set[str] | None = None) -> list[list[dict]]:
    """Split a run longer than max_words at its best internal break (middle half preferred): the largest pause, with a comma or a
    capital helping and a cut right after a dangling word ("and", "the"...) penalised (`segment.split_score`)."""
    if len(words) <= max_words:
        return [words]
    dangling = dangling or set()
    lo, hi = len(words) // 4, max(len(words) * 3 // 4, len(words) // 4 + 1)
    best = max(range(max(lo, 1), min(hi, len(words))), key=lambda k: segment.split_score(words, k, dangling))
    return _split_long(words[:best], max_words, dangling) + _split_long(words[best:], max_words, dangling)


def _group_sentences(words: list[dict], rubric: dict) -> list[list[dict]]:
    cfg = rubric["sentences"]
    abbr = {a.lower() for a in cfg.get("abbreviations", [])}
    sc = segment.config(rubric)
    dangling = set(sc["dangling_endings"])
    use_case = segment.capitalisation_is_informative(words, abbr)
    groups: list[list[dict]] = []
    for w in words:
        if groups:
            prev = groups[-1][-1]
            same_speaker = (w.get("speaker") == prev.get("speaker")) or w.get("speaker") is None or prev.get("speaker") is None
            gap = w["start"] - prev["end"]
            if same_speaker and not _terminal(prev["w"], abbr):
                # a long pause ends a sentence even without punctuation, EXCEPT a hesitation inside a clause: the speaker stopped on a
                # dangling word or a comma ("and and ...") and resumes in lowercase ("behind the counter")
                resumes = (gap < sc["merge_gap_s"] and (segment.norm_token(prev["w"]) in dangling or prev["w"].rstrip().endswith(","))
                           and (segment.starts_lower(w["w"]) if use_case else segment.norm_token(w["w"]) not in sc["continuation_openers"]))
                if gap < cfg["gap_s"] or resumes:
                    groups[-1].append(w)
                    continue
        groups.append([w])
    out: list[list[dict]] = []
    for g in groups:
        out += _split_long(g, int(cfg["max_words"]), dangling)
    return out


class Sentences:
    """The sentence table for one stage doc: issued ids, flat words, and boundary snapping."""

    def __init__(self, ep: Episode, doc: dict, rubric: dict):
        self.rubric = rubric
        self.duration = float(doc["duration"])
        self.words = mapped_words(ep, doc)
        self.fillers = {f.lower() for f in rubric["snap"]["filler_words"]}
        h = rubric["heuristics"]
        self._mid = set(h["mid_thought_starts"])
        self._pron = set(h["context_pronouns"])
        self._phrases = [p.lower() for p in h["context_phrases"]]
        abbr = {a.lower() for a in rubric["sentences"].get("abbreviations", [])}
        self.seg = segment.Segmenter(self.words, self.fillers, rubric)
        self.items: list[dict] = []
        pos = 0
        for n, g in enumerate(_group_sentences(self.words, rubric)):
            self.items.append(self._make(n, g, pos, abbr))
            pos += len(g)
        self.by_id = {s["id"]: s for s in self.items}
        self._complete_edges()
        self._back: dict[int, int] = {}
        self._fwd: dict[int, int] = {}

    def _complete_edges(self) -> None:
        """Per sentence: can a clip begin / end here (`segment.Segmenter`)? Judged on the spoken edges (fillers looked past)."""
        for s in self.items:
            a, b = self._edge_words(s["w0"], s["w1"] - 1)
            f = s["features"]
            if a is None:
                f.update(start_complete=False, end_complete=False, start_issue="only fillers", end_issue="only fillers", needs_context=None)
                continue
            si, ei = self.seg.start_info(a), self.seg.end_info(b)
            f.update(start_complete=si["ok"], end_complete=ei["ok"], start_issue=(si["reasons"] or [None])[0],
                     end_issue=(ei["reasons"] or [None])[0], needs_context=self.seg.needs_context(a))

    def _edge_words(self, a: int, b: int) -> tuple[int | None, int | None]:
        while a <= b and self._is_filler(self.words[a]):
            a += 1
        while b >= a and self._is_filler(self.words[b]):
            b -= 1
        return (a, b) if a <= b else (None, None)

    def _is_filler(self, w: dict) -> bool:
        return norm_token(w["w"]) in self.fillers

    def _make(self, n: int, g: list[dict], pos: int, abbr: set[str]) -> dict:
        toks = [norm_token(w["w"]) for w in g]
        text = " ".join(w["w"] for w in g)
        spk = [w.get("speaker") for w in g]
        speaker = max(set(spk), key=spk.count) if spk else None
        nf = sum(1 for w in g if self._is_filler(w))
        lead = 0
        for w in g:
            if not self._is_filler(w):
                break
            lead += 1
        low = " ".join(toks)
        phrase = any(p in low for p in self._phrases)
        return {
            "id": f"s{n:03d}", "i": n, "start": g[0]["start"], "end": g[-1]["end"], "e0": g[0]["e0"],
            "speaker": speaker, "text": text, "w0": pos, "w1": pos + len(g),
            "features": {
                "n_words": len(g), "duration": round(g[-1]["end"] - g[0]["start"], 3),
                "wps": round(len(g) / max(g[-1]["end"] - g[0]["start"], 0.1), 2),
                "filler_count": nf, "leading_fillers": lead,
                "starts_mid_thought": lead < len(toks) and toks[lead] in self._mid,
                "has_context_phrase": phrase,
                "refs_prior_context": phrase or (toks[lead] in self._pron if lead < len(toks) else False),
                "is_question": g[-1]["w"].rstrip(_CLOSERS).endswith("?"),
                "ends_complete": _terminal(g[-1]["w"], abbr),
            },
        }

    # -- thought edges: snap OUT, never cut in ---------------------------------------------------------------
    def _interjection(self, k: int, speaker: str | None) -> bool:
        """A short line by the other speaker (mm-hm, yeah): never a place for a clip to begin or end."""
        s = self.items[k]
        return s["speaker"] != speaker and s["features"]["n_words"] <= self.seg.cfg["interjection_max_words"]

    def thought_start(self, i: int) -> int:
        """The nearest sentence at or before `i` where a clip can begin (a complete thought start), looking back at most
        `completeness.max_expand_back` sentences and past the other speaker's interjections. `i` itself if it already is one,
        or if none is close enough (the clip is then reported as not start_complete rather than cut somewhere arbitrary)."""
        got = self._back.get(i)
        if got is None:
            sp, p, got = self.items[i]["speaker"], i, i
            for _ in range(int(self.seg.cfg["max_expand_back"]) + 1):
                if self.items[p]["features"]["start_complete"] and (p == i or not self._interjection(p, sp)):
                    got = p
                    break
                if p == 0:
                    break
                p -= 1
            self._back[i] = got
        return got

    def thought_end(self, j: int) -> int:
        """The nearest sentence at or after `j` that finishes a thought, looking ahead at most `completeness.max_expand_forward`."""
        got = self._fwd.get(j)
        if got is None:
            sp, q, got = self.items[j]["speaker"], j, j
            for _ in range(int(self.seg.cfg["max_expand_forward"]) + 1):
                if self.items[q]["features"]["end_complete"] and (q == j or not self._interjection(q, sp)):
                    got = q
                    break
                if q + 1 >= len(self.items):
                    break
                q += 1
            self._fwd[j] = got
        return got

    def expand(self, i: int, j: int, max_s: float | None = None, taken: list | None = None) -> tuple[int, int]:
        """Snap sentences i..j OUT to complete thought edges. Each side only moves if the result still fits `max_s` and does not run
        into a range in `taken` ((a, b, who) tuples); otherwise it stays and the clip honestly reports not start/end_complete."""
        i2, j2 = self.thought_start(i), self.thought_end(j)
        for a, b in ((i2, j2), (i2, j), (i, j2)):
            if (a, b) == (i, j):
                continue
            sn = self.snap(a, b)
            if sn is None or (max_s is not None and sn["duration"] > max_s):
                continue
            if any(not (b < x or a > y) for x, y, _ in (taken or [])):
                continue
            return a, b
        return i, j

    # -- identity -----------------------------------------------------------------------------------------
    def fingerprint(self) -> str:
        blob = json.dumps([(s["id"], s["start"], s["end"], s["text"]) for s in self.items], ensure_ascii=False)
        return hashlib.sha1(blob.encode()).hexdigest()[:16]

    # -- ranges -------------------------------------------------------------------------------------------
    def resolve(self, ids: list[str]) -> tuple[int, int, list[str]]:
        """Validate ids against the table. Returns (i, j, problems): inclusive sentence indexes of a CONTIGUOUS run."""
        problems: list[str] = []
        unknown = [x for x in ids if x not in self.by_id]
        if unknown:
            shown = ", ".join(map(str, unknown[:6])) + (" ..." if len(unknown) > 6 else "")
            problems.append(f"unknown sentence ids: {shown} (valid: s000..s{len(self.items) - 1:03d}; re-run `ave clips prepare` if the edit changed)")
            return -1, -1, problems
        idx = sorted(self.by_id[x]["i"] for x in ids)
        if len(set(idx)) != len(idx):
            problems.append("sentence_ids contains duplicates")
        i, j = idx[0], idx[-1]
        missing = sorted(set(range(i, j + 1)) - set(idx))
        if missing:
            ms = ", ".join(self.items[m]["id"] for m in missing[:6]) + (" ..." if len(missing) > 6 else "")
            problems.append(f"sentence_ids is not contiguous: missing {ms}. A clip is one continuous run; list every id from first to last")
        return i, j, problems

    def snap(self, i: int, j: int) -> dict | None:
        """Boundaries for sentences i..j: first/last spoken word (fillers trimmed) +lead/+tail, never crossing into a
        neighbouring word. Returns None if the run is only fillers."""
        a, b = self.items[i]["w0"], self.items[j]["w1"] - 1
        while a <= b and self._is_filler(self.words[a]):
            a += 1
        while b >= a and self._is_filler(self.words[b]):
            b -= 1
        if a > b:
            return None
        cfg = self.rubric["snap"]
        ws, we = self.words[a]["start"], self.words[b]["end"]
        prev_end = self.words[a - 1]["end"] if a > 0 else 0.0
        next_start = self.words[b + 1]["start"] if b + 1 < len(self.words) else self.duration
        start = max(ws - cfg["lead_s"], min(ws, (prev_end + ws) / 2.0) if a > 0 else 0.0, 0.0)
        end = min(we + cfg["tail_s"], max(we, (we + next_start) / 2.0) if b + 1 < len(self.words) else self.duration, self.duration)
        start, end = round(start, 3), round(end, 3)
        return {
            "start": start, "end": end, "duration": round(end - start, 3), "n_words": b - a + 1,
            "trimmed_leading": [w["w"] for w in self.words[self.items[i]["w0"]:a]],
            "trimmed_trailing": [w["w"] for w in self.words[b + 1:self.items[j]["w1"]]],
            "filler_count": sum(1 for w in self.words[a:b + 1] if self._is_filler(w)),
            "first_word": a, "last_word": b,
        }

    def text(self, i: int, j: int, snapped: dict | None = None) -> str:
        if snapped:
            return " ".join(w["w"] for w in self.words[snapped["first_word"]:snapped["last_word"] + 1])
        return " ".join(s["text"] for s in self.items[i:j + 1])


# ---- prepare -----------------------------------------------------------------------------------------------

def _window_features(S: Sentences, i: int, j: int, snapped: dict) -> dict:
    h = S.rubric["heuristics"]
    run = S.items[i:j + 1]
    turns = sum(1 for a, b in zip(run, run[1:]) if a["speaker"] != b["speaker"])
    pauses = [S.words[k + 1]["start"] - S.words[k]["end"] for k in range(snapped["first_word"], snapped["last_word"])]
    return {
        "starts_mid_thought": run[0]["features"]["starts_mid_thought"],
        "start_complete": S.seg.start_info(snapped["first_word"])["ok"],
        "refs_prior_context": run[0]["features"]["refs_prior_context"] or (len(run) > 1 and run[1]["features"]["has_context_phrase"]),
        "question_at_start": run[0]["features"]["is_question"] or (len(run) > 1 and run[1]["features"]["is_question"]),
        "ends_complete": run[-1]["features"]["ends_complete"],
        "filler_density": round(snapped["filler_count"] / max(snapped["n_words"], 1), 3),
        "speaker_turns": turns, "speakers": sorted({s["speaker"] for s in run if s["speaker"]}),
        "max_pause_s": round(max(pauses, default=0.0), 2),
        "long_pause": bool(pauses) and max(pauses) >= h["long_pause_s"],
    }


def _hint(f: dict, dur: float, h: dict) -> tuple[float, list[str]]:
    """A 0-1 ordering hint. It decides which windows are listed first, nothing more."""
    s, why = 1.0, []

    def adj(delta: float, reason: str) -> None:
        nonlocal s
        if delta:
            s += delta
            why.append(f"{delta:+.2f} {reason}")
    adj(-0.30 if f["starts_mid_thought"] else 0, "starts mid-thought")
    adj(-0.25 if f["refs_prior_context"] else 0, "refers to earlier context")
    adj(-0.20 if not f["ends_complete"] else 0, "ends without a terminal sentence")
    adj(-round(min(0.2, f["filler_density"] * 2), 2), "filler density")
    adj(-0.15 if f["long_pause"] else 0, "long internal pause")
    lo, hi = h["sweet_spot_s"]
    adj(-round(min(0.15, 0.01 * (lo - dur if dur < lo else dur - hi)), 2) if not lo <= dur <= hi else 0, "outside sweet-spot length")
    adj(0.10 if f["question_at_start"] else 0, "question near the start")
    adj(round(0.05 * min(f["speaker_turns"], 3), 2), "speaker turns")
    return round(max(0.0, min(1.0, s)), 3), why


def evaluate_window(S: Sentences, i: int, j: int, sn: dict, f: dict, params: dict, rubric: dict) -> dict:
    """Heuristic evidence + score for sentences i..j (the same score_clip the agent's picks go through, with heuristic 0-10 inputs)."""
    prof = recall.window_profile(S, i, j, sn)
    scores = recall.heuristic_scores(S, i, j, f, prof)
    inputs = {"scores": {k: scores.get(k, 3.0) for k in rubric["weights"]}, "sensitive": False, "needs_context": None}
    sc = score_clip(S, i, j, sn, inputs, params, rubric)
    total, penalties = sc["total"], list(sc["penalties"])
    tp = float(recall.config(rubric)["tangent_penalty"])
    if "tangent" in prof["tags"] and tp > 0:
        penalties.append({"name": "tangent", "points": tp, "reason": "reads as an aside"})
        total = round(max(0.0, total - tp), 2)
    return {"score": total, "inputs": inputs, "weighted": sc["weighted"], "penalties": penalties, "warnings": sc["warnings"],
            "reason": recall.reason_line(prof, f, sn["duration"], penalties), **prof}


def _alternates(cluster: list[tuple], primary: tuple, want: int) -> list[dict]:
    """Other in/out choices for the same region: best first, each with a different start AND end sentence from the primary and each other."""
    starts, ends, out = {primary[1]}, {primary[2]}, []
    for sc, i, j, sn, ev in cluster:
        if len(out) >= want:
            break
        if i in starts or j in ends:
            continue
        starts.add(i)
        ends.add(j)
        out.append({"start_sid": f"s{i:03d}", "end_sid": f"s{j:03d}", "start": sn["start"], "end": sn["end"], "duration": sn["duration"],
                    "score": sc, "ends_on_sentence": ev["ends_on_sentence"], "complete_statement": ev["complete_statement"],
                    "tags": ev["tags"], "reason": ev["reason"]})
    return out


def candidate_windows(S: Sentences, min_s: float, max_s: float, max_candidates: int | None = None, info: dict | None = None,
                      floor: float | None = None) -> list[dict]:
    """Recall first: EVERY sentence-aligned window whose SNAPPED length is within [min_s, max_s] and whose heuristic score is at least
    `floor` (default `recall.floor`, 30/100) is scored, then overlapping windows are clustered. The best of each cluster is listed
    (non-overlapping, so each region of the episode appears once) and the cluster's other in/out choices hang off it as `alternates`.

    If more clusters exist than `max_candidates` (default `recall.max_per_session`, 80, a sanity cap) the survivors are SPREAD across
    the whole edit (the best of each time bucket): ranking ties used to fall back to start time, so a fixed cap of 60 silently kept
    only the first 36 minutes of a 103-minute session (N17). `info`, when given, receives {available, kept, cap, truncated, floor}."""
    h = S.rubric["heuristics"]
    rc = recall.config(S.rubric)
    floor = recall.floor(S.rubric) if floor is None else float(floor)
    params = {"min_s": min_s, "max_s": max_s}
    found = []
    expand = bool(S.seg.cfg["expand"])
    seen: set[tuple[int, int]] = set()
    for i0 in range(len(S.items)):
        for j0 in range(i0, len(S.items)):
            sn = S.snap(i0, j0)
            if sn is None:
                continue
            if sn["duration"] > max_s:
                break
            if sn["duration"] < min_s:
                continue
            i, j = S.expand(i0, j0, max_s) if expand else (i0, j0)    # snap OUT to complete thought edges (within max_s), never cut in
            if (i, j) in seen:
                continue
            seen.add((i, j))
            if (i, j) != (i0, j0):
                sn = S.snap(i, j)
            f = _window_features(S, i, j, sn)
            ev = evaluate_window(S, i, j, sn, f, params, S.rubric)
            if ev["score"] < floor:
                continue
            hint, why = _hint(f, sn["duration"], h)
            found.append((ev["score"], hint, i, j, sn, f, why, ev))
    found.sort(key=lambda x: (-x[0], -x[1], x[2], x[3]))
    taken: list[tuple[int, int]] = []
    clusters: list[list[tuple]] = []
    for sc, hint, i, j, sn, f, why, ev in found:
        for k, (a, b) in enumerate(taken):
            if not (j < a or i > b):
                clusters[k].append((sc, i, j, sn, ev))
                break
        else:
            taken.append((i, j))
            clusters.append([(sc, i, j, sn, ev)])
    out = []
    by_key = {(i, j): (hint, why, f) for sc, hint, i, j, sn, f, why, ev in found}
    margin = float(S.seg.cfg["prefer_complete_margin"])
    for members in clusters:
        top = members[0]
        if not top[4]["complete_statement"]:      # a complete statement within `margin` points beats a slightly higher-scoring fragment
            alt = next((m for m in members if m[4]["complete_statement"] and m[0] >= top[0] - margin), None)
            if alt is not None:
                members = [alt] + [m for m in members if m is not alt]
        sc, i, j, sn, ev = members[0]
        hint, why, f = by_key[(i, j)]
        out.append({
            "id": "", "start_sid": S.items[i]["id"], "end_sid": S.items[j]["id"],
            "n_sentences": j - i + 1, "start": sn["start"], "end": sn["end"], "duration": sn["duration"],
            "score": ev["score"], "scores": ev["inputs"]["scores"], "breakdown": {"weighted": ev["weighted"], "penalties": ev["penalties"]},
            "tags": ev["tags"], "hook_pos_s": ev["hook_pos_s"], "ends_on_sentence": ev["ends_on_sentence"],
            "speaker_balance": ev["speaker_balance"], "reason": ev["reason"],
            "start_complete": ev["start_complete"], "end_complete": ev["end_complete"], "complete_statement": ev["complete_statement"],
            "completeness_note": ev["completeness_note"], "needs_context": ev["needs_context"],
            "alternates": _alternates(members[1:], members[0][:3], int(rc["alternates"])), "cluster_size": len(members),
            "features": f, "hint": hint, "hint_reasons": why, "preview": S.text(i, j, sn)[:140],
        })
    available = len(out)
    cap = int(max_candidates) if max_candidates else int(rc["max_per_session"])
    if available > cap:
        out.sort(key=lambda w: w["start"])
        buckets = [out[round(k * available / cap):round((k + 1) * available / cap)] for k in range(cap)]
        out = [max(b, key=lambda w: (w["score"], w["hint"], -w["start"])) for b in buckets if b]       # best window per time bucket, spread evenly
    out.sort(key=lambda w: (-w["score"], -w["hint"], w["start"]))
    for n, w in enumerate(out, 1):
        w["id"] = f"w{n:02d}"
    if info is not None:
        info.update({"available": available, "kept": len(out), "cap": cap, "truncated": available > len(out), "floor": floor})
    return out


def _clock(t: float) -> str:
    return f"{int(t // 60):02d}:{t % 60:04.1f}"


def write_transcript_txt(path: Path, S: Sentences, meta: str, windows: list[dict] | None = None, exemplars: dict | None = None,
                         info: dict | None = None) -> None:
    lines = [meta, "Tags: ? question, mid starts mid-thought, ctx refers to earlier context, ~start a clip cannot BEGIN here (a continuation: "
                   "lowercase, 'and/so/but...', or it runs on from the sentence before), ~end a clip cannot END here (dangling word, comma, run-on). "
                   "Reference sentences by id; the code snaps your range OUT to complete thoughts and reports `complete_statement`.", ""]
    if exemplars and (exemplars.get("keep") or exemplars.get("skip")):
        lines.append("What the human kept and skipped before (learn the taste, do not copy it):")
        for verdict in ("keep", "skip"):
            for e in exemplars.get(verdict, []):
                why = ", ".join(e.get("reasons") or []) or (e.get("note") or "no reason given")
                lines.append(f"  {verdict.upper():4} score {e.get('score') if e.get('score') is not None else '--'}  [{', '.join(e.get('tags') or [])}]  "
                             f"{why}: \"{e.get('quote', '')}\"")
        lines.append("")
    for s in S.items:
        f = s["features"]
        tags = " ".join(t for t, on in (("?", f["is_question"]), ("mid", f["starts_mid_thought"]), ("ctx", f["refs_prior_context"]),
                                        ("~start", not f.get("start_complete", True)), ("~end", not f.get("end_complete", True))) if on)
        lines.append(f"{s['id']}  {_clock(s['start'])}  {s['speaker'] or '--'}  {s['text']}" + (f"  [{tags}]" if tags else ""))
    if windows:
        lines += ["", f"Surfaced sections ({len(windows)}; every one scored {info.get('floor', 0):g}+ by cheap heuristics, spread over the whole edit; "
                      "a section is a candidate, not a verdict. `propose` adds these for the human even if you do not pick them):"]
        for w in sorted(windows, key=lambda w: w["start"]):
            alt = f"  +{len(w['alternates'])} alt" if w.get("alternates") else ""
            comp = "" if w.get("complete_statement", True) else f"  INCOMPLETE ({w.get('completeness_note')})"
            lines.append(f"{w['id']}  {w['start_sid']}-{w['end_sid']}  {_clock(w['start'])}  {w['duration']:.0f}s  score {w['score']:.0f}  "
                         f"[{', '.join(w['tags']) or 'plain'}]  {w['reason']}{alt}{comp}")
    path.write_text("\n".join(lines) + "\n")


def mark_stale(ep: Episode, name: str, source: dict) -> list[str]:
    """After a session's candidates moved to `source`, flag that session's picks made from a different sentence table.

    Their sentence ids belong to the old table, so they cannot be re-validated, nudged or rebuilt against the new one; they are
    kept exactly as they were (nothing is rewritten or deleted) and marked `stale` with the reason. A pick whose table is
    identical to the new one is fresh again (the edit was only re-versioned). Returns the ids newly marked."""
    data = load_clips(ep)
    if not data:
        return []
    marked, changed = [], False
    for c in session_clips(data, name):
        src = c["source"]
        if src["fingerprint"] != source["fingerprint"]:
            if not c.get("stale"):
                marked.append(c["id"])
                changed = True
            c["stale"] = True
            c["stale_reason"] = (f"picked from {_label(src['stage'], name)} v{src['version']}; `ave clips prepare` now reads "
                                 f"{_label(source['stage'], name)} v{source['version']}, whose sentences differ. Re-pick this moment from the new prepare")
        elif c.get("stale"):
            c.pop("stale")
            c.pop("stale_reason", None)
            changed = True
    if changed:
        write_json(ep.artifact(CLIPS), data)
    return marked


def prepare(ep: Episode, stage: str | None = None, version: int | None = None, min_s: float = 20.0, max_s: float = 60.0,
            rubric: str | Path | None = None, max_candidates: int | None = None, name: str | None = None,
            floor: float | None = None) -> dict:
    """Write work/clips_candidates[.<name>].json (+ a token-light clips_transcript[.<name>].txt) for the agent to read."""
    name = resolve_name(ep, name)
    if not ep.artifact("transcript.json").exists():
        raise SystemExit("No transcript yet. Run: ave transcribe <episode>")
    if not 0 < min_s < max_s:
        raise SystemExit("--min-s must be > 0 and < --max-s")
    r = load_rubric(ep, rubric)
    doc, stage, v = load_stage_doc(ep, stage, version, name)
    S = Sentences(ep, doc, r)
    if not S.items:
        raise SystemExit(f"No transcript words survive in {_label(stage, name)} v{v}; nothing to clip.")
    cinfo: dict = {}
    windows = candidate_windows(S, min_s, max_s, max_candidates, cinfo, floor)
    from . import taste
    ex = taste.exemplars(ep)
    data = {
        "version": 1, "made": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": {"stage": stage, **({"name": name} if name else {}), "version": v,
                   "path": str(editdoc.path_for(ep, stage, v, name).relative_to(ep.root)),
                   "duration": doc["duration"], "fingerprint": S.fingerprint()},
        "params": {"min_s": min_s, "max_s": max_s, "max_candidates": cinfo.get("cap"), "floor": cinfo.get("floor"), "rubric": r["_path"],
                   "windows_available": cinfo.get("available"), "taste": r.get("_taste")},
        "criteria": r["weights"], "exemplars": ex,
        "sentences": [{k: v_ for k, v_ in s.items() if k not in ("w0", "w1")} for s in S.items],
        "windows": windows,
    }
    write_json(ep.artifact(candidates_file(name)), data)
    write_transcript_txt(ep.artifact(transcript_file(name)), S,
                         f"{ep.root.name} | {_label(stage, name)} v{v} | {doc['duration']:.1f}s | {len(S.items)} sentences",
                         windows, ex, cinfo)
    out = {"candidates": str(ep.artifact(candidates_file(name))), "transcript": str(ep.artifact(transcript_file(name))), "stage": stage, "version": v,
           "sentences": len(S.items), "windows": len(windows), "windows_available": cinfo.get("available"), "floor": cinfo.get("floor"),
           "cap": cinfo.get("cap"), "truncated": cinfo.get("truncated"), "fingerprint": S.fingerprint(),
           "next": f"Read work/{transcript_file(name)}, judge the surfaced sections and any others, write picks.json, run: "
                   f"ave clips propose <episode> --picks picks.json{f' --name {name}' if name else ''} "
                   f"(or without --picks to surface every section for the human)"}
    if name:
        out = {"name": name, **out, "stale_clips": mark_stale(ep, name, data["source"])}
    return out


# ---- shared state loading ----------------------------------------------------------------------------------

def load_candidates(ep: Episode, name: str | None = None) -> dict:
    p = ep.artifact(candidates_file(name))
    if not p.exists():
        raise SystemExit("No candidates yet. Run: ave clips prepare <episode>" + (f" --name {name}" if name else ""))
    return read_json(p)


def _live_table(ep: Episode, cand: dict, rubric: dict) -> tuple[Sentences, dict]:
    """Rebuild the sentence table from disk and prove it is the one the candidates were made from."""
    src = cand["source"]
    name = src.get("name")
    doc, _, _ = load_stage_doc(ep, src["stage"], src["version"], name)
    S = Sentences(ep, doc, rubric)
    if S.fingerprint() != src["fingerprint"]:
        raise SystemExit(f"{candidates_file(name)} is stale: the transcript or the edit changed since `ave clips prepare`. Re-run prepare "
                         "and re-pick (sentence ids are only meaningful for the table they were issued from).")
    return S, doc


def load_clips(ep: Episode) -> dict | None:
    p = ep.artifact(CLIPS)
    return read_json(p) if p.exists() else None


# ---- finalizing a clip: context, tighten suggestions, the expanded variant, rank -----------------------------

def _context_text(S: Sentences, lo: int, hi: int, tail: bool) -> str:
    cfg = S.seg.cfg
    txt = " ".join(s["text"] for s in S.items[max(lo, 0):max(hi, 0)])
    cap = int(cfg["context_chars"])
    if len(txt) <= cap:
        return txt
    return ("..." + txt[-cap:].lstrip()) if tail else (txt[:cap].rstrip() + "...")


def context_variant(S: Sentences, i: int, j: int, params: dict) -> dict | None:
    """For a clip that opens on an unresolved referent: the same clip started one or two sentences earlier, at a complete thought
    start, if that still fits the target length. Offered, never applied."""
    for back in range(1, int(S.seg.cfg["variant_max_back"]) + 1):
        p = i - back
        if p < 0:
            break
        sn = S.snap(p, j)
        if sn is None or sn["duration"] > params["max_s"]:
            break
        if S.items[p]["features"]["start_complete"] and not S._interjection(p, S.items[i]["speaker"]):
            added = " ".join(s["text"] for s in S.items[p:i])
            return {"sentence_ids": [S.items[k]["id"] for k in range(p, j + 1)], "start": sn["start"], "end": sn["end"],
                    "duration": sn["duration"], "added_text": added,
                    "note": f"starts {back} sentence{'s' if back > 1 else ''} earlier so the opening has its antecedent"}
    return None


def finalize_fields(S: Sentences, clip: dict, params: dict, rubric: dict) -> dict:
    """Round-2 fields that depend on the final sentence range: context_before/after, tighten (+ tight_duration/tight_text), and the
    `context_variant` for a clip that needs context. Pure: the same clip always gets the same fields."""
    i, j = S.by_id[clip["sentence_ids"][0]]["i"], S.by_id[clip["sentence_ids"][-1]]["i"]
    sn = S.snap(i, j)
    n = int(S.seg.cfg["context_sentences"])
    out = {"context_before": _context_text(S, i - n, i, True), "context_after": _context_text(S, j + 1, j + 1 + n, False)}
    out.update(tighten.suggest(S, i, j, sn, clip, rubric) if sn else {"tighten": [], "tight_duration": clip.get("duration"), "tight_text": clip.get("text")})
    nc = bool(clip.get("needs_context")) or bool((clip.get("inputs") or {}).get("needs_context"))
    out["context_variant"] = context_variant(S, i, j, params) if nc else None
    return out


def _current_rubric(ep: Episode, data: dict) -> dict:
    """The rubric as configured NOW (so editing `recall.top_n` takes effect on the next list/build), else the one stored with the clips."""
    try:
        return load_rubric(ep)
    except SystemExit:
        return data.get("rubric") or {}


def rank_map(clips_: list[dict]) -> dict[str, int]:
    """1 = best total within the given clips (stale ones are not ranked). Ties go to the earlier clip."""
    live = [c for c in clips_ if not c.get("stale")]
    live.sort(key=lambda c: (-c["total"], c["start"], c["id"]))
    return {c["id"]: n for n, c in enumerate(live, 1)}


def assign_ranks(data: dict) -> None:
    """(Re)write `rank` on every clip, per session. `total` is the score. Top-N and min-score are views over this one list."""
    groups: dict[Any, list[dict]] = {}
    for c in data["clips"]:
        groups.setdefault(c.get("session"), []).append(c)
    for group in groups.values():
        ranks = rank_map(group)
        for c in group:
            c["rank"] = ranks.get(c["id"])


def selection_config(rubric: dict, top_n: Any = None, min_score: Any = None) -> tuple[int | None, float | None]:
    """Explicit arguments win; else `recall.top_n` / `recall.min_score` from the rubric. Validated."""
    rc = recall.config(rubric)
    top_n = rc.get("top_n") if top_n is None else top_n
    min_score = rc.get("min_score") if min_score is None else min_score
    if top_n is not None and (isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 1):
        raise SystemExit(f"--top / recall.top_n must be a whole number >= 1, got {top_n!r}.")
    if min_score is not None and (not _num(min_score) or not 0 <= min_score <= 100):
        raise SystemExit(f"--min-score / recall.min_score must be a number from 0 to 100, got {min_score!r}.")
    return top_n, (None if min_score is None else float(min_score))


def selection_view(items: list[dict], top_n: int | None, min_score: float | None) -> dict:
    """The cut-off as a VIEW over ranked clips (dicts with id, rank, total): nothing is deleted or rewritten, the clips below the
    line are simply not shown. A clip is shown when rank <= top_n AND total >= min_score."""
    ranked = sorted((c for c in items if c.get("rank")), key=lambda c: c["rank"])
    shown = [c["id"] for c in ranked if (top_n is None or c["rank"] <= top_n) and (min_score is None or c["total"] >= min_score)]
    hidden = [c["id"] for c in ranked if c["id"] not in set(shown)]
    rules = [f"top {top_n}" if top_n else None, f"score >= {min_score:g}" if min_score is not None else None]
    rule = " and ".join(r for r in rules if r) or "everything"
    return {"top_n": top_n, "min_score": min_score, "shown": shown, "hidden": hidden,
            "cut_line": f"--- cut line ({rule}): {len(shown)} of {len(ranked)} shown, {len(hidden)} below the line, none deleted ---"}


# ---- propose -----------------------------------------------------------------------------------------------

def slugify(title: str, fallback: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-")
    return s or fallback


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _quote_in(quote: str, clip_tokens: list[str]) -> bool:
    q = norm_tokens(quote)
    if not q:
        return False
    return any(clip_tokens[k:k + len(q)] == q for k in range(len(clip_tokens) - len(q) + 1))


def score_clip(S: Sentences, i: int, j: int, snapped: dict, inputs: dict, params: dict, rubric: dict) -> dict:
    """Weighted rubric score minus explainable penalties. Pure: the same inputs always give the same total."""
    weights = rubric["weights"]
    wsum = float(sum(weights.values()))
    weighted = {k: round(weights[k] * inputs["scores"][k] / 10.0 * 100.0 / wsum, 2) for k in weights}
    base = sum(weighted.values())
    pen = rubric["penalties"]
    penalties: list[dict] = []
    warnings: list[str] = []

    def add(name: str, pts: float, reason: str) -> None:
        if pts > 0:
            penalties.append({"name": name, "points": round(pts, 2), "reason": reason})

    dur = snapped["duration"]
    lo, hi = params["min_s"], params["max_s"]
    off = lo - dur if dur < lo else (dur - hi if dur > hi else 0.0)
    cfg = pen["length_out_of_range"]
    add("length_out_of_range", min(cfg["max_points"], off * cfg["points_per_second"]), f"{dur:.1f}s is outside the {lo:g}-{hi:g}s target")
    si, ei = S.seg.start_info(snapped["first_word"]), S.seg.end_info(snapped["last_word"])
    if not si["ok"]:
        add("start_incomplete", pen.get("start_incomplete", DEFAULT_PENALTIES["start_incomplete"]), "starts mid-sentence: " + si["reasons"][0])
    elif S.items[i]["features"]["starts_mid_thought"]:
        add("starts_mid_thought", pen["starts_mid_thought"], f"opens with '{S.items[i]['text'].split()[0]}...' (continues an earlier thought)")
    if not ei["ok"]:
        add("end_incomplete", pen.get("end_incomplete", DEFAULT_PENALTIES["end_incomplete"]), "ends mid-thought: " + ei["reasons"][0])
    auto_ctx = S.items[i]["features"]["refs_prior_context"] or (j > i and S.items[i + 1]["features"]["has_context_phrase"])
    flag = inputs.get("needs_context")
    if flag is True or (flag is None and auto_ctx):
        why = "agent flagged" if flag else "opens by referring to something not in the clip"
        add("needs_prior_context", pen["needs_prior_context"], why)
    elif flag is False and auto_ctx:
        warnings.append("opening looks context-dependent (pronoun/earlier-reference) but the agent marked needs_context=false; the human will check")
    if inputs.get("sensitive"):
        add("sensitive", pen["sensitive"], "agent flagged sensitive content")
    dens = snapped["filler_count"] / max(snapped["n_words"], 1)
    if dens > rubric["heuristics"]["filler_density_warn"]:
        add("high_filler_density", pen["high_filler_density"], f"{snapped['filler_count']} fillers in {snapped['n_words']} words")
    if not ei["ok"]:
        warnings.append("the clip does not end on a finished thought; check the ending lands")
    total = max(0.0, min(100.0, base - sum(p["points"] for p in penalties)))
    return {"weighted": weighted, "base": round(base, 2), "penalties": penalties, "total": round(total, 2), "warnings": warnings}


def _validate_scores(scores: Any, criteria: dict) -> list[str]:
    if not isinstance(scores, dict):
        return [f"scores must be an object with keys {sorted(criteria)}"]
    p = []
    missing = [k for k in criteria if k not in scores]
    extra = [k for k in scores if k not in criteria]
    if missing:
        p.append(f"scores is missing criteria: {', '.join(missing)}")
    if extra:
        p.append(f"scores has unknown criteria: {', '.join(extra)} (valid: {', '.join(criteria)})")
    for k, v in scores.items():
        if k in criteria and (not _num(v) or not 0 <= v <= 10):
            p.append(f"score '{k}' must be a number 0-10, got {v!r}")
    return p


def _overlaps(i: int, j: int, other: tuple[int, int]) -> bool:
    return not (j < other[0] or i > other[1])


def evaluate_pick(S: Sentences, pick: Any, params: dict, rubric: dict, taken: list[tuple[int, int, str]]) -> tuple[dict | None, list[str]]:
    """Validate ONE pick. Returns (clip or None, problems). Reports every problem, not just the first."""
    if not isinstance(pick, dict):
        return None, ["a pick must be an object"]
    problems: list[str] = []
    title = pick.get("title")
    if not isinstance(title, str) or not title.strip():
        problems.append("title is required")
    if not isinstance(pick.get("hook"), str) or not pick["hook"].strip():
        problems.append("hook is required: one line saying why the first seconds stop a scroll")
    if not isinstance(pick.get("rationale"), str) or not pick["rationale"].strip():
        problems.append("rationale is required: why this clip, in one or two sentences")
    ids = pick.get("sentence_ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(x, str) for x in ids):
        problems.append("sentence_ids must be a non-empty list of ids like [\"s014\", ..., \"s031\"]")
        return None, problems + _validate_scores(pick.get("scores"), rubric["weights"])
    i, j, p = S.resolve(ids)
    problems += p
    problems += _validate_scores(pick.get("scores"), rubric["weights"])
    if i < 0:
        return None, problems
    sn = S.snap(i, j)
    if sn is None:
        problems.append("the selected sentences contain only fillers")
        return None, problems
    expanded = None
    if S.seg.cfg["expand_picks"] and not problems:
        i2, j2 = S.expand(i, j, params["max_s"], taken)          # snap OUT to complete thought edges; reported, never silent
        if (i2, j2) != (i, j):
            expanded = {"from": [S.items[i]["id"], S.items[j]["id"]], "start_sentences": i - i2, "end_sentences": j2 - j,
                        "note": f"expanded from {S.items[i]['id']}-{S.items[j]['id']} to {S.items[i2]['id']}-{S.items[j2]['id']} to start/end on a complete thought"}
            i, j = i2, j2
            sn = S.snap(i, j)
    lim = rubric["limits"]
    if sn["duration"] < lim["hard_min_s"]:
        problems.append(f"too short: {sn['duration']:.1f}s after snapping (minimum {lim['hard_min_s']:g}s). Add neighbouring sentences")
    if sn["duration"] > lim["hard_max_s"]:
        problems.append(f"too long: {sn['duration']:.1f}s after snapping (maximum {lim['hard_max_s']:g}s). Trim sentences")
    for a, b, who in taken:
        if _overlaps(i, j, (a, b)):
            problems.append(f"overlaps {who} (sentences s{a:03d}-s{b:03d}); clips may not share sentences")
            break
    clip_tokens = norm_tokens(S.text(i, j, sn))
    quotes = [("hook_quote", pick.get("hook_quote")), ("end_quote", pick.get("end_quote"))]
    quotes += [(f"quotes[{n}]", q) for n, q in enumerate(pick.get("quotes") or [])]
    for name, q in quotes:
        if q is None:
            continue
        if not isinstance(q, str) or not _quote_in(q, clip_tokens):
            problems.append(f"{name} {str(q)[:60]!r} does not appear verbatim in the selected sentences (checked against the transcript, ignoring case and punctuation)")
    if pick.get("sensitive") is not None and not isinstance(pick["sensitive"], bool):
        problems.append("sensitive must be true or false")
    if pick.get("needs_context") is not None and not isinstance(pick["needs_context"], bool):
        problems.append("needs_context must be true or false")
    if problems:
        return None, problems
    inputs = {"scores": {k: float(v) for k, v in pick["scores"].items()}, "sensitive": bool(pick.get("sensitive")),
              "needs_context": pick.get("needs_context")}
    sc = score_clip(S, i, j, sn, inputs, params, rubric)
    warn = list(sc["warnings"])
    hq = pick.get("hook_quote")
    if hq and not _quote_in(hq, norm_tokens(" ".join(s["text"] for s in S.items[i:min(j, i + 1) + 1]))):
        warn.append("hook_quote is not in the first two sentences; the hook may not land in the opening seconds")
    fl = recall.floor(rubric)
    if sc["total"] < fl:
        return None, [f"score {sc['total']:.1f} is below the minimum {fl:g} "
                      f"(penalties: {', '.join(p['name'] for p in sc['penalties']) or 'none'}). Rework the pick or drop it"]
    clip = {
        "title": title.strip(), "hook": pick["hook"].strip(), "hook_quote": hq, "end_quote": pick.get("end_quote"),
        "rationale": pick["rationale"].strip(), "inputs": inputs,
        "sentence_ids": [S.items[k]["id"] for k in range(i, j + 1)],
        "start": sn["start"], "end": sn["end"], "duration": sn["duration"],
        "speakers": sorted({s["speaker"] for s in S.items[i:j + 1] if s["speaker"]}),
        "text": S.text(i, j, sn), "trimmed": {"leading": sn["trimmed_leading"], "trailing": sn["trimmed_trailing"]},
        "weighted": sc["weighted"], "penalties": sc["penalties"], "total": sc["total"], "warnings": warn,
        "origin": "agent", "alternates": [], "expanded": expanded, **recall.window_profile(S, i, j, sn),
    }
    clip["reason"] = pick.get("reason") or recall.reason_line(clip, S.items[i]["features"], sn["duration"], sc["penalties"])
    return clip, []


def _auto_title(text: str) -> str:
    words = text.split()
    t = ""
    for w in words:
        if len(t) + len(w) + 1 > 60:
            t += "..."
            break
        t = f"{t} {w}".strip()
    return t.rstrip(" ,;:-") or "Untitled section"


def auto_clip(S: Sentences, w: dict, params: dict, rubric: dict) -> tuple[dict | None, list[str]]:
    """A heuristic-scored clip for one surfaced window (recomputed from the live table, never trusted from the file)."""
    if w["start_sid"] not in S.by_id or w["end_sid"] not in S.by_id:
        return None, [f"{w['id']}: sentence ids are not in the live table"]
    i, j = S.by_id[w["start_sid"]]["i"], S.by_id[w["end_sid"]]["i"]
    sn = S.snap(i, j)
    if sn is None:
        return None, [f"{w['id']}: only fillers"]
    lim = rubric["limits"]
    if not lim["hard_min_s"] <= sn["duration"] <= lim["hard_max_s"]:
        return None, [f"{w['id']}: {sn['duration']:.1f}s is outside {lim['hard_min_s']:g}-{lim['hard_max_s']:g}s"]
    f = _window_features(S, i, j, sn)
    ev = evaluate_window(S, i, j, sn, f, params, rubric)
    if ev["score"] < recall.floor(rubric):
        return None, [f"{w['id']}: score {ev['score']:.1f} is below the floor"]
    stags = recall.sentence_tags(S)
    hooky = next((k for k in range(i, j + 1) if stags[k] & {"hook", "claim", "quotable"}), i)
    hook_text = S.items[hooky]["text"]
    return {
        "title": _auto_title(S.items[i]["text"]), "hook": hook_text if len(hook_text) <= 140 else hook_text[:137] + "...",
        "hook_quote": None, "end_quote": None, "rationale": ev["reason"], "reason": ev["reason"], "inputs": ev["inputs"],
        "sentence_ids": [S.items[k]["id"] for k in range(i, j + 1)], "start": sn["start"], "end": sn["end"], "duration": sn["duration"],
        "speakers": sorted({s["speaker"] for s in S.items[i:j + 1] if s["speaker"]}), "text": S.text(i, j, sn),
        "trimmed": {"leading": sn["trimmed_leading"], "trailing": sn["trimmed_trailing"]},
        "weighted": ev["weighted"], "penalties": ev["penalties"], "total": ev["score"], "warnings": list(ev["warnings"]),
        "origin": "heuristic", "alternates": w.get("alternates") or [], "tags": ev["tags"], "hook_pos_s": ev["hook_pos_s"],
        "ends_on_sentence": ev["ends_on_sentence"], "speaker_balance": ev["speaker_balance"], "text_sha": ev["text_sha"],
        "start_complete": ev["start_complete"], "end_complete": ev["end_complete"], "complete_statement": ev["complete_statement"],
        "completeness_note": ev["completeness_note"], "needs_context": ev["needs_context"],
        "needs_context_reason": ev["needs_context_reason"], "expanded": None,
    }, []


def propose(ep: Episode, picks: Any = None, rubric: str | Path | None = None, name: str | None = None, recall_fill: bool = False,
            top_n: int | None = None, min_score: float | None = None) -> dict:
    """Validate agent picks against the transcript and rubric; write the accepted ones to work/clips.json.

    `recall_fill` (the CLI default) also proposes EVERY surfaced section of the candidate pack that does not overlap a pick or a
    human-decided clip, scored by the cheap heuristics (`origin: heuristic`), so the human sees all clip-worthy sections and the
    agent's picks are just the ones it vouches for. With `picks=None` only the surfaced sections are proposed.

    Human-decided clips (approved/rejected, or marked maybe) survive a re-run and also block overlaps. Undecided `proposed` clips are
    replaced by this batch, so the agent can iterate freely before the human reviews. With `name`, only that session's
    clips are touched (ids `<name>-cNN`, each recording the exact edit it came from); stale picks (made from an older
    sentence table) are kept as they are and block nothing.

    Every clip gets the round-2 fields (rank, complete_statement, context_before/after, tighten, ...). `top_n` / `min_score` (or the
    rubric's `recall.top_n` / `recall.min_score`) describe the CUT-OFF, which is only a view: every clip is still written, and the
    result and clips.json say which ones sit above the line.
    """
    name = resolve_name(ep, name)
    if isinstance(picks, dict) and "picks" in picks:
        picks = picks["picks"]
    if picks is None:
        recall_fill = True
        picks = []
    elif not isinstance(picks, list) or (not picks and not recall_fill):
        raise SystemExit("picks.json must be a non-empty JSON list of picks (or {\"picks\": [...]}).")
    cand = load_candidates(ep, name)
    r = load_rubric(ep, rubric or cand["params"].get("rubric"))
    S, _ = _live_table(ep, cand, r)
    existing = load_clips(ep) or {"clips": []}
    if name and existing["clips"] and "sessions" not in existing:
        raise SystemExit("work/clips.json holds unnamed clips from before sessions; move it aside (or finish with the unnamed commands) before using --name.")
    mine = session_clips(existing, name) if name else existing["clips"]
    others = [c for c in existing["clips"] if c not in mine]
    kept = [c for c in mine if c["status"] != "proposed" or c.get("maybe") or c.get("stale")]
    live = [c for c in kept if not c.get("stale")]
    taken = [(S.by_id[c["sentence_ids"][0]]["i"], S.by_id[c["sentence_ids"][-1]]["i"], f"{c['id']} ({c['status']})")
             for c in live if c["status"] != "rejected" and c["sentence_ids"][0] in S.by_id]
    pat = re.compile(rf"{re.escape(name)}-c(\d+)" if name else r"c(\d+)")
    nums = [int(m[1]) for c in mine if (m := pat.fullmatch(c["id"]))]
    used_slugs = {c["slug"] for c in kept + others}
    accepted, rejected = [], []

    def admit(clip: dict) -> None:
        nid = (max(nums, default=0) + 1)
        nums.append(nid)
        num = f"c{nid:02d}"
        cid = f"{name}-{num}" if name else num
        slug = slugify(clip["title"], num)
        if name:
            slug = f"{name}-{slug}"
        if slug in used_slugs or slug == (f"{name}-selects" if name else "selects"):
            slug = f"{slug}-{num}"
        used_slugs.add(slug)
        i, j = S.by_id[clip["sentence_ids"][0]]["i"], S.by_id[clip["sentence_ids"][-1]]["i"]
        taken.append((i, j, f"{cid} ({clip['title']})"))
        clip.update({"id": cid, "slug": slug, "status": "proposed", "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "edit": None})
        clip.update(finalize_fields(S, clip, cand["params"], r))
        if name:
            clip.update({"session": name, "source": {k: cand["source"][k] for k in ("stage", "name", "version", "fingerprint")}})
        accepted.append(clip)

    for n, pick in enumerate(picks):
        clip, problems = evaluate_pick(S, pick, cand["params"], r, taken)
        label = pick.get("title") if isinstance(pick, dict) else None
        if problems:
            rejected.append({"index": n, "title": label, "problems": problems})
            continue
        admit(clip)
    n_agent = len(accepted)
    if recall_fill:
        # a section the human already skipped is not surfaced again by the automatic fill (an agent can still re-pick it on purpose)
        skipped = [(S.by_id[c["sentence_ids"][0]]["i"], S.by_id[c["sentence_ids"][-1]]["i"]) for c in live
                   if c["status"] == "rejected" and c["sentence_ids"][0] in S.by_id]
        for w in cand["windows"]:       # best first
            i, j = S.by_id.get(w["start_sid"], {}).get("i"), S.by_id.get(w["end_sid"], {}).get("i")
            if i is None or j is None or any(_overlaps(i, j, (a, b)) for a, b, _ in taken) or any(_overlaps(i, j, r) for r in skipped):
                continue
            clip, _ = auto_clip(S, w, cand["params"], r)
            if clip:
                admit(clip)
    for c in live:                    # clips kept from an earlier run (human decisions) get the round-2 fields too, ranges and scores untouched
        if "complete_statement" not in c and c["sentence_ids"][0] in S.by_id and c["sentence_ids"][-1] in S.by_id:
            i0, j0 = S.by_id[c["sentence_ids"][0]]["i"], S.by_id[c["sentence_ids"][-1]]["i"]
            sn0 = S.snap(i0, j0)
            if sn0:
                c.update({k: v for k, v in recall.window_profile(S, i0, j0, sn0).items() if k in ("start_complete", "end_complete", "complete_statement",
                                                                                                  "completeness_note", "needs_context")})
                c.update(finalize_fields(S, c, cand["params"], r))
    sel_n, sel_s = selection_config(r, top_n, min_score)
    if name:
        clips = sorted(others + kept + accepted, key=lambda c: (c.get("session") or "", c["start"]))
        sess = dict(existing.get("sessions") or {})
        sess[name] = {"source": cand["source"], "params": cand["params"], "selects": (sess.get(name) or {}).get("selects")}
        data = {"version": 1, "sessions": sess, "rubric": {k: v for k, v in r.items() if k != "_path"}, "rubric_path": r["_path"],
                "clips": clips, "rejected_picks": rejected}
    else:
        clips = sorted(kept + accepted, key=lambda c: c["start"])
        data = {"version": 1, "source": cand["source"], "params": cand["params"],
                "rubric": {k: v for k, v in r.items() if k != "_path"}, "rubric_path": r["_path"],
                "clips": clips, "rejected_picks": rejected, "selects": (existing.get("selects") if existing else None)}
    assign_ranks(data)
    view = selection_view(session_clips(data, name) if name else data["clips"], sel_n, sel_s)
    holder = data["sessions"][name] if name else data
    if sel_n is not None or sel_s is not None:           # informational (the UI/agent can see the last cut-off); nothing reads it implicitly
        holder["selection"] = {k: view[k] for k in ("top_n", "min_score", "cut_line")}
    else:
        holder.pop("selection", None)
    if accepted or not (existing and existing["clips"]):
        write_json(ep.artifact(CLIPS), data)
    flag = f" --name {name}" if name else ""
    shown = set(view["shown"])
    return {"accepted": [{**{k: c[k] for k in ("id", "slug", "title", "start", "end", "duration", "total", "warnings")},
                          "origin": c.get("origin"), "tags": c.get("tags", []), "rank": c.get("rank"),
                          "complete_statement": c.get("complete_statement"), "in_view": c["id"] in shown} for c in accepted],
            "selection": {"top_n": sel_n, "min_score": sel_s, "shown": len(view["shown"]), "below_the_line": len(view["hidden"]),
                          "cut_line": view["cut_line"], "note": "a view only: every clip is kept in clips.json; change it with --top / --min-score"},
            "agent_picks": n_agent, "recall_added": len(accepted) - n_agent,
            "rejected": rejected, "kept_human_decisions": [c["id"] for c in kept], "clips_json": str(ep.artifact(CLIPS)),
            "next": ("Fix rejected picks and re-run propose, or: " if rejected else "") + f"ave clips review <episode>{flag}"}


# ---- decide ------------------------------------------------------------------------------------------------

def _log(ep: Episode, record: dict) -> None:
    p = ep.artifact(DECISIONS_LOG)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _apply_range(S: Sentences, clip: dict, i: int, j: int, params: dict, rubric: dict, others: list[tuple[int, int, str]]) -> tuple[dict | None, str | None]:
    """Re-snap a clip to sentences i..j and rescore it. Returns (updated clip fields, error)."""
    if not 0 <= i <= j < len(S.items):
        return None, f"nudge leaves the transcript (valid sentences s000..s{len(S.items) - 1:03d})"
    sn = S.snap(i, j)
    if sn is None:
        return None, "nudged range contains only fillers"
    lim = rubric["limits"]
    if not lim["hard_min_s"] <= sn["duration"] <= lim["hard_max_s"]:
        return None, f"nudged clip would be {sn['duration']:.1f}s, outside {lim['hard_min_s']:g}-{lim['hard_max_s']:g}s"
    for a, b, who in others:
        if _overlaps(i, j, (a, b)):
            return None, f"nudged range would overlap {who}"
    sc = score_clip(S, i, j, sn, clip["inputs"], params, rubric)
    warn = list(sc["warnings"])
    toks = norm_tokens(S.text(i, j, sn))
    for key in ("hook_quote", "end_quote"):
        if clip.get(key) and not _quote_in(clip[key], toks):
            warn.append(f"{key} is no longer inside the clip after the nudge")
    upd = {"sentence_ids": [S.items[k]["id"] for k in range(i, j + 1)], "start": sn["start"], "end": sn["end"],
           "duration": sn["duration"], "speakers": sorted({s["speaker"] for s in S.items[i:j + 1] if s["speaker"]}),
           "text": S.text(i, j, sn), "trimmed": {"leading": sn["trimmed_leading"], "trailing": sn["trimmed_trailing"]},
           "weighted": sc["weighted"], "penalties": sc["penalties"], "total": sc["total"], "warnings": warn,
           **recall.window_profile(S, i, j, sn)}
    upd["reason"] = recall.reason_line(upd, S.items[i]["features"], sn["duration"], sc["penalties"])
    upd["expanded"] = None                                     # a human nudge is explicit: the range is exactly what they asked for
    upd.update(finalize_fields(S, {**clip, **upd}, params, rubric))
    return upd, None


_VERDICT_KIND = {"keep": "approve", "skip": "reject", "maybe": "maybe"}
_KIND_VERDICT = {"approve": "keep", "reject": "skip", "maybe": "maybe"}


def decide(ep: Episode, decisions: Any, name: str | None = None, by: str | None = None, source: str = "cli") -> dict:
    """Apply human decisions. This is the ONE code path that writes a clip's status AND the learning ledger.

    Each item: {"id" (or "clip_id"), and either "verdict": "keep|maybe|skip" or the older "decision": "approve|reject|nudge|pending",
    "reasons": [..] (vocabulary in config/clip_reasons.yaml), "note", "nudge": {"start":-1,"end":2}}.
    keep = approve (status approved), skip = reject (status rejected), maybe stays `proposed` with `maybe: true`; every
    keep/maybe/skip (old approve/reject included) appends a line to work/learning/clip_decisions.jsonl with the clip's features.

    `nudge.start`/`nudge.end` move the boundary by that many SENTENCES (start -1 = begin one sentence earlier, end +2 = run
    two sentences longer) and the clip is re-snapped and re-scored. A nudged clip that is not also decided goes back to `proposed`
    for another look. Every decision (applied or not) is appended to work/clips_decisions.jsonl.
    With `name`, only that session's clips can be decided. A stale clip (picked from an older sentence table) can only be
    rejected or set back to pending: approving or nudging it would trust sentence ids that no longer mean what they did.
    """
    from . import learning
    name = resolve_name(ep, name)
    if isinstance(decisions, dict) and "decisions" in decisions:
        decisions = decisions["decisions"]
    if not isinstance(decisions, list):
        raise SystemExit("decisions.json must be a JSON list (or {\"decisions\": [...]}).")
    data = load_clips(ep)
    if not data:
        raise SystemExit("No clips yet. Run: ave clips propose <episode> --picks picks.json")
    cand = load_candidates(ep, name)
    r = data["rubric"]
    S, _ = _live_table(ep, cand, r)
    params = data["sessions"][name]["params"] if name else data["params"]
    by_id = {c["id"]: c for c in data["clips"]}
    vocab = learning.load_reasons(ep)
    applied, errors = [], []
    for d in decisions:
        dd = d if isinstance(d, dict) else {}
        cid = dd.get("id") or dd.get("clip_id")
        verdict, kind = dd.get("verdict"), dd.get("decision")
        if verdict is not None:
            kind = _VERDICT_KIND.get(verdict, f"?{verdict}")
        reasons = dd.get("reasons") or []
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "clip": cid, "decision": kind, "nudge": dd.get("nudge"), "note": dd.get("note")}
        err = None
        clip = by_id.get(cid)
        if clip is None:
            err = f"unknown clip id {cid!r} (have {sorted(c for c in by_id if not name or by_id[c].get('session') == name)})"
        elif name and clip.get("session") != name:
            err = f"clip {cid} belongs to session {clip.get('session')}, not {name}: decide it with --name {clip.get('session')}"
        elif kind not in ("approve", "reject", "nudge", "pending") and not (verdict is not None and kind == "maybe"):
            err = f"decision must be approve, reject, nudge or pending (or verdict keep, maybe, skip), got {verdict or kind!r}"
        elif not isinstance(reasons, list) or any(x not in vocab for x in reasons):
            err = f"reasons must be a list drawn from: {', '.join(vocab)}"
        else:
            if name:
                rec["session"] = name
            rec.update(title=clip["title"], before={"status": clip["status"], "sentence_ids": [clip["sentence_ids"][0], clip["sentence_ids"][-1]],
                                                    "total": clip["total"], "scores": clip["inputs"]["scores"]})
            nudge = rec["nudge"] or {}
            ds, de = int(nudge.get("start", 0) or 0), int(nudge.get("end", 0) or 0)
            if clip.get("stale") and (kind in ("approve", "maybe", "nudge") or ds or de) and kind != "reject":
                err = f"clip {cid} is stale: {clip['stale_reason']}"
            elif (ds or de) and kind != "reject":
                i0, j0 = S.by_id[clip["sentence_ids"][0]]["i"], S.by_id[clip["sentence_ids"][-1]]["i"]
                others = [(S.by_id[c["sentence_ids"][0]]["i"], S.by_id[c["sentence_ids"][-1]]["i"], c["id"])
                          for c in session_clips(data, name) if c is not clip and c["status"] != "rejected" and not c.get("stale")]
                upd, err = _apply_range(S, clip, i0 + ds, j0 + de, params, r, others)
                if not err:
                    clip.update(upd)
            if not err:
                if kind in ("approve", "reject"):
                    clip["status"] = "approved" if kind == "approve" else "rejected"
                    clip.pop("maybe", None)
                elif kind == "maybe":
                    clip["status"] = "proposed"
                    clip["maybe"] = True
                elif kind == "pending":
                    clip["status"] = "proposed"
                    clip.pop("maybe", None)
                else:                                  # nudge only: back to proposed for another look
                    clip["status"] = "proposed"
                if rec["note"]:
                    clip.setdefault("notes", []).append(rec["note"])
                clip["decided"] = rec["ts"]
                rec["after"] = {"status": clip["status"], "sentence_ids": [clip["sentence_ids"][0], clip["sentence_ids"][-1]],
                                "total": clip["total"], "duration": clip["duration"]}
                if kind in _KIND_VERDICT:
                    learning.record_decision(ep, clip["id"], _KIND_VERDICT[kind], reasons, rec["note"], by=by or dd.get("by"), source=source,
                                             session=clip.get("session"), edit=learning.edit_label(data, clip), features=learning.clip_features(clip))
        rec["applied"] = err is None
        rec["error"] = err
        _log(ep, rec)
        (errors if err else applied).append({"id": cid, "error": err} if err else {"id": cid, "status": clip["status"], "duration": clip["duration"], "total": clip["total"]})
    data["clips"].sort(key=(lambda c: (c.get("session") or "", c["start"])) if name else (lambda c: c["start"]))
    assign_ranks(data)                                     # a nudge changes a clip's total, so the ranking is recomputed
    write_json(ep.artifact(CLIPS), data)
    return {"applied": applied, "errors": errors, "approved": [c["id"] for c in session_clips(data, name) if c["status"] == "approved"],
            "log": str(ep.artifact(DECISIONS_LOG)), "ledger": str(ep.artifact("learning/clip_decisions.jsonl"))}


# ---- list ----------------------------------------------------------------------------------------------------

def list_rows(ep: Episode, name: str | None = None, show_all: bool = False, top_n: int | None = None,
              min_score: float | None = None) -> list[dict] | dict:
    """Compact rows for `ave clips list`. On a multi-session episode a name (or show_all) is required; `newer_edit` is set
    when the session's edit has a later version than the one the clip was picked from."""
    if not show_all:
        name = resolve_name(ep, name)
    d = load_clips(ep)
    if not d:
        raise SystemExit("No clips yet. Run: ave clips propose <episode> --picks picks.json" + (f" --name {name}" if name else ""))
    rows = []
    for c in (d["clips"] if show_all or not name else session_clips(d, name)):
        row = {k: c[k] for k in ("id", "slug", "title", "status", "total", "duration", "start", "end")}
        row.update(tags=c.get("tags", []), origin=c.get("origin"), maybe=bool(c.get("maybe")), rank=c.get("rank"),
                   complete_statement=c.get("complete_statement"), tight_duration=c.get("tight_duration"))
        if c.get("session"):
            src = c["source"]
            row.update(session=c["session"], stale=bool(c.get("stale")))
            latest = (editdoc.list_versions(ep, src["stage"], src["name"]) or [src["version"]])[-1]
            row["newer_edit"] = latest if latest > src["version"] else None
        rows.append(row)
    top_n, min_score = selection_config(_current_rubric(ep, d), top_n, min_score)
    if top_n is None and min_score is None:
        return rows
    # the cut-off view: best first, a marker row where the line falls, per session. Nothing is deleted.
    out, views = [], []
    for sess in dict.fromkeys(r.get("session") for r in rows):
        mine = sorted((r for r in rows if r.get("session") == sess), key=lambda r: (r["rank"] is None, r["rank"] or 0))
        v = selection_view(mine, top_n, min_score)
        views.append({**({"session": sess} if sess else {}), "shown": len(v["shown"]), "below_the_line": len(v["hidden"]), "cut_line": v["cut_line"]})
        cut_done = False
        for r in mine:
            r["in_view"] = r["id"] in v["shown"]
            if not r["in_view"] and not cut_done:
                out.append({"cut_line": v["cut_line"]})
                cut_done = True
            out.append(r)
        if not cut_done:
            out.append({"cut_line": v["cut_line"]})
    return {"selection": views, "rows": out}


# ---- build -------------------------------------------------------------------------------------------------

def concat_docs(docs: list[tuple[str, dict]], gap_s: float, notes: str = "") -> dict:
    """Concatenate EditDocs (all of one stage's lineage) into one valid doc with a `gap_s` room-tone gap between them.

    The picture holds on the previous clip's last frame range through the gap (the video track must stay gap-free); the
    dialogue gets a `{"fill": "room_tone"}` segment. Ids are prefixed with the clip label so they stay unique.
    """
    first = docs[0][1]
    out = editdoc.new_doc("select", first["fps"], (first["canvas"]["w"], first["canvas"]["h"]), {}, 0.0)
    out["notes"] = notes
    out["tracks"]["room_tone"] = copy.deepcopy(first["tracks"].get("room_tone"))
    t = 0.0
    for n, (label, d) in enumerate(docs):
        if d["fps"] != first["fps"] or d["canvas"] != first["canvas"]:
            raise ValueError(f"{label}: fps/canvas differ from {docs[0][0]}; cannot concatenate")
        for k, s in d["sources"].items():
            if out["sources"].setdefault(k, s) != s:
                raise ValueError(f"{label}: source {k} differs from an earlier clip")
        if out["tracks"]["room_tone"] is None and d["tracks"].get("room_tone"):
            out["tracks"]["room_tone"] = copy.deepcopy(d["tracks"]["room_tone"])
        for name in ("video", "dialogue", "music", "overlays"):
            for s in d["tracks"].get(name) or []:
                s2 = dict(s)
                s2["id"] = f"{label}_{s['id']}"
                s2["o0"], s2["o1"] = round(s["o0"] + t, 6), round(s["o1"] + t, 6)
                out["tracks"][name].append(s2)
        end = t + d["duration"]
        if n < len(docs) - 1 and gap_s > 0:
            v_last = out["tracks"]["video"][-1]
            v_last["o1"] = round(end + gap_s, 6)
            dlg = out["tracks"]["dialogue"][-1]
            out["tracks"]["dialogue"].append({"id": f"{label}_gap", "src": dlg["src"], "o0": round(end, 6), "o1": round(end + gap_s, 6),
                                              "e0": dlg["e0"] + (dlg["o1"] - dlg["o0"]), "xfade_ms": 0, "gain_db": 0.0,
                                              "fill": "room_tone", "reason": "gap between selects", "confidence": 1.0})
            end += gap_s
        t = end
    out["duration"] = round(t, 6)
    out["tracks"]["video"][-1]["o1"] = out["duration"]
    out["tracks"]["dialogue"][-1]["o1"] = out["duration"]
    for name in ("video", "dialogue"):
        out["tracks"][name][0]["o0"] = 0.0
    return out


def _clip_notes(c: dict, src: dict) -> str:
    return (f"{c['title']} [{c['id']}, {c['status']}] score {c['total']:.0f}/100\nhook: {c['hook']}\nwhy: {c['rationale']}\n"
            f"sentences {c['sentence_ids'][0]}-{c['sentence_ids'][-1]} of {_label(src['stage'], src.get('name'))} v{src['version']}, output {c['start']:.2f}-{c['end']:.2f}s\n"
            + "\n".join(f"flag: {w}" for w in c.get("warnings", [])))


def _source_docs(ep: Episode, rubric: dict, chosen: list[dict]) -> dict[tuple, dict]:
    """The exact edit each named-session clip was picked from, keyed (stage, name, version), proven unchanged by its fingerprint."""
    docs: dict[tuple, dict] = {}
    for c in chosen:
        src = c["source"]
        key = (src["stage"], src["name"], src["version"])
        if key in docs:
            continue
        doc, _, _ = load_stage_doc(ep, src["stage"], src["version"], src["name"])
        if Sentences(ep, doc, rubric).fingerprint() != src["fingerprint"]:
            raise SystemExit(f"{c['id']} is stale: the transcript or {_label(src['stage'], src['name'])} v{src['version']} changed since it was "
                             "picked. Re-run `ave clips prepare` and re-pick (sentence ids are only meaningful for the table they were issued from).")
        docs[key] = doc
    return docs


def build(ep: Episode, approved_only: bool = False, gap_s: float | None = None, order: str = "time", force: bool = False,
          name: str | None = None, include_stale: bool = False, top_n: int | None = None, min_score: float | None = None) -> dict:
    """One `select.<slug>` EditDoc per clip (sliced from the pinned source stage) plus the combined `select.selects`.

    With `name`: the session's clips only, each cut from the exact edit recorded in its `source` (a newer rough does not
    change it), and the combined timeline is `select.<name>-selects`. Stale clips are skipped unless `include_stale`."""
    name = resolve_name(ep, name)
    data = load_clips(ep)
    if not data:
        raise SystemExit("No clips yet. Run: ave clips propose <episode> --picks picks.json")
    if order not in ("time", "score"):
        raise SystemExit("--order must be time or score")
    r = data["rubric"]
    if name:
        pool = session_clips(data, name)
        if not pool:
            raise SystemExit(f"No clips for session {name} yet. Run: ave clips propose <episode> --picks picks.json --name {name}")
        sel_holder = data["sessions"][name]
        sel_key, sel_name = "selects", f"{name}-selects"
    else:
        cand = load_candidates(ep)
        _live_table(ep, cand, r)  # refuses if the source moved
        pool = data["clips"]
        sel_holder, sel_key, sel_name = data, "selects", "selects"
    chosen = [c for c in pool if (c["status"] == "approved" or (not approved_only and c["status"] == "proposed"))
              and (include_stale or not c.get("stale"))]
    top_n, min_score = selection_config(_current_rubric(ep, data), top_n, min_score)
    if top_n is not None or min_score is not None:        # build only what is above the line (a view: the others stay in clips.json)
        shown: set[str] = set()
        for sess in dict.fromkeys(c.get("session") for c in pool):
            mine = [c for c in pool if c.get("session") == sess]
            ranks = rank_map(mine)
            shown |= set(selection_view([{"id": c["id"], "rank": ranks.get(c["id"]), "total": c["total"]} for c in mine], top_n, min_score)["shown"])
        chosen = [c for c in chosen if c["id"] in shown]
    skipped_stale = [c["id"] for c in pool if c.get("stale") and not include_stale
                     and (c["status"] == "approved" or (not approved_only and c["status"] == "proposed"))]
    if name:
        docs = _source_docs(ep, r, chosen)
        doc_of = lambda c: docs[(c["source"]["stage"], c["source"]["name"], c["source"]["version"])]   # noqa: E731
        src_of = lambda c: c["source"]                                                                  # noqa: E731
    else:
        src = data["source"]
        doc, _, _ = load_stage_doc(ep, src["stage"], src["version"])
        docs = {None: doc}
        doc_of, src_of = (lambda c: doc), (lambda c: src)                                               # noqa: E731
    gap = float(gap_s if gap_s is not None else r["selects"]["gap_s"])
    warnings = []
    if gap > 0 and not all(d["tracks"].get("room_tone") for d in docs.values()):
        # The compiler fills gaps from tracks.room_tone and refuses a fill without one, so butt-join instead of faking a gap.
        first = src_of(chosen[0]) if chosen else {"stage": "rough"}
        warnings.append(f"source {first['stage']} edit has no tracks.room_tone: the {gap:g}s gaps between selects were skipped (clips are butt-joined). "
                        "Re-run `ave cleanup` (it now picks a quiet stretch automatically) or add a room_tone range to the edit by hand, then rebuild with --force to get gaps")
        gap = 0.0
    if not chosen:
        if name and skipped_stale:
            raise SystemExit(f"Every candidate clip of session {name} is stale ({', '.join(skipped_stale)}): re-pick them after "
                             "`ave clips prepare`, or build the old picks with --include-stale.")
        raise SystemExit("No clips to build" + (" (none approved yet; omit --approved-only to build proposals)." if approved_only else "."))
    built, skipped = [], []
    for c in chosen:
        src = src_of(c)
        e = c.get("edit")
        same = (e and e["range"] == [c["start"], c["end"]] and e["source"] == [src["stage"], src["version"]]
                and (ep.root / e["path"]).exists())
        if same and not force:
            skipped.append(c["id"])
            continue
        sl = editdoc.slice_doc(doc_of(c), c["start"], c["end"], stage="select")
        if sl["tracks"]["dialogue"]:
            sl["tracks"]["dialogue"][0]["xfade_ms"] = 0   # nothing before the first segment to crossfade with
        sl["notes"] = _clip_notes(c, src)
        path = editdoc.save_new_version(ep, sl, name=c["slug"])
        c["edit"] = {"name": c["slug"], "version": editdoc.list_versions(ep, "select", c["slug"])[-1], "path": str(path.relative_to(ep.root)),
                     "range": [c["start"], c["end"]], "source": [src["stage"], src["version"]]}
        built.append(c["id"])
    members = sorted(chosen, key=(lambda c: -c["total"]) if order == "score" else (lambda c: c["start"]))
    sig = {"members": [[c["id"], c["edit"]["version"]] for c in members], "gap_s": gap, "order": order}
    prev = sel_holder.get(sel_key)
    if prev and prev.get("sig") == sig and (ep.root / prev["path"]).exists() and not force:
        sel_path = ep.root / prev["path"]
    else:
        cdocs = [(c["id"], editdoc.load(ep.root / c["edit"]["path"])) for c in members]
        notes = "Selects: " + "; ".join(f"{c['id']} {c['title']} ({c['duration']:.0f}s)" for c in members)
        sel_path = editdoc.save_new_version(ep, concat_docs(cdocs, gap, notes), name=sel_name)
        sel_holder[sel_key] = {"path": str(sel_path.relative_to(ep.root)), "version": editdoc.list_versions(ep, "select", sel_name)[-1],
                               "sig": sig, "duration": editdoc.load(sel_path)["duration"]}
    write_json(ep.artifact(CLIPS), data)
    out = {"built": built, "unchanged": skipped, "clips": {c["id"]: c["edit"]["path"] for c in chosen},
           "selects": sel_holder[sel_key]["path"], "selects_duration": sel_holder[sel_key]["duration"], "warnings": warnings}
    if name:
        out = {"name": name, **out, "stale_skipped": skipped_stale}
    return out
