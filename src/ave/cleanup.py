"""Cleanup: assembly EditDoc -> rough cut (dead air, fillers, repeats, false starts) with every splice masked.

Why this shape. An editor trusts a cleanup tool only if (1) it never cuts inside a word, (2) it can say why it made
every cut, and (3) the splices do not show. So:

  * Detection is transcript-first. A cut is always a span BETWEEN words (or exactly a removed word's span); the
    allowed positions for each splice point are computed from word boundaries before any audio is looked at.
  * Audio refines, never overrules: each splice point slides to the quietest spot inside its allowed window
    (`snap.window_ms`), then to a zero crossing, so the join lands in a valley instead of on a consonant.
  * Masking follows the editor's priority order (see `_mask_cut`): camera already changes -> nothing; else cut away
    to the other camera; else a wide; else a punch-in; else audio crossfade only and `needs_review`.
  * Every decision records `reason` and `confidence`; every number is in the policy YAML (config/cleanup.default.yaml).

Time bases: transcripts and cuts are EPISODE time. EditDoc tracks are OUTPUT time; `editdoc.TimeMap` converts.
Cut points are exposed at full float precision; compilers snap video to frames and keep audio sample-accurate.
Dialogue segments abut at a cut (o1 of one == o0 of the next) and carry `xfade_ms` on the segment AFTER the cut: the
compiler crossfades across the seam using the source handles that exist because the cut removed material.
"""

from __future__ import annotations

import bisect
import copy
import re
import time
from pathlib import Path

import numpy as np
import yaml

from . import audio, config, cutwords, editdoc, sessions, state, zoomstate
from .editdoc import EPS, TimeMap
from .episode import Episode, read_json, write_json

DEFAULT_POLICY = config.REPO_ROOT / "config" / "cleanup.default.yaml"
SENT_END = ".!?"
DASHES = ("-", "–", "—")
OVERRIDES = "cleanup_overrides.json"
PACING_CAMERAS = ("wide", "listener", "punch_in")


# ---- policy -----------------------------------------------------------------------------------------------------

def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def validate_policy(p: dict) -> None:
    """Raise ValueError naming the broken key. Catches the mistakes that would silently produce bad cuts."""
    pa = p["pause"]
    if not (0 < pa["floor_ms"] <= pa["target_ms"] <= pa["max_ms"]):
        raise ValueError("policy pause: need 0 < floor_ms <= target_ms <= max_ms "
                         f"(got floor {pa['floor_ms']}, target {pa['target_ms']}, max {pa['max_ms']})")
    if not 0 <= pa["tail_ratio"] <= 1:
        raise ValueError("policy pause.tail_ratio must be between 0 and 1")
    m = p["masks"]
    if m["min_hold_s"] <= 0 or m["cutaway_s"] < m["min_hold_s"]:
        raise ValueError("policy masks: need min_hold_s > 0 and cutaway_s >= min_hold_s")
    if any(z < 1.0 for z in m["zooms"]) or not m["zooms"]:
        raise ValueError("policy masks.zooms must be a non-empty list of factors >= 1.0")
    if p["snap"]["window_ms"] < 0:
        raise ValueError("policy snap.window_ms must be >= 0")
    if m.get("zoom_min_dwell_s", 0) < 0 or m.get("max_ins_per_shot", 1) < 0:
        raise ValueError("policy masks.zoom_min_dwell_s and masks.max_ins_per_shot must be >= 0")
    if pa.get("word_tail_pad_ms", 0) < 0 or pa.get("word_lead_pad_ms", 0) < 0:
        raise ValueError("policy pause.word_tail_pad_ms and word_lead_pad_ms must be >= 0")
    pc = p.get("pacing")
    if pc:
        r = pc["reaction"]
        if not (0 < r["min_s"] <= r["prefer_s"] <= r["max_s"]):
            raise ValueError("policy pacing.reaction: need 0 < min_s <= prefer_s <= max_s "
                             f"(got {r['min_s']}, {r['prefer_s']}, {r['max_s']})")
        if pc["max_hold_s"] <= r["min_gap_after_cut_s"] * 2 + r["min_s"] or r["min_gap_after_cut_s"] < 0:
            raise ValueError("policy pacing: max_hold_s must leave room for a reaction plus min_gap_after_cut_s on each side")
        bad = [c for c in pc["camera_order"] if c not in PACING_CAMERAS]
        if bad or not pc["camera_order"]:
            raise ValueError(f"policy pacing.camera_order must be a non-empty list of {PACING_CAMERAS} (got {pc['camera_order']})")
        if pc["max_cuts_per_min"] <= 0 or pc["rate_window_s"] <= 0:
            raise ValueError("policy pacing: max_cuts_per_min and rate_window_s must be > 0")
        if any(z < 1.0 for z in pc["punch_zooms"]) or not pc["punch_zooms"]:
            raise ValueError("policy pacing.punch_zooms must be a non-empty list of factors >= 1.0")
        if pc.get("zoom_min_dwell_s", 0) < 0:
            raise ValueError("policy pacing.zoom_min_dwell_s must be >= 0")


def load_policy(path: Path | str | None = None) -> dict:
    """The default policy, with the YAML at `path` (if any) merged over it. Validated."""
    base = yaml.safe_load(DEFAULT_POLICY.read_text())
    if path:
        base = _deep_merge(base, yaml.safe_load(Path(path).read_text()) or {})
    validate_policy(base)
    return base


def resolve_policy(ep: Episode, explicit: Path | str | None = None, home: Path | None = None) -> tuple[dict, str]:
    """--policy > <ep>/policy/cleanup.yaml > ~/.config/ave/cleanup.yaml > default. Returns (policy, source label)."""
    home = Path(home) if home else Path.home()
    if explicit:
        if not Path(explicit).is_file():
            raise FileNotFoundError(f"policy file not found: {explicit}")
        return load_policy(explicit), str(Path(explicit).resolve())
    for cand in (ep.root / "policy" / "cleanup.yaml", home / ".config" / "ave" / "cleanup.yaml"):
        if cand.is_file():
            return load_policy(cand), str(cand)
    return load_policy(), str(DEFAULT_POLICY)


# ---- words ------------------------------------------------------------------------------------------------------

_NORM = re.compile(r"[^a-z0-9'\-]+")


def norm(token: str) -> str:
    """Lowercase, punctuation stripped (inner hyphens/apostrophes kept: "mm-hm", "don't"; trailing dashes dropped)."""
    return _NORM.sub("", token.lower()).strip("-")


def prepare_words(transcript: dict) -> list[dict]:
    """Words sorted by start, each with `i` (position here) and `ti` (index in transcript.json, used by overrides)."""
    raw = [dict(w, ti=k) for k, w in enumerate(transcript["words"])]
    raw.sort(key=lambda w: (w["start"], w["end"]))
    return [{"i": i, "ti": w["ti"], "w": w["w"], "n": norm(w["w"]), "start": float(w["start"]), "end": float(w["end"]),
             "speaker": w.get("speaker"), "conf": float(w.get("conf", 1.0))} for i, w in enumerate(raw)]


def restrict_words(words: list[dict], spans: list[tuple[float, float]]) -> list[dict]:
    """Keep the words whose midpoint lies in one of the episode-time `spans`; renumber `i` (position) and keep `ti` (transcript index)."""
    kept = [w for w in words if any(a <= (w["start"] + w["end"]) / 2 <= b for a, b in spans)]
    return [dict(w, i=i) for i, w in enumerate(kept)]


def _span(words: list[dict], idx: list[int]) -> tuple[float, float]:
    return min(words[i]["start"] for i in idx), max(words[i]["end"] for i in idx)


def _text(words: list[dict], idx: list[int]) -> str:
    return " ".join(words[i]["w"] for i in idx)


# ---- detection: word-level candidates -----------------------------------------------------------------------------

def detect_fillers(words: list[dict], pol: dict) -> list[dict]:
    """Lexical fillers (um/uh/...) -> cut; review-only phrases (like, you know) -> flag when they cluster."""
    f = pol["fillers"]
    cut_set = {norm(x) for x in f["cut"]}
    out: list[dict] = []
    for w in words:
        if w["n"] not in cut_set:
            continue
        dur = w["end"] - w["start"]
        if dur > f["max_cut_s"]:
            out.append(dict(type="filler", idx=[w["i"]], action="flag", confidence=0.4, review=True,
                            reason=f'"{w["w"]}" lasts {dur:.2f}s (> fillers.max_cut_s {f["max_cut_s"]}s): probably real speech, not cut'))
        else:
            conf = round(min(0.95, 0.6 + 0.35 * w["conf"]), 3)
            low = w["conf"] < f["min_conf"]
            out.append(dict(type="filler", idx=[w["i"]], action="cut", confidence=conf, review=low,
                            reason=f'lexical filler "{w["w"]}" ({dur * 1000:.0f} ms)' + (" (low ASR confidence: check)" if low else "")))
    # Review-only phrases: only clusters are interesting; a lone "like" is just English.
    phrases = [p.split() for p in (norm(x) if " " not in x else " ".join(norm(t) for t in x.split()) for x in f["review"])]
    occ: list[tuple[float, list[int], str]] = []
    for w in words:
        for ph in phrases:
            idx = list(range(w["i"], w["i"] + len(ph)))
            if idx[-1] >= len(words):
                continue
            if [words[k]["n"] for k in idx] == ph and len({words[k]["speaker"] for k in idx}) == 1:
                occ.append((w["start"], idx, " ".join(ph)))
    for t, idx, ph in occ:
        near = [o for o in occ if abs(o[0] - t) <= f["review_window_s"]]
        if len(near) >= f["review_min_cluster"]:
            out.append(dict(type="review_phrase", idx=idx, action="flag", confidence=0.5, review=True,
                            reason=f'"{ph}" x{len(near)} within {f["review_window_s"]:g}s: may be a verbal tic; ambiguous so never auto-cut'))
    return out


def detect_repeats(words: list[dict], pol: dict, claimed: set[int]) -> list[dict]:
    """"the the" / "we need to we need to": remove the EARLIER copy (the second is the delivered version)."""
    r = pol["repeats"]
    if not r["enabled"]:
        return []
    allow = {norm(x) for x in r["allow"]}
    out: list[dict] = []
    taken = set(claimed)
    for n in range(r["max_ngram"], 0, -1):
        i = 0
        while i + 2 * n <= len(words):
            a, b = list(range(i, i + n)), list(range(i + n, i + 2 * n))
            ok = (all(words[k]["n"] for k in a + b) and not (set(a) & taken)
                  and [words[k]["n"] for k in a] == [words[k]["n"] for k in b]
                  and len({words[k]["speaker"] for k in a + b}) == 1
                  and words[b[0]]["start"] - words[a[-1]]["end"] <= r["max_gap_ms"] / 1000.0
                  and not (n == 1 and words[i]["n"] in allow))
            if ok:
                out.append(dict(type="repeat", idx=a, action="cut", confidence=0.85 if n == 1 else 0.9, review=False,
                                reason=f'repeated {"word" if n == 1 else f"{n}-word phrase"} "{_text(words, a)}"; the earlier copy is removed'))
                taken |= set(a)
                i += n
            else:
                i += 1
    return out


def detect_false_starts(words: list[dict], pol: dict, claimed: set[int]) -> list[dict]:
    """A cut-off word ("wh-", "we—") ends an abandoned clause. Cut it (and the clause before it) when the speaker restarts."""
    fs = pol["false_starts"]
    if not fs["enabled"]:
        return []
    out: list[dict] = []
    for w in words:
        if not w["w"].rstrip().endswith(DASHES) or w["i"] in claimed:
            continue
        j = w["i"]
        while (j > 0 and w["i"] - j < fs["max_words"] - 1 and words[j - 1]["speaker"] == w["speaker"]
               and words[j]["start"] - words[j - 1]["end"] <= fs["clause_gap_ms"] / 1000.0
               and not words[j - 1]["w"].rstrip().endswith(tuple(SENT_END))):
            j -= 1
        idx = [k for k in range(j, w["i"] + 1) if k not in claimed]
        nxt = words[w["i"] + 1] if w["i"] + 1 < len(words) else None
        restarts = nxt is not None and nxt["speaker"] == w["speaker"] and nxt["n"] == words[idx[0]]["n"]
        if len(idx) == 1 or restarts:
            conf = 0.85 if restarts else 0.8
            out.append(dict(type="false_start", idx=idx, action="cut", confidence=conf, review=False,
                            reason=(f'abandoned start "{_text(words, idx)}" restarted with "{nxt["w"]}"' if restarts
                                    else f'cut-off fragment "{_text(words, idx)}"')))
        else:
            out.append(dict(type="false_start", idx=idx, action="flag", confidence=0.5, review=True,
                            reason=f'possible false start "{_text(words, idx)}" but no clean restart follows: left for a human'))
    return out


def detect_backchannels(words: list[dict], pol: dict, claimed: set[int]) -> list[dict]:
    """Record (action keep) short listener noises from the OTHER speaker so `explain` shows they were considered."""
    keep = {norm(x) for x in pol["backchannels"]["keep"]}
    out = []
    for k, w in enumerate(words):
        if w["n"] in keep and k not in claimed:
            nbr = [words[j]["speaker"] for j in (k - 1, k + 1) if 0 <= j < len(words)]
            if nbr and all(s != w["speaker"] for s in nbr):
                out.append(dict(type="backchannel", idx=[k], action="keep", confidence=0.9, review=False,
                                reason=f'"{w["w"]}" is the listener reacting during the other speaker: kept (backchannels.keep)'))
    return out


def detect_candidates(words: list[dict], pol: dict) -> list[dict]:
    """All word-level decisions, each with `idx`, ids c001... in transcript order. Priority: filler > false start > repeat."""
    fill = detect_fillers(words, pol)
    claimed = {i for c in fill for i in c["idx"]}
    fs = detect_false_starts(words, pol, claimed)
    claimed |= {i for c in fs for i in c["idx"]}
    rep = detect_repeats(words, pol, claimed)
    claimed |= {i for c in rep for i in c["idx"]}
    bc = detect_backchannels(words, pol, claimed)
    cands = sorted(fill + fs + rep + bc, key=lambda c: (c["idx"][0], c["type"]))
    for n, c in enumerate(cands, 1):
        c["id"] = f"c{n:03d}"
    return cands


# ---- overrides (documented in docs/cleanup-policy.md) ---------------------------------------------------------------

def load_overrides(ep: Episode) -> dict:
    p = ep.artifact(OVERRIDES)
    return read_json(p) if p.exists() else {}


def apply_overrides(cands: list[dict], words: list[dict], ov: dict, warnings: list[str]) -> list[dict]:
    """`keep`: veto detected decisions by id or time range. `cut`: add decisions by transcript word indices or time range.

    An LLM or human may write work/cleanup_overrides.json; the command honors it on every run. Added cuts are snapped
    to whole words, so an override can never cause a cut inside a word.
    """
    out = list(cands)
    for k in ov.get("keep", []) or []:
        for c in out:
            e0, e1 = _span(words, c["idx"])
            hit = (k.get("id") == c["id"]) or ("e0" in k and "e1" in k and e0 < k["e1"] and e1 > k["e0"])
            if hit and c["action"] in ("cut", "flag"):
                c.update(action="keep", review=False, confidence=1.0,
                         reason=f'kept by override ({k.get("reason", "no reason given")}); was: {c["reason"]}')
    by_ti = {w["ti"]: w["i"] for w in words}
    for n, a in enumerate(ov.get("cut", []) or [], 1):
        if "words" in a:
            lo, hi = a["words"]
            if lo not in by_ti or hi not in by_ti:
                warnings.append(f"override cut #{n}: word indices {a['words']} are not in the transcript; skipped")
                continue
            idx = list(range(by_ti[lo], by_ti[hi] + 1))
        elif "e0" in a and "e1" in a:
            mid = lambda w: (w["start"] + w["end"]) / 2  # noqa: E731
            idx = [w["i"] for w in words if a["e0"] <= mid(w) <= a["e1"]]
        else:
            warnings.append(f"override cut #{n}: needs `words` [first,last] or `e0`/`e1`; skipped")
            continue
        if not idx:
            warnings.append(f"override cut #{n}: no words in that range; skipped")
            continue
        if "text" in a and a["text"].strip().lower() != _text(words, idx).strip().lower():
            warnings.append(f'override cut #{n}: text "{a["text"]}" does not match transcript "{_text(words, idx)}"; skipped')
            continue
        out = [c for c in out if not (set(c["idx"]) & set(idx))]   # an explicit cut replaces what detection said there
        out.append(dict(id=f"o{n:03d}", type=a.get("type", "override"), idx=idx, action="cut",
                        confidence=float(a.get("confidence", 0.8)), review=bool(a.get("review", False)),
                        reason=f'override: {a.get("reason", "added by cleanup_overrides.json")}'))
    return sorted(out, key=lambda c: c["idx"][0])


# ---- audio: energy envelope and snapping --------------------------------------------------------------------------

class Envelope:
    """Short-time RMS (dB) of the analysis audio, addressed in EPISODE time (source clock = episode - offset)."""

    def __init__(self, samples: np.ndarray, sr: int, offset: float, hop_ms: float = 5, frame_ms: float = 10, drift: float = 0.0,
                 sync_map: list | None = None):
        self.x, self.sr, self.offset = samples.astype(np.float64), sr, float(offset)
        self.k_clock = 1.0 + float(drift)       # file seconds advance 1/k_clock per episode second (editdoc.src_time)
        self.clock = {"start_offset": float(offset), "drift_ppm": float(drift) * 1e6, "sync_map": sync_map}
        hop = max(1, int(sr * hop_ms / 1000))
        win = max(2, int(sr * frame_ms / 1000))
        self.hop_s = hop / sr
        sq = np.concatenate([[0.0], np.cumsum(self.x ** 2)])
        centers = np.arange(len(self.x) // hop + 1) * hop
        lo, hi = np.clip(centers - win // 2, 0, len(self.x)), np.clip(centers + win // 2, 0, len(self.x))
        rms = np.sqrt((sq[hi] - sq[lo]) / np.maximum(hi - lo, 1))
        self.db = 20 * np.log10(rms + 1e-9)
        self.speech_db = float(np.percentile(self.db, 90))
        self.floor_db = float(np.percentile(self.db, 10))

    @classmethod
    def from_wav(cls, path: Path, offset: float, hop_ms: float = 5, frame_ms: float = 10, drift: float = 0.0,
                 sync_map: list | None = None) -> "Envelope":
        import soundfile as sf
        x, sr = sf.read(str(path), dtype="float32", always_2d=True)
        return cls(x[:, 0], sr, offset, hop_ms, frame_ms, drift, sync_map)

    def _file_t(self, e):
        return editdoc.src_time(self.clock, e)

    def _episode_t(self, f):
        return editdoc.episode_time(self.clock, f)

    def _k(self, e: float) -> int:
        return int(round(self._file_t(e) / self.hop_s))

    def t(self, k: int) -> float:
        return self._episode_t(k * self.hop_s)

    def db_at(self, e: float) -> float:
        return float(self.db[min(max(self._k(e), 0), len(self.db) - 1)])

    def valley(self, desired: float, lo: float, hi: float, window_s: float, tol_db: float) -> float:
        """Quietest spot in [lo, hi] ∩ desired±window; among near-equally quiet frames, the one nearest `desired`."""
        a, b = max(lo, desired - window_s), min(hi, desired + window_s)
        if b <= a:
            return min(max(desired, lo), hi)
        ka, kb = max(self._k(a), 0), min(self._k(b), len(self.db) - 1)
        if kb <= ka:
            return min(max(desired, lo), hi)
        seg = self.db[ka:kb + 1]
        ok = np.nonzero(seg <= seg.min() + tol_db)[0] + ka
        k = int(ok[np.argmin(np.abs(self.t(ok) - desired))])
        return min(max(self.t(k), lo), hi)

    def decays(self, a: float, b: float, tol_db: float) -> bool:
        """True if walking from episode time `a` to `b` (either direction) the level never climbs more than `tol_db` above its
        running minimum: the sound only dies away between the two points (a word's tail), no new syllable starts."""
        ka, kb = self._k(a), self._k(b)
        step = 1 if kb >= ka else -1
        lo = min(max(ka, 0), len(self.db) - 1)
        run_min = float("inf")
        for k in range(lo, min(max(kb, 0), len(self.db) - 1) + step, step):
            v = float(self.db[k])
            run_min = min(run_min, v)
            if v > run_min + tol_db:
                return False
        return True

    def zero_cross(self, e: float, lo: float, hi: float, radius_s: float) -> float:
        """Nearest sample where the waveform crosses zero, inside [lo, hi]; unchanged if none."""
        c = int(round(self._file_t(e) * self.sr))
        r = int(radius_s * self.sr)
        a = max(c - r, int(np.ceil(self._file_t(lo) * self.sr)), 0)
        b = min(c + r, int(np.floor(self._file_t(hi) * self.sr)), len(self.x) - 2)
        if b <= a:
            return e
        s = np.sign(self.x[a:b + 1])
        s[s == 0] = 1.0
        cross = np.nonzero(s[:-1] != s[1:])[0] + a
        if not len(cross):
            return e
        k = int(cross[np.argmin(np.abs(cross - c))])
        k += int(abs(self.x[k + 1]) < abs(self.x[k]))     # the sample closer to zero
        return min(max(self._episode_t(k / self.sr), lo), hi)


def _snap(env: Envelope | None, desired: float, lo: float, hi: float, pol: dict) -> float:
    if env is None or hi <= lo:
        return min(max(desired, lo), hi)
    s = pol["snap"]
    t = env.valley(desired, lo, hi, s["window_ms"] / 1000.0, s["valley_tolerance_db"])
    qlim = env.speech_db - s["quiet_db_below_speech"]
    if s.get("widen_if_not_valley") and env.db_at(t) > qlim and hi - lo > 2 * s["window_ms"] / 1000.0:
        wide = env.valley(desired, lo, hi, hi - lo, s["valley_tolerance_db"])      # the whole allowed range, not just desired +- window
        if env.db_at(wide) <= env.db_at(t) - s.get("widen_min_gain_db", 6):
            t = wide
    return env.zero_cross(t, lo, hi, s["zero_cross_ms"] / 1000.0)


def _snap_padded(env: Envelope, desired: float, lo: float, hi: float, pad_lo: float, pad_hi: float, pol: dict) -> float:
    """`_snap`, but when the point it finds is still not in a quiet valley and a pad is allowed, also look in [lo - pad_lo, hi + pad_hi]
    and take that only if it is clearly quieter (snap.widen_min_gain_db)."""
    t = _snap(env, desired, lo, hi, pol)
    qlim = env.speech_db - pol["snap"]["quiet_db_below_speech"]
    if (pad_lo <= 0 and pad_hi <= 0) or env.db_at(t) <= qlim:
        return t
    s = pol["snap"]
    lo2, hi2 = lo - pad_lo, hi + pad_hi
    t2 = env.valley(min(max(desired, lo2), hi2), lo2, hi2, max(s["window_ms"] / 1000.0, pad_lo + pad_hi), s["valley_tolerance_db"])
    t2 = env.zero_cross(t2, lo2, hi2, s["zero_cross_ms"] / 1000.0)
    # Only a REAL valley counts, reached by FOLLOWING THE DECAY of the kept word (energy never climbs back up on the way): a point
    # past a fresh syllable would trade a clipped tail for a stub of the removed word ("the the" -> "the").
    ok = env.db_at(t2) <= qlim and env.db_at(t2) < env.db_at(t) and env.decays(t, t2, s.get("pad_decay_tol_db", 4.0))
    return t2 if ok else t


# ---- regions: where cuts go --------------------------------------------------------------------------------------

def _protected(words: list[dict], prev: int | None, nxt: int | None, pol: dict) -> str | None:
    """Policy-driven heuristics: a pause that is the point (after laughter/emphasis, before a punchline)."""
    pr = pol["protect"]
    if prev is not None:
        tok = words[prev]["w"].strip()
        n = words[prev]["n"]
        for rx in pr["after_regex"]:
            if re.search(rx, tok, re.I) or re.search(rx, n, re.I):
                return f'follows a laughter/emotion marker "{tok}"'
        if tok.endswith(tuple(pr["after_punct"])):
            return f'follows "{tok}", which lands on a beat'
    if nxt is not None:
        ahead = " ".join(words[k]["n"] for k in range(nxt, min(len(words), nxt + pr["lookahead_words"])))
        for ph in pr["before_phrases"]:
            if ahead.startswith(" ".join(norm(t) for t in ph.split())):
                return f'precedes "{ph}", a likely punchline'
    return None


def plan_regions(words: list[dict], removed: set[int], pol: dict) -> tuple[list[dict], list[dict]]:
    """Gaps to act on: runs of removed words, and kept-word gaps longer than pause.max_ms.

    Returns (regions, skipped). Each region has L/U (the kept speech on either side), a_max/b_max (room before/after the
    removed words), G (the silence involved), and the pause decision (R = silence to keep, shrink?, protected reason).
    """
    pa = pol["pause"]
    regions, skipped = [], []
    prev_end, prev_idx, i, n = None, None, 0, len(words)
    while i < n:
        if i in removed:
            j = i
            while j + 1 < n and (j + 1) in removed:
                j += 1
            nxt = j + 1 if j + 1 < n else None
            fs, le = _span(words, list(range(i, j + 1)))
            L = prev_end if prev_end is not None else fs
            U = words[nxt]["start"] if nxt is not None else le
            reg = dict(kind="words", run=(i, j), prev=prev_idx, next=nxt, L=L, U=U, fs=fs, le=le, first_w=words[i], last_w=words[j])
            if L > fs + EPS or U < le - EPS:
                skipped.append(dict(reg, why="overlaps neighbouring speech (another speaker talks over it)"))
            else:
                reg.update(a_max=fs - L, b_max=U - le)
                regions.append(reg)
            i = j + 1
            continue
        if prev_idx is not None and prev_idx == i - 1:
            L, U = prev_end, words[i]["start"]
            if U - L > pa["max_ms"] / 1000.0:
                regions.append(dict(kind="pause", run=None, prev=prev_idx, next=i, L=L, U=U, fs=None, le=None,
                                    a_max=U - L, b_max=U - L))
        prev_end = words[i]["end"] if prev_end is None else max(prev_end, words[i]["end"])
        prev_idx = i
        i += 1
    for r in regions:
        r["G"] = r["a_max"] + r["b_max"] if r["kind"] == "words" else r["U"] - r["L"]
        prot = _protected(words, r["prev"], r["next"], pol)
        r["protected"] = prot
        shrink = r["G"] > pa["max_ms"] / 1000.0
        keep_s = pa["target_ms"] / 1000.0
        if shrink and prot:
            cap = pol["protect"]["cap_ms"] / 1000.0
            shrink, keep_s = r["G"] > cap, cap
        r["shrink"], r["R"] = shrink, (keep_s if shrink else r["G"])
    return regions, skipped


def _cut_for_region(r: dict, env: Envelope | None, pol: dict) -> dict | None:
    """Turn a region into one splice [r0, r1]: desired points from the policy, then valley + zero-crossing snapping."""
    pa = pol["pause"]
    tail, lead = pa["min_tail_ms"] / 1000.0, pa["min_lead_ms"] / 1000.0
    L, U, R = r["L"], r["U"], r["R"]
    if r["kind"] == "pause" and not r["shrink"]:
        return None
    a = min(R * pa["tail_ratio"], r["a_max"])
    b = min(R - a, r["b_max"])
    a = min(R - b, r["a_max"])
    d0, d1 = L + a, U - b                       # desired splice points
    if r["kind"] == "words":
        lo0, hi0 = min(L + tail, r["fs"]), r["fs"]            # r0 may not clip the kept word before it ...
        lo1, hi1 = r["le"], max(U - lead, r["le"])            # ... nor r1 the kept word after, nor leave a stub of the removed word
    else:
        lo0, hi0 = L + tail, U - lead
        lo1, hi1 = L + tail, U - lead
    d0, d1 = min(max(d0, lo0), hi0), min(max(d1, lo1), hi1)
    if d1 - d0 < pa["min_cut_ms"] / 1000.0 and r["kind"] == "pause":
        return None
    # Clipped word edges: ASR word ends/starts are often early/late, so a cut AT the ASR edge can slice the kept word's tail or onset.
    # Where the default point is not in a quiet valley, allow the cut to slide a little INTO the removed word (never more than 40% of it).
    ext0 = ext1 = 0.0
    if r["kind"] == "words" and env is not None:
        width = r["le"] - r["fs"]

        def pad(w: dict, base: str) -> float:
            # A removed word of one or two ASR frames with low confidence next to a kept word is often the kept word's own tail (a "ghost"): the
            # slide may go further through it. A confident real word is never eaten more than the base pad (one 80 ms ASR frame).
            ghost = (w["end"] - w["start"]) * 1000.0 <= pa.get("ghost_max_ms", 160) + 1e-6 and w.get("conf", 1.0) < pa.get("ghost_below_conf", 0.9)
            return pa.get(base + ("_ghost_ms" if ghost else "_ms"), 0) / 1000.0
        ext0 = min(pad(r["first_w"], "word_tail_pad"), 0.4 * width)
        ext1 = min(pad(r["last_w"], "word_lead_pad"), 0.4 * width)
    min_keep = min(r["G"], pa["floor_ms"] / 1000.0)   # never leave less silence than the floor (or than there was)
    if env is None:
        s0, s1 = d0, d1
    else:
        s0 = _snap_padded(env, d0, lo0, hi0, 0.0, ext0, pol)
        if r["kind"] == "pause":      # re-aim the second point so the silence kept still equals the target after the first one moved
            d1 = min(max(U - (R - (s0 - L)), lo1), hi1)
        s1 = _snap_padded(env, d1, lo1, hi1, ext1, 0.0, pol)
        tol = pa["target_tolerance_ms"] / 1000.0 if r["kind"] == "pause" else float("inf")
        combos = ((s0, s1), (s0, d1), (d0, s1), (d0, d1))
        for strict in (True, False):       # first insist the kept silence is within tolerance of the target, then settle for the floor
            pick = next(((c0, c1) for c0, c1 in combos
                         if c1 - c0 > EPS and (c0 - L) + (U - c1) >= min_keep - 1e-6
                         and (r["kind"] == "words" or c1 - c0 >= pa["min_cut_ms"] / 1000.0)
                         and (not strict or abs((c0 - L) + (U - c1) - R) <= tol)), None)
            if pick:
                s0, s1 = pick
                break
    if s1 - s0 < EPS:
        return None
    info = {"desired": [round(d0, 4), round(d1, 4)]}
    if env is not None:
        qlim = env.speech_db - pol["snap"]["quiet_db_below_speech"]
        db0, db1 = env.db_at(s0), env.db_at(s1)
        info.update(db=[round(db0, 1), round(db1, 1)], speech_db=round(env.speech_db, 1), valley=bool(max(db0, db1) <= qlim))
    else:
        info.update(valley=None)
    return {"e0": round(s0, 6), "e1": round(s1, 6), "snap": info, "kept_gap_s": round((s0 - L) + (U - s1), 6)}


def find_nonlexical(words: list[dict], env: Envelope | None, pol: dict) -> list[dict]:
    """Audio bursts inside gaps that the transcript does not explain (breath, click, cough, a missed "um")."""
    nl = pol["nonlexical"]
    if env is None or not nl["detect"]:
        return []
    thr = env.floor_db + nl["threshold_db"]
    out, end = [], None
    for k in range(len(words) - 1):
        end = words[k]["end"] if end is None else max(end, words[k]["end"])
        L, U = end + nl["edge_ms"] / 1000.0, words[k + 1]["start"] - nl["edge_ms"] / 1000.0
        if U - L < (nl["min_gap_ms"] - 2 * nl["edge_ms"]) / 1000.0:
            continue
        ka, kb = env._k(L), env._k(U)
        run = None
        for kk in range(ka, kb + 2):
            hot = kk <= kb and env.db[min(max(kk, 0), len(env.db) - 1)] > thr
            if hot and run is None:
                run = kk
            elif not hot and run is not None:
                t0, t1 = env.t(run), env.t(kk)
                if nl["min_ms"] / 1000.0 <= t1 - t0 <= nl["max_ms"] / 1000.0:
                    out.append({"e0": t0, "e1": t1, "L": L, "U": U, "db": float(env.db[run:kk].max())})
                run = None
    return out


# ---- building cuts and decisions ----------------------------------------------------------------------------------

def build_cuts(words: list[dict], cands: list[dict], env: Envelope | None, pol: dict) -> tuple[list[dict], list[dict], list[str]]:
    """Returns (cuts, decisions, warnings). A cut is one splice; decisions (and review flags) point at it by id."""
    warnings: list[str] = []
    removed = {i for c in cands if c["action"] == "cut" for i in c["idx"]}
    regions, skipped = plan_regions(words, removed, pol)
    skipped_idx = {i for s in skipped for i in range(s["run"][0], s["run"][1] + 1)}
    decisions: list[dict] = []
    cuts: list[dict] = []

    for c in cands:
        e0, e1 = _span(words, c["idx"])
        d = {"id": c["id"], "type": c["type"], "e0": round(e0, 3), "e1": round(e1, 3), "text": _text(words, c["idx"]),
             "action": c["action"], "reason": c["reason"], "confidence": c["confidence"], "mask": None,
             "review": c["review"] or c["action"] == "flag", "cut": None}
        if c["action"] == "cut" and set(c["idx"]) & skipped_idx:
            d.update(action="flag", review=True, confidence=0.3,
                     reason=c["reason"] + "; NOT cut: it overlaps another speaker's words (cutting would remove them)")
        decisions.append(d)
    by_run = {}
    for c, d in zip(cands, decisions):
        if d["action"] == "cut":
            by_run[c["idx"][0]] = d

    pn = 0
    for r in regions:
        cut = _cut_for_region(r, env, pol)
        members: list[dict] = []
        if r["kind"] == "words":
            members = [d for c, d in zip(cands, decisions) if d["action"] == "cut" and c["idx"][0] >= r["run"][0] and c["idx"][-1] <= r["run"][1]]
        if r["kind"] == "pause" or r["shrink"]:
            pn += 1
            dead = {"id": f"p{pn:03d}", "type": "dead_air", "e0": round(r["L"], 3), "e1": round(r["U"], 3), "text": "",
                    "mask": None, "cut": None}
            if r["protected"] and not r["shrink"]:
                dead.update(action="keep", review=False, confidence=0.8,
                            reason=f'{r["G"] * 1000:.0f} ms pause kept: {r["protected"]} (protect rules)')
            elif cut is None:
                dead.update(action="keep", review=True, confidence=0.4, reason=f'{r["G"] * 1000:.0f} ms pause could not be shortened safely')
            else:
                what = f'{r["G"] * 1000:.0f} ms of silence shortened to {cut["kept_gap_s"] * 1000:.0f} ms (target {r["R"] * 1000:.0f}, floor {pol["pause"]["floor_ms"]})'
                if r["protected"]:
                    what += f'; protected pause ({r["protected"]}) capped at protect.cap_ms'
                dead.update(action="shorten", review=False, confidence=0.95, reason=what)
            decisions.append(dead)
            members.append(dead)
        if cut is not None and any(m["action"] in ("cut", "shorten") for m in members):
            cut["id"] = f"k{len(cuts) + 1:03d}"
            cut["decisions"] = [m["id"] for m in members if m["action"] in ("cut", "shorten")]
            cut["context"] = "intra_speech" if cut["kept_gap_s"] * 1000 < pol["xfade"]["intra_if_kept_gap_ms"] else "across_gap"
            cut["xfade_ms"] = pol["xfade"]["intra_speech_ms" if cut["context"] == "intra_speech" else "across_gap_ms"]
            cut["_region"] = r
            cuts.append(cut)
            for m in members:
                if m["action"] in ("cut", "shorten"):
                    m["cut"] = cut["id"]
        elif cut is None:
            for m in members:
                if m["action"] == "cut":
                    m.update(action="flag", review=True, confidence=0.3,
                             reason=m["reason"] + "; NOT cut: no safe splice point between the neighbouring words")

    # Non-lexical audio bursts the transcript doesn't explain.
    nn = 0
    for b in find_nonlexical(words, env, pol):
        nn += 1
        covering = next((k for k in cuts if k["e0"] - 0.01 <= b["e0"] and b["e1"] <= k["e1"] + 0.01), None)
        base = {"id": f"n{nn:03d}", "type": "nonlexical", "e0": round(b["e0"], 3), "e1": round(b["e1"], 3), "text": "",
                "mask": None, "cut": None}
        desc = f'{(b["e1"] - b["e0"]) * 1000:.0f} ms of sound ({b["db"]:.0f} dB) in a gap the transcript calls silent'
        if covering:
            decisions.append(dict(base, action="cut", review=False, confidence=0.8, cut=covering["id"],
                                  reason=desc + f'; removed with pause cut {covering["id"]}'))
        elif pol["nonlexical"]["auto_cut"]:
            L, U = b["L"], b["U"]
            s0 = _snap(env, b["e0"], L, b["e0"], pol)
            s1 = _snap(env, b["e1"], b["e1"], U, pol)
            k = {"e0": round(s0, 6), "e1": round(s1, 6), "snap": {"valley": True}, "kept_gap_s": round(U - L - (s1 - s0), 6),
                 "id": f"k{len(cuts) + 1:03d}", "decisions": [base["id"]], "context": "across_gap",
                 "xfade_ms": pol["xfade"]["across_gap_ms"], "_region": None}
            if not any(k["e0"] < o["e1"] and k["e1"] > o["e0"] for o in cuts):
                cuts.append(k)
                decisions.append(dict(base, action="cut", review=True, confidence=0.6, cut=k["id"], reason=desc + "; cut (nonlexical.auto_cut)"))
                continue
            decisions.append(dict(base, action="flag", review=True, confidence=0.4, reason=desc + "; overlaps another cut, left for review"))
        else:
            decisions.append(dict(base, action="flag", review=True, confidence=0.5,
                                  reason=desc + "; possible breath/cough/missed filler: flagged, not cut (nonlexical.auto_cut is off)"))
    cuts.sort(key=lambda k: k["e0"])
    for n, k in enumerate(cuts, 1):
        old = k["id"]
        k["id"] = f"k{n:03d}"
        for d in decisions:
            if d.get("cut") == old:
                d["cut"] = k["id"]
    decisions.sort(key=lambda d: (d["e0"], d["id"]))
    return cuts, decisions, warnings


# ---- EditDoc building ---------------------------------------------------------------------------------------------

def _intervals(segs: list[dict]) -> list[dict]:
    """Assembly segments as EPISODE-time intervals (e0, e1) keeping the segment itself."""
    return sorted(({**s, "ea": s["e0"], "eb": s["e0"] + (s["o1"] - s["o0"])} for s in segs), key=lambda s: s["ea"])


def keeps_from(dialogue: list[dict], cuts: list[dict]) -> list[tuple[float, float]]:
    out = []
    for s in _intervals(dialogue):
        pos = s["ea"]
        for c in sorted(cuts, key=lambda c: c["e0"]):
            if c["e1"] <= pos or c["e0"] >= s["eb"]:
                continue
            if c["e0"] > pos + EPS:
                out.append((pos, c["e0"]))
            pos = max(pos, c["e1"])
        if s["eb"] - pos > EPS:
            out.append((pos, s["eb"]))
    return out


def _same_framing(a: dict, b: dict, delta: float) -> bool:
    return a["src"] == b["src"] and a.get("crop") == b.get("crop") and abs(a["zoom"] - b["zoom"]) < delta


def _overlay(plan: list[dict], a: float, b: float, **attrs) -> list[dict]:
    out = []
    for e in plan:
        if e["o1"] <= a + 1e-9 or e["o0"] >= b - 1e-9:
            out.append(e)
            continue
        if e["o0"] < a - 1e-9:
            out.append({**e, "o1": a})
        out.append({**e, "o0": max(e["o0"], a), "o1": min(e["o1"], b), **attrs})
        if e["o1"] > b + 1e-9:
            out.append({**e, "o0": b})
    return out


def _coverage(ep: Episode, doc: dict, warnings: list[str]) -> dict[str, tuple[float, float]]:
    """Episode-time span each video source actually has footage for (start_offset .. start_offset + duration)."""
    media = {}
    p = ep.artifact("media.json")
    if p.exists():
        media = {m["file"]: m for m in read_json(p)["files"]}
    cov = {}
    for k, s in doc["sources"].items():
        if s.get("kind") != "video":
            continue
        d = (media.get(s["file"]) or {}).get("duration")
        if d is None:
            warnings.append(f"no duration for {s['file']} in media.json: assuming it covers the whole episode for masks")
        cov[k] = (editdoc.episode_time(s, 0.0), editdoc.episode_time(s, float(d)) if d is not None else float("inf"))
    return cov


class _Masker:
    """Applies the stated mask priority to each cut, in order, editing `plan` (video entries in OUTPUT time)."""

    def __init__(self, plan, tm, cuts, doc, cov, pol):
        self.plan, self.tm, self.cuts, self.doc, self.cov = plan, tm, cuts, doc, cov
        self.m = pol["masks"]
        self.last_cutaway: dict[str, float] = {}
        self.punches = 0

    def same(self, a, b):
        return _same_framing(a, b, self.m["min_zoom_delta"])

    def run(self):
        for k in self.cuts:
            t = self.tm.to_output_snap(k["e0"])
            k["t_out"] = round(t, 6)
            if t <= EPS or t >= self.tm.duration - EPS:
                k["mask"] = {"type": "none", "reason": "cut at the very start/end of the program: nothing to mask"}
                k["review"] = False
                continue
            k["mask"] = self._mask_cut(k, t)
            k["review"] = k["mask"]["type"] == "none"

    def _idx_left(self, t):
        return max(j for j, e in enumerate(self.plan) if e["o0"] < t - 1e-6)

    def _mask_cut(self, k, t) -> dict:
        m, plan = self.m, self.plan
        li = self._idx_left(t)
        left, right = plan[li], plan[li + 1]
        if not self.same(left, right):
            what = f'{left["src"]}->{right["src"]}' if left["src"] != right["src"] else f'framing {left["zoom"]:.2f}->{right["zoom"]:.2f}'
            return {"type": "camera_change", "detail": what, "reason": f"the picture already changes at this cut ({what}): nothing extra needed"}
        s = li
        while s > 0 and self.same(plan[s - 1], plan[s]):
            s -= 1
        hold_before = t - plan[s]["o0"]
        e = li + 1
        while e + 1 < len(plan) and self.same(plan[e], plan[e + 1]):
            e += 1
        nxt = min((c["t_out"] if "t_out" in c else self.tm.to_output_snap(c["e0"]) for c in self.cuts
                   if self.tm.to_output_snap(c["e0"]) > t + 1e-6), default=self.tm.duration)
        end_limit = min(plan[e]["o1"], nxt)
        x = end_limit - t
        why_not = []
        if hold_before >= m["min_hold_s"] - 1e-9:
            e_start = k["e1"]
            for kind, enabled, cams in (("cutaway", m["cutaway_other_camera"], self._others(left["src"], wide=False)),
                                        ("wide_cutaway", m["cutaway_wide"], self._others(left["src"], wide=True))):
                if not enabled:
                    why_not.append(f"{kind} disabled by policy")
                    continue
                for cam in cams:
                    opt = self._cutaway_hold(cam, t, x, end_limit, e_start, nxt)
                    if isinstance(opt, str):
                        why_not.append(f"{cam}: {opt}")
                        continue
                    self.plan[:] = _overlay(plan, t, t + opt, src=cam, zoom=1.0, crop=None, speaker=None, confidence=0.9,
                                            reason=f"{kind.replace('_', ' ')} to {cam} masks cut {k['id']}")
                    self.last_cutaway[cam] = t
                    return {"type": kind, "camera": cam, "o0": round(t, 6), "o1": round(t + opt, 6),
                            "reason": f"{cam} covers this moment; cut away for {opt:.2f}s then return (hold before {hold_before:.2f}s >= min_hold_s {m['min_hold_s']})"}
        else:
            why_not.append(f"shot only on screen {hold_before:.2f}s < min_hold_s {m['min_hold_s']}: no cutaway")
        if m["punch_in"]:
            base = right.get("base_zoom", 1.0)
            going_in = left["zoom"] < base + m["min_zoom_delta"] / 2
            z = round(base * m["zooms"][self.punches % len(m["zooms"])], 3) if going_in else base
            ok, why = zoomstate.allows(plan, t, right["src"], "in" if going_in else "out", m.get("zoom_min_dwell_s", 0.0),
                                       m["min_zoom_delta"], m.get("max_ins_per_shot", 1))
            if ok and m.get("zoom_min_dwell_s", 0.0) > 0:
                ok, why = zoomstate.upcoming_change_ok(plan, t, plan[e]["o1"], right["src"], z, m["zoom_min_dwell_s"], m["min_zoom_delta"])
            if not ok:
                why_not.append(f"{'punch-in' if going_in else 'punch-out'} blocked by the zoom limit: {why}")
            else:
                if going_in:
                    self.punches += 1
                self.plan[:] = _overlay(plan, t, plan[e]["o1"], zoom=z, confidence=0.75,
                                        reason=f"{right['reason']}; reframed {z:.2f}x masks cut {k['id']}")
                return {"type": "punch_in", "zoom": z, "o0": round(t, 6), "o1": round(plan[e]["o1"], 6),
                        "reason": f"no usable cutaway ({'; '.join(why_not)}): reframe to {z:.2f}x on the segment after the cut"}
        return {"type": "none", "reason": f"no mask available ({'; '.join(why_not)}): audio crossfade only, needs review"}

    def _others(self, cur: str, wide: bool) -> list[str]:
        out = []
        for key, s in sorted(self.doc["sources"].items()):
            if s.get("kind") == "video" and key != cur and (s.get("role") == "wide") == wide:
                out.append(key)
        return out

    def _cutaway_hold(self, cam, t, x, end_limit, e_start, nxt) -> float | str:
        """Length of a cutaway to `cam` at output t, or a string saying why not."""
        m = self.m
        lo, hi = self.cov.get(cam, (0.0, float("inf")))
        if t - self.last_cutaway.get(cam, -1e9) < m["repeat_window_s"]:
            return f"used as a cutaway {t - self.last_cutaway[cam]:.1f}s ago (repeat_window_s {m['repeat_window_s']})"
        options = []
        ret = min(m["cutaway_s"], x - m["min_hold_s"])
        if ret >= m["min_hold_s"] - 1e-9:
            options.append(ret)
        if m["min_hold_s"] <= x <= m["cutaway_max_s"] + 1e-9:
            after = [e for e in self.plan if e["o0"] >= end_limit - 1e-6]
            joins_same = bool(after) and after[0]["src"] == cam and end_limit >= nxt - 1e-6 and nxt < self.tm.duration - EPS
            if not joins_same:
                options.append(x)
        if not options:
            return f"only {x:.2f}s of room before the next change (needs {2 * m['min_hold_s']:.1f}s)"
        for h in options:
            if lo <= e_start and e_start + h <= hi + 1e-6:
                return h
        return f"footage covers {lo:.1f}-{hi:.1f}s but the cutaway needs {e_start:.1f}-{e_start + options[0]:.1f}s"


def _video_plan(av: list[dict], tm: TimeMap) -> list[dict]:
    plan = []
    for ki, (a, b) in enumerate(tm.keeps):
        for s in _intervals(av):
            lo, hi = max(a, s["ea"]), min(b, s["eb"])
            if hi - lo > EPS:
                base = {k: v for k, v in s.items() if k not in ("ea", "eb", "e0", "o0", "o1", "id")}
                plan.append({**base, "base_zoom": s.get("zoom", 1.0), "zoom": s.get("zoom", 1.0), "crop": s.get("crop"),
                             "o0": tm.o_starts[ki] + lo - a, "o1": tm.o_starts[ki] + hi - a})
    return plan


def _episode_at_start(tm: TimeMap, o: float) -> float:
    """Episode time for an output time that STARTS a segment. Float dust must not pull it into the previous keep."""
    i = max(bisect.bisect_right(tm.o_starts, o + 1e-9) - 1, 0)
    return tm.keeps[i][0] + max(o - tm.o_starts[i], 0.0)


def _emit_video(plan: list[dict], tm: TimeMap) -> list[dict]:
    pieces = []
    for e in sorted(plan, key=lambda e: e["o0"]):
        cuts_at = [o for o in tm.o_starts[1:] if e["o0"] + 1e-9 < o < e["o1"] - 1e-9]   # never straddle a splice
        edges = [e["o0"], *cuts_at, e["o1"]]
        for a, b in zip(edges, edges[1:]):
            if b - a > 1e-9:
                pieces.append({**e, "o0": a, "o1": b, "e0": _episode_at_start(tm, a)})
    merged: list[dict] = []
    for p in pieces:
        q = merged[-1] if merged else None
        if (q and q["src"] == p["src"] and q.get("crop") == p.get("crop") and abs(q["zoom"] - p["zoom"]) < 1e-9
                and abs(q["e0"] + (q["o1"] - q["o0"]) - p["e0"]) < 1e-6 and abs(q["o1"] - p["o0"]) < 1e-6):
            q["o1"] = p["o1"]
        else:
            merged.append(p)
    out = []
    for n, p in enumerate(merged, 1):
        seg = {"id": f"v{n}", "src": p["src"], "o0": round(p["o0"], 6), "o1": round(p["o1"], 6), "e0": round(p["e0"], 6),
               "crop": p.get("crop"), "zoom": p["zoom"], "reason": p["reason"], "confidence": p["confidence"]}
        if p.get("speaker"):
            seg["speaker"] = p["speaker"]
        out.append(seg)
    for a, b in zip(out, out[1:]):
        b["o0"] = a["o1"]
    out[0]["o0"] = 0.0
    out[-1]["o1"] = round(tm.duration, 6)
    return out


def _emit_dialogue(dialogue: list[dict], tm: TimeMap, cuts: list[dict]) -> list[dict]:
    out = []
    cut_ending = {round(c["e1"], 5): c for c in cuts}
    for s in _intervals(dialogue):
        for ki, (a, b) in enumerate(tm.keeps):
            lo, hi = max(a, s["ea"]), min(b, s["eb"])
            if hi - lo <= EPS:
                continue
            c = cut_ending.get(round(lo, 5))
            xf = c["xfade_ms"] if c else (s.get("xfade_ms", 0) if lo <= s["ea"] + EPS else 0)
            out.append({"id": "", "src": s["src"], "o0": tm.o_starts[ki] + lo - a, "o1": tm.o_starts[ki] + hi - a, "e0": round(lo, 6),
                        "xfade_ms": xf, "gain_db": s.get("gain_db", 0.0), "confidence": 1.0, "_cut": c["id"] if c else None,
                        "reason": (f"kept speech after cut {c['id']} ({'; '.join(c['decisions'])})" if c else s.get("reason", "kept"))})
    for n, seg in enumerate(out, 1):
        seg["id"] = f"d{n}"
        seg["o0"], seg["o1"] = round(seg["o0"], 6), round(seg["o1"], 6)
    for i in range(len(out)):
        if i == 0:
            out[i]["xfade_ms"] = 0
        else:   # a crossfade cannot be longer than half of either neighbour
            lim = int(min(out[i]["o1"] - out[i]["o0"], out[i - 1]["o1"] - out[i - 1]["o0"]) * 500)
            out[i]["xfade_ms"] = min(out[i]["xfade_ms"], lim)
        if i:
            out[i]["o0"] = out[i - 1]["o1"]
    out[0]["o0"] = 0.0
    out[-1]["o1"] = round(tm.duration, 6)
    return out


def build_rough(assembly: dict, cuts: list[dict], cov: dict, pol: dict, note: str) -> tuple[dict, TimeMap]:
    """Assembly EditDoc + finished cuts -> rough EditDoc (stage 'rough'), with masks applied to the video track."""
    tr = assembly["tracks"]
    tm = TimeMap(keeps_from(tr["dialogue"], cuts))
    doc = editdoc.clone(assembly)
    doc["stage"] = "rough"
    doc["duration"] = round(tm.duration, 6)
    plan = _video_plan(tr["video"], tm)
    masker = _Masker(plan, tm, cuts, assembly, cov, pol)
    masker.run()
    doc["tracks"]["video"] = _emit_video(masker.plan, tm)
    doc["tracks"]["dialogue"] = _emit_dialogue(tr["dialogue"], tm, cuts)
    for name in ("music", "overlays"):        # same episode clock as the assembly: re-time through the cuts
        kept = []
        for s in tr.get(name) or []:
            o0, o1 = tm.to_output_snap(s["o0"]), tm.to_output_snap(s["o1"])
            if o1 - o0 > EPS:
                kept.append({**s, "o0": o0, "o1": o1})
        doc["tracks"][name] = kept
    doc["captions"] = {"srt": None, "ass": None, "burn": False}
    doc["notes"] = note
    return doc, tm


# ---- pacing: reaction shots that break long holds (field note N7, ROADMAP W1) ----------------------------------------------

def _hold_runs(video: list[dict], delta: float) -> list[tuple[int, int]]:
    """(first, last) segment index of each hold: consecutive segments with the same camera and framing (a splice is not a cut)."""
    runs, s = [], 0
    for i in range(1, len(video) + 1):
        if i == len(video) or not _same_framing(video[i - 1], video[i], delta):
            runs.append((s, i - 1))
            s = i
    return runs


def _pace_stats(video: list[dict], pol: dict) -> dict:
    """Holds over the limit, average hold length and cuts per minute of a video track."""
    if not video:
        return {"holds": 0, "holds_over_limit": 0, "holds_over_limit_plus_reaction": 0, "avg_shot_s": 0.0, "cuts_per_min": 0.0, "longest_hold_s": 0.0}
    runs = _hold_runs(video, pol["masks"]["min_zoom_delta"])
    lens = [video[b]["o1"] - video[a]["o0"] for a, b in runs]
    dur = video[-1]["o1"] - video[0]["o0"]
    hard = pol["pacing"]["max_hold_s"] + pol["pacing"]["reaction"]["max_s"] + 1e-6
    return {"holds": len(runs), "holds_over_limit": sum(1 for x in lens if x > pol["pacing"]["max_hold_s"] + 1e-6),
            "holds_over_limit_plus_reaction": sum(1 for x in lens if x > hard),
            "avg_shot_s": round(sum(lens) / len(lens), 2), "cuts_per_min": round((len(runs) - 1) / (dur / 60.0), 2) if dur > 0 else 0.0,
            "longest_hold_s": round(max(lens), 2)}


def _output_words(words: list[dict], tm: TimeMap) -> list[dict]:
    """Words that survive the cuts, in OUTPUT time ({o0, o1, w, speaker}); a word straddling a splice is dropped."""
    out = []
    for w in words:
        a, b = tm.to_output(w["start"]), tm.to_output(w["end"])
        if a is None or b is None or abs((b - a) - (w["end"] - w["start"])) > 2e-3:
            continue
        out.append({"o0": a, "o1": b, "w": w["w"], "speaker": w.get("speaker")})
    out.sort(key=lambda x: (x["o0"], x["o1"]))
    return out


def _pacing_boundaries(ow: list[dict], pb: dict) -> list[dict]:
    """Every place a video cut may land: a gap between words where no word of ANYONE is running (so never inside a word).
    kind: `sentence` (a strong sentence just ended), `pause` (a breath), or `word` (an ordinary word boundary). t = middle of the gap."""
    out, run_end, last, count = [], None, None, 0
    for w in ow:
        if last is not None:
            ends = last["w"].rstrip().endswith(tuple(SENT_END))
            if w["o0"] >= run_end - 1e-9:
                gap = w["o0"] - run_end
                kind = ("sentence" if ends and count >= pb["sentence_min_words"] else "pause" if gap * 1000.0 >= pb["pause_ms"] else "word")
                out.append({"t": (run_end + w["o0"]) / 2.0, "kind": kind, "gap": gap})
            if ends:
                count = 0
        count += 1
        if run_end is None or w["o1"] > run_end:
            run_end, last = w["o1"], w
    return out


def _splice_in(video: list[dict], a: float, b: float, **attrs) -> list[dict]:
    """Replace [a, b] of the video track by the same time with `attrs` changed; every piece keeps its own episode clock (e0)."""
    out = []
    for s in video:
        if s["o1"] <= a + 1e-9 or s["o0"] >= b - 1e-9:
            out.append(s)
            continue
        lo, hi = max(s["o0"], a), min(s["o1"], b)
        if s["o0"] < a - 1e-9:
            out.append({**s, "o1": a})
        out.append({**s, "o0": lo, "o1": hi, "e0": s["e0"] + (lo - s["o0"]), **attrs})
        if s["o1"] > b + 1e-9:
            out.append({**s, "o0": b, "e0": s["e0"] + (b - s["o0"])})
    return out


def _episode_pieces(video: list[dict], a: float, b: float) -> list[tuple[float, float]]:
    """Episode-time (start, end) of each piece of [a, b] (several when a splice sits inside it)."""
    out = []
    for s in video:
        lo, hi = max(s["o0"], a), min(s["o1"], b)
        if hi - lo > 1e-9:
            e = s["e0"] + (lo - s["o0"])
            out.append((e, e + (hi - lo)))
    return out


def apply_pacing(video: list[dict], words: list[dict], tm: TimeMap, sources: dict, cov: dict, pol: dict,
                 cam_speaker: dict[str, str] | None = None) -> tuple[list[dict], dict]:
    """Break every hold longer than `pacing.max_hold_s` with a reaction shot. Returns (new video track, report). The dialogue is never touched.

    A reaction starts and ends BETWEEN words (a sentence end that finishes a strong sentence is best, then a breath pause), at least
    `min_gap_after_cut_s` after the previous cut and before the next, and is shown on the first camera in `camera_order` that works:
    a `role: wide` camera, the listener's camera (never while that person speaks), or a punch-in on the same camera. The local cut
    rate stays under `max_cuts_per_min`. Deterministic, and idempotent: a track with no over-long holds comes back unchanged.
    `cam_speaker` maps a camera to the speaker it frames (to know who the listener is)."""
    pc = pol["pacing"]
    before = _pace_stats(video, pol)
    rep = {"enabled": bool(pc["enabled"]), "max_hold_s": pc["max_hold_s"], "inserted": 0, "still_over_limit": [], "by_mask": {}, "rate_overrides": 0,
           "decisions": [], "before": before, "after": before}
    if not pc["enabled"] or not video:
        return video, rep
    delta, rx, pb = pol["masks"]["min_zoom_delta"], pc["reaction"], pc["boundary"]
    cam_speaker = cam_speaker or {}
    ow = _output_words(words, tm)
    ow_starts = [w["o0"] for w in ow]
    bounds = _pacing_boundaries(ow, pb)
    b_t = [b["t"] for b in bounds]
    cut_ts = sorted(video[b]["o0"] for a, b in _hold_runs(video, delta)[1:])
    video = [dict(s) for s in video]
    zoom_turn = 0

    def spoken(speaker, a, b) -> bool:
        lo = bisect.bisect_left(ow_starts, a - 10.0)
        return any(w["speaker"] == speaker and w["o0"] < b and w["o1"] > a for w in ow[lo:bisect.bisect_left(ow_starts, b)])

    def rate_ok(a, b) -> bool:
        win = pc["rate_window_s"]
        mid = (a + b) / 2.0
        n = bisect.bisect_right(cut_ts, mid + win / 2.0) - bisect.bisect_left(cut_ts, mid - win / 2.0)
        return n + 2 <= pc["max_cuts_per_min"] * win / 60.0 + 1e-9

    def covered(cam, a, b) -> bool:
        lo, hi = cov.get(cam, (0.0, float("inf")))
        return all(lo - 1e-6 <= e0 and e1 <= hi + 1e-6 for e0, e1 in _episode_pieces(video, a, b))

    dwell = float(pc.get("zoom_min_dwell_s", 0.0))
    max_ins = int(pc.get("max_ins_per_shot", 1))

    def punch_end(a, h1) -> float | None:
        """Where a SUSTAINED reframe starting at `a` ends: the hold's end, or (hold longer than max_hold_s) the best boundary at least
        one dwell later and at most max_hold_s later, so the return is a real dwell after the way in. None: no such boundary."""
        if h1 - a <= pc["max_hold_s"] + 1e-6:
            return h1
        lo_e, hi_e = a + dwell, min(a + pc["max_hold_s"], h1 - rx["min_gap_after_cut_s"])
        cand = [bounds[k] for k in range(bisect.bisect_left(b_t, lo_e - 1e-9), bisect.bisect_right(b_t, hi_e + 1e-9))]
        if not cand:
            return None
        cand.sort(key=lambda c: (-(pb["prefer"][c["kind"]] - pb["distance_penalty_per_s"] * abs(c["t"] - (a + pc["target_hold_s"]))), c["t"]))
        return cand[0]["t"]

    def pick_camera(hold_src, hold_seg, a, b, h1):
        """(mask kind, attrs, end) for the first camera in camera_order that works for [a, b], else (None, reason, None).
        `end` is `b` except for a punch-in, which is sustained (see punch_end) when pacing.zoom_min_dwell_s > 0."""
        nonlocal zoom_turn
        why = []
        others = [k for k, s in sorted(sources.items()) if s.get("kind") == "video" and k != hold_src]
        for kind in pc["camera_order"]:
            if kind == "wide":
                cams = [k for k in others if sources[k].get("role") == "wide"]
                if not cams:
                    why.append("no wide camera")
                for k in cams:
                    if covered(k, a, b):
                        return "wide", dict(src=k, zoom=1.0, crop=None, speaker=None), b
                    why.append(f"{k} has no footage there")
            elif kind == "listener":
                cams = [k for k in others if sources[k].get("role") != "wide"]
                if not cams:
                    why.append("no other camera")
                for k in cams:
                    spk = cam_speaker.get(k)
                    if not pc["listener_may_speak"] and (spk is None or spoken(spk, a, b)):
                        why.append(f"{k}'s speaker is talking" if spk else f"{k} has no known speaker")
                    elif not covered(k, a, b):
                        why.append(f"{k} has no footage there")
                    else:
                        return "listener", dict(src=k, zoom=1.0, crop=None, speaker=spk), b
            else:
                base = hold_seg.get("zoom", 1.0)
                zs = pc["punch_zooms"]
                z = base * zs[zoom_turn % len(zs)] if base < 1.0 + delta else 1.0
                z = round(min(z, 2.0), 3)
                if abs(z - base) < delta - 1e-9:
                    why.append("no distinguishable framing")
                    continue
                if dwell <= 0:                      # legacy: a short blip that returns to the base framing
                    zoom_turn += 1
                    return "punch_in", dict(zoom=z), b
                ok, bad = zoomstate.allows(video, a, hold_src, "in" if z > base else "out", dwell, delta, max_ins)
                end = punch_end(a, h1) if ok else None
                if ok and end is None:
                    ok, bad = False, f"no word boundary {dwell:g}-{pc['max_hold_s']:g}s after {a:.1f}s for the reframe to come back out"
                if ok:
                    ok, bad = zoomstate.upcoming_change_ok(video, a, end, hold_src, z, dwell, delta)
                if not ok:
                    why.append(f"punch-in blocked by the zoom limit: {bad}")
                    continue
                zoom_turn += 1
                return "punch_in", dict(zoom=z), end
        return None, "; ".join(why), None

    i = 0
    n_react = 0
    while i < len(_hold_runs(video, delta)):
        runs = _hold_runs(video, delta)
        a_i, b_i = runs[i]
        h0, h1 = video[a_i]["o0"], video[b_i]["o1"]
        i += 1
        cursor, failed = h0, None
        while h1 - cursor > pc["max_hold_s"] + 1e-6 and failed is None:
            lo = cursor + rx["min_gap_after_cut_s"]
            hi = min(cursor + pc["max_hold_s"], h1 - rx["min_gap_after_cut_s"] - rx["min_s"])
            starts = [bounds[k] for k in range(bisect.bisect_left(b_t, lo - 1e-9), bisect.bisect_right(b_t, hi + 1e-9))]
            starts.sort(key=lambda c: (-(pb["prefer"][c["kind"]] - pb["distance_penalty_per_s"] * abs(c["t"] - (cursor + pc["target_hold_s"]))), c["t"]))
            why = (f"no word boundary between {lo:.1f}s and {hi:.1f}s (one long ASR word or unbroken overlapping speech?)" if not starts
                   else f"no candidate boundary has a second boundary {rx['min_s']:g}-{rx['max_s']:g}s later with room before the next cut")
            # The cut-rate cap is honoured first. A hold already beyond max_hold_s + reaction.max_s is worse than a busy minute, so then
            # (unless hard_rate_cap) the cap yields and the decision says so.
            relax = (not pc["hard_rate_cap"]) and h1 - cursor > pc["max_hold_s"] + rx["max_s"] + 1e-6
            done = False
            for enforce in ((True, False) if relax else (True,)):
                for st in starts:
                    end_hi = min(st["t"] + rx["max_s"], h1 - rx["min_gap_after_cut_s"])
                    ends = [bounds[k] for k in range(bisect.bisect_left(b_t, st["t"] + rx["min_s"] - 1e-9), bisect.bisect_right(b_t, end_hi + 1e-9))]
                    ends.sort(key=lambda c: (-(pb["prefer"][c["kind"]] - pb["distance_penalty_per_s"] * abs(c["t"] - st["t"] - rx["prefer_s"])), c["t"]))
                    for en in ends:
                        a, b = st["t"], en["t"]
                        busy = not rate_ok(a, b)
                        if busy and enforce:
                            why = f"would exceed pacing.max_cuts_per_min ({pc['max_cuts_per_min']:g}) around {a:.1f}s"
                            continue
                        hold_seg = next(s for s in video if s["o0"] <= a + 1e-9 < s["o1"])
                        kind, got, b_eff = pick_camera(hold_seg["src"], hold_seg, a, b, h1)
                        if kind is None:
                            why = got
                            continue
                        b = b_eff
                        conf = pc["confidence"][st["kind"]] * (pc["confidence"]["punch_in_factor"] if kind == "punch_in" else 1.0)
                        n_react += 1
                        cam = got.get("src", hold_seg["src"])
                        video = _splice_in(video, a, b, reason="pacing: reaction", confidence=round(min(conf, 1.0), 3), **got)
                        bisect.insort(cut_ts, a)
                        if b < h1 - 1e-9:          # a reframe held to the end of the hold ends on a camera change that was already a cut
                            bisect.insort(cut_ts, b)
                        rep["by_mask"][kind] = rep["by_mask"].get(kind, 0) + 1
                        if busy:
                            rep["rate_overrides"] += 1
                        rep["decisions"].append({
                            "id": f"r{n_react:03d}", "type": "pacing_reaction", "o0": round(a, 3), "o1": round(b, 3), "e0": round(tm.to_episode(a + 1e-9), 3),
                            "mask": kind, "camera": cam, "zoom": got.get("zoom", 1.0), "boundary": st["kind"], "confidence": round(min(conf, 1.0), 3),
                            "hold": [round(h0, 3), round(h1, 3)], "rate_override": busy, "sustained": bool(kind == "punch_in" and dwell > 0),
                            "reason": (f"hold on {hold_seg['src']} would reach {h1 - cursor:.1f}s (> pacing.max_hold_s {pc['max_hold_s']:g}); "
                                       + (f"sustained reframe to {got['zoom']:.2f}x on {cam}, held {b - a:.1f}s "
                                          f"{'to the end of the hold' if b >= h1 - 1e-9 else 'then back out'} (pacing.zoom_min_dwell_s {dwell:g}) at a {st['kind']} boundary"
                                          if kind == "punch_in" and dwell > 0 else
                                          f"{f'reframe to {got['zoom']:.2f}x' if kind == 'punch_in' else kind} on {cam} for {b - a:.1f}s at a {st['kind']} boundary")
                                       + (f"; cut rate above pacing.max_cuts_per_min here, but the hold was past max_hold_s + reaction.max_s" if busy else ""))})
                        cursor, done = b, True
                        break
                    if done:
                        break
                if done:
                    break
            if not done:
                failed = why
        if failed is not None:
            rep["still_over_limit"].append({"o0": round(cursor, 3), "o1": round(h1, 3), "length_s": round(h1 - cursor, 2), "why": failed,
                                            "within_tolerance": h1 - cursor <= pc["max_hold_s"] + rx["max_s"] + 1e-6})
        runs = _hold_runs(video, delta)       # reactions split this hold: continue after its (new) last piece
        i = next((k for k, (ra, _) in enumerate(runs) if video[ra]["o0"] >= h1 - 1e-9), len(runs))
    for n, s in enumerate(video, 1):
        s["id"] = f"v{n}"
    rep["inserted"] = n_react
    rep["after"] = _pace_stats(video, pol)
    return video, rep


def _long_holds(video: list[dict], pol: dict, pacing_on: bool = False) -> list[str]:
    """Warnings for holds still over the limit. With pacing on the limit is `pacing.max_hold_s` (the pass tried to fix them)."""
    delta = pol["masks"]["min_zoom_delta"]
    mx = pol["pacing"]["max_hold_s"] if pacing_on else pol["masks"]["max_hold_s"]
    out, start = [], 0
    for i in range(1, len(video) + 1):
        if i == len(video) or not _same_framing(video[i - 1], video[i], delta):
            length = video[i - 1]["o1"] - video[start]["o0"]
            if length > mx:
                out.append(f"long hold: {video[start]['src']} on screen {length:.1f}s from {video[start]['o0']:.1f}s (> "
                           f"{'pacing' if pacing_on else 'masks'}.max_hold_s {mx:g})")
            start = i
    return out


# ---- orchestration -------------------------------------------------------------------------------------------------

def _env_for(ep: Episode, assembly: dict, pol: dict) -> Envelope:
    seg = assembly["tracks"]["dialogue"][0]
    src = assembly["sources"][seg["src"]]
    wav = audio.analysis_wav_path(ep, src["file"])
    if not wav.exists():
        raise SystemExit(f"Missing analysis audio {wav.relative_to(ep.root)}. Run: ave audio {ep.root}")
    return Envelope.from_wav(wav, src["start_offset"], pol["snap"]["hop_ms"], pol["snap"]["frame_ms"], editdoc.drift_of(src), src.get("sync_map"))


def _camera_speakers(assembly: dict) -> dict[str, str]:
    """camera -> the speaker it frames, from the assembly's own shots (each carries its `speaker`; the most common one wins)."""
    seen: dict[str, dict[str, int]] = {}
    for s in assembly["tracks"]["video"]:
        if s.get("speaker"):
            seen.setdefault(s["src"], {})
            seen[s["src"]][s["speaker"]] = seen[s["src"]].get(s["speaker"], 0) + 1
    return {cam: max(c, key=lambda k: (c[k], k)) for cam, c in seen.items()}


def pick_room_tone(x: np.ndarray, sr: int, lo_s: float, hi_s: float, rt: dict) -> dict | None:
    """The quietest steady window of `rt["min_s"]`..`rt["max_s"]` seconds of `x` inside [lo_s, hi_s] (file seconds), or None.

    `x` is the analysis audio, which already follows the channel layout rules (speaker_per_channel masters are the L+R SUM,
    a dead-channel file is its live channel), so a window quiet HERE is quiet in what the compiler will fold. Digital silence
    (a recorder pad) is skipped and so is any window with a transient (steady_db), so a cough never becomes the room."""
    hop = max(1, int(sr * rt["frame_ms"] / 1000))
    a, b = max(0, int((lo_s + rt["margin_s"]) * sr)), min(len(x), int((hi_s - rt["margin_s"]) * sr))
    n = (b - a) // hop
    nwin = int(round(rt["min_s"] * 1000 / rt["frame_ms"]))
    if n < nwin:
        return None
    p = (x[a:a + n * hop].astype(np.float64).reshape(n, hop) ** 2).mean(axis=1)          # power per frame
    cs = np.concatenate([[0.0], np.cumsum(p)])
    mean = (cs[nwin:] - cs[:-nwin]) / nwin                                                 # mean power of every window
    win = np.lib.stride_tricks.sliding_window_view(p, nwin)
    peak, low = win.max(axis=1), win.min(axis=1)
    db = 10 * np.log10(mean + 1e-12)
    # steady = no frame far above the mean (a cough, a door) and none far below it (speech with pauses is not a room)
    ok = ((db > rt["min_floor_db"]) & (10 * np.log10(peak + 1e-12) - db <= rt["steady_db"])
          & (db - 10 * np.log10(low + 1e-12) <= rt["steady_db"]))
    cand = np.nonzero(ok)[0]
    if not len(cand):
        return None
    i = int(cand[np.argmin(db[cand])])
    extra = int(round((rt["max_s"] - rt["min_s"]) * 1000 / rt["frame_ms"]))
    j = nwin                                       # grow while the stretch stays at the same level (within 1 dB of the floor)
    while j - nwin < extra and i + j < n and 10 * np.log10(p[i + j] + 1e-12) <= db[i] + 1.0:
        j += 1
    f0 = (a + i * hop) / sr
    return {"f0": f0, "f1": f0 + j * hop / sr, "rms_db": round(float(10 * np.log10(p[i:i + j].mean() + 1e-12)), 2)}


def auto_room_tone(ep: Episode, doc: dict, pol: dict) -> tuple[dict | None, str | None]:
    """(tracks.room_tone value, warning) for a rough doc: the quietest stretch of the dialogue source inside the take."""
    rt = pol.get("room_tone") or {}
    if not rt.get("enabled") or not doc["tracks"]["dialogue"]:
        return None, None
    seg = doc["tracks"]["dialogue"][0]
    src = doc["sources"][seg["src"]]
    wav = audio.analysis_wav_path(ep, src["file"])
    if not wav.exists():
        return None, None
    import soundfile as sf
    x, sr = sf.read(str(wav), dtype="float32", always_2d=True)
    x = x[:, 0]
    dur = len(x) / sr
    # stay near the span this edit uses (its dialogue extent), so a long recorder file does not borrow another session's silence
    es = [(s["e0"], s["e0"] + (s["o1"] - s["o0"])) for s in doc["tracks"]["dialogue"] if s["src"] == seg["src"] and not s.get("fill")]
    lo = max(0.0, editdoc.src_time(src, min(a for a, _ in es)) - 30.0) if es else 0.0
    hi = min(dur, editdoc.src_time(src, max(b for _, b in es)) + 30.0) if es else dur
    hit = pick_room_tone(x, sr, lo, hi, rt)
    if hit is None and (lo > 0 or hi < dur):
        hit = pick_room_tone(x, sr, 0.0, dur, rt)
    if hit is None:
        return None, (f"no room tone: found no steady quiet stretch of {rt['min_s']:g} s in {src['file']}; selects built from this cut will "
                      "butt-join (set tracks.room_tone by hand to get gaps)")
    e0, e1 = editdoc.episode_time(src, hit["f0"]), editdoc.episode_time(src, hit["f1"])
    return {"src": seg["src"], "e0": round(e0, 3), "e1": round(e1, 3), "rms_db": hit["rms_db"], "auto": True}, None


def analyze(ep: Episode, assembly: dict, pol: dict, pacing: bool = True) -> dict:
    """Everything except writing: detection -> cuts -> masked rough doc -> pacing. Pure with respect to the episode folder."""
    transcript = read_json(ep.artifact("transcript.json"))
    words = prepare_words(transcript)
    if assembly.get("excluded"):        # a per-session assembly: other sessions' and uncovered speech is not this edit's to judge
        words = restrict_words(words, [(s["ea"], s["eb"]) for s in _intervals(assembly["tracks"]["dialogue"])])
    warnings: list[str] = []
    cands = apply_overrides(detect_candidates(words, pol), words, load_overrides(ep), warnings)
    env = _env_for(ep, assembly, pol)
    cuts, decisions, w2 = build_cuts(words, cands, env, pol)
    warnings += w2
    cov = _coverage(ep, assembly, warnings)
    doc, tm = build_rough(assembly, cuts, cov, pol, "cleanup of the assembly cut; every decision is in work/cleanup.json")
    # Mask + confidence feed back into the cut and its decisions so one record tells the whole story.
    low = pol["review"]["below_confidence"]
    for k in cuts:
        dec = [d for d in decisions if d["id"] in k["decisions"]]
        conf = min([d["confidence"] for d in dec] + [0.4 if k["mask"]["type"] == "none" else 1.0])
        if k["snap"].get("valley") is False:       # no quiet spot inside the allowed window: the splice may be audible
            conf -= pol["snap"]["unsnapped_confidence_penalty"]
        k["confidence"] = round(conf, 3)
        k["review"] = k["review"] or k["confidence"] < low
        for d in dec:
            d["mask"] = k["mask"]
            d["review"] = d["review"] or k["review"]
            d["confidence"] = min(d["confidence"], k["confidence"])
    conf_of = {k["id"]: k["confidence"] for k in cuts}
    for seg in doc["tracks"]["dialogue"]:
        seg["confidence"] = conf_of.get(seg.pop("_cut", None), 1.0)
    if not doc["tracks"].get("room_tone"):
        rtone, rwarn = auto_room_tone(ep, doc, pol)
        doc["tracks"]["room_tone"] = rtone
        if rwarn:
            warnings.append(rwarn)
    pace_pol = pol if pacing else {**pol, "pacing": {**pol["pacing"], "enabled": False}}
    doc["tracks"]["video"], pace = apply_pacing(doc["tracks"]["video"], words, tm, doc["sources"], cov, pace_pol, _camera_speakers(assembly))
    pace["enabled"] = bool(pacing and pol["pacing"]["enabled"])
    warnings += _long_holds(doc["tracks"]["video"], pol, pace["enabled"])
    dwell, zdelta, zmax = zoomstate.policy_limits(pol)
    zoom = zoomstate.validate(doc["tracks"]["video"], dwell, zdelta, zmax)
    warnings += zoomstate.format_violations(zoom)
    cutcheck = cutwords.run_checks(doc, words, env, pol)
    warnings += cutcheck["warnings"]
    for k in cuts:
        k.pop("_region", None)
    return {"words": words, "decisions": decisions, "cuts": cuts, "doc": doc, "timemap": tm, "warnings": warnings, "pacing": pace,
            "zoom": zoom, "cutcheck": cutcheck}


def check_doc(ep: Episode, doc: dict, pol: dict | None = None) -> dict:
    """The post-pass validators for ANY compiled EditDoc (a final, a rough someone edited in Resolve): zoom-state rule and cuts in/at words.
    Read-only. Returns {zoom, cutcheck, warnings}. Without the transcript or analysis audio the parts that need them are skipped, with a warning."""
    pol = pol or resolve_policy(ep)[0]
    dwell, delta, mx = zoomstate.policy_limits(pol)
    zoom = zoomstate.validate(doc["tracks"]["video"], dwell, delta, mx)
    warnings = zoomstate.format_violations(zoom)
    tp = ep.artifact("transcript.json")
    if not tp.exists() or not doc["tracks"].get("dialogue"):
        return {"zoom": zoom, "cutcheck": None, "warnings": warnings + ["cut-in-a-word check skipped: no transcript"]}
    words = prepare_words(read_json(tp))
    try:
        env = _env_for(ep, doc, pol)
    except (SystemExit, OSError, ValueError, KeyError):
        env = None
        warnings.append("cut-in-a-word check ran on the transcript only: no analysis audio (run ave audio)")
    cc = cutwords.run_checks(doc, words, env, pol)
    return {"zoom": zoom, "cutcheck": cc, "warnings": warnings + cc["warnings"]}


def totals(decisions: list[dict], cuts: list[dict], in_dur: float, out_dur: float) -> dict:
    by = lambda t, *a: sum(1 for d in decisions if d["type"] == t and d["action"] in a)  # noqa: E731
    masked: dict[str, int] = {}
    for k in cuts:
        masked[k["mask"]["type"]] = masked.get(k["mask"]["type"], 0) + 1
    word_removed = sum(d["e1"] - d["e0"] for d in decisions if d["action"] == "cut" and d["type"] != "dead_air" and d["type"] != "nonlexical")
    removed = sum(k["e1"] - k["e0"] for k in cuts)
    return {"input_duration_s": round(in_dur, 3), "output_duration_s": round(out_dur, 3), "time_removed_s": round(removed, 3),
            "cuts": len(cuts), "fillers_cut": by("filler", "cut"), "repeats_cut": by("repeat", "cut"),
            "false_starts_cut": by("false_start", "cut"), "dead_air_shortened": by("dead_air", "shorten"),
            "dead_air_removed_s": round(removed - word_removed, 3), "flagged_for_review": sum(1 for d in decisions if d["review"]),
            "cuts_needing_review": sum(1 for k in cuts if k["review"]), "masks": masked}


def report_name(name: str | None) -> str:
    return f"cleanup.{name}.json" if name else "cleanup.json"


def run(ep: Episode, policy_path: Path | str | None = None, dry_run: bool = False, from_version: int | None = None,
        home: Path | None = None, name: str | None = None, pacing: bool = True) -> dict:
    """Detect, decide, mask, pace, and (unless dry_run) save the next rough EditDoc and work/cleanup.json.
    `pacing=False` skips the reaction-shot pass for this run (`ave cleanup --no-pacing`).

    `name` (a session id) cleans the named assembly `assembly.<name>` into `rough.<name>` and writes work/cleanup.<name>.json."""
    pol, source = resolve_policy(ep, policy_path, home)
    versions = editdoc.list_versions(ep, "assembly", name)
    if not versions:
        raise SystemExit(f"No assembly edit{' ' + name if name else ''} yet. Run: ave assemble {ep.root}" + (f" --session {name}" if name else "") + sessions.hint(ep, name))
    ver = from_version if from_version is not None else versions[-1]
    if ver not in versions:
        raise SystemExit(f"assembly{'.' + name if name else ''} v{ver} does not exist (have: {', '.join(f'v{v}' for v in versions)})")
    apath = editdoc.path_for(ep, "assembly", ver, name)
    assembly = editdoc.load(apath)
    if not ep.artifact("transcript.json").exists():
        raise SystemExit(f"No transcript. Run: ave transcribe {ep.root}")
    a = analyze(ep, assembly, pol, pacing)
    doc = a["doc"]
    report = {"version": 1, "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "episode": ep.root.name,
              "assembly": {"file": str(apath.relative_to(ep.root)), "version": ver},
              "policy": {"source": source, "resolved": pol}, "dry_run": dry_run,
              "rough": None, "totals": totals(a["decisions"], a["cuts"], assembly["duration"], doc["duration"]),
              "pacing": a["pacing"], "zoom": a["zoom"], "cutcheck": a["cutcheck"],
              "decisions": a["decisions"], "cuts": a["cuts"], "warnings": a["warnings"]}
    if dry_run:
        return report
    path = editdoc.save_new_version(ep, doc, name)
    report["rough"] = {"file": str(path.relative_to(ep.root)), "version": editdoc.list_versions(ep, "rough", name)[-1]}
    if name:
        report["name"] = name
    write_json(ep.artifact(report_name(name)), report)
    ep.mark_stage("rough", edit=path.name)
    t = report["totals"]
    flag = f" --name {name}" if name else ""
    state.set_review(ep, f"rough cut ready: {path.name} ({t['time_removed_s']:.1f}s removed, {t['cuts']} cuts, "
                         f"{t['cuts_needing_review']} need review, {report['pacing']['inserted']} pacing reaction shots). Read `ave cleanup explain {ep.root}{flag}`, then confirm before the final cut")
    return report


# ---- explain ----------------------------------------------------------------------------------------------------

def explain(report: dict) -> str:
    """Human-readable account of every decision, flagged items first."""
    t = report["totals"]
    L = [f"Cleanup of {report['episode']}  (assembly v{report['assembly']['version']} -> "
         f"{report['rough']['file'] if report.get('rough') else 'DRY RUN, nothing saved'})",
         f"Policy: {report['policy']['source']}",
         f"{t['input_duration_s']:.1f}s -> {t['output_duration_s']:.1f}s ({t['time_removed_s']:.2f}s removed in {t['cuts']} cuts): "
         f"{t['fillers_cut']} fillers, {t['repeats_cut']} repeats, {t['false_starts_cut']} false starts, "
         f"{t['dead_air_shortened']} pauses shortened ({t['dead_air_removed_s']:.2f}s).",
         "Masks: " + (", ".join(f"{k} x{v}" for k, v in sorted(t["masks"].items())) or "none")]
    L += _pacing_lines(report.get("pacing"))
    L += _check_lines(report.get("zoom"), report.get("cutcheck"))
    L.append("")
    cutmap = {k["id"]: k for k in report["cuts"]}
    need = [d for d in report["decisions"] if d["review"]]
    if need:
        L.append(f"NEEDS A HUMAN LOOK ({len(need)}):")
        L += [_line(d, cutmap) for d in need]
        L.append("")
    L.append("All decisions:")
    L += [_line(d, cutmap) for d in report["decisions"]]
    if report["warnings"]:
        L += ["", "Warnings:"] + [f"  - {w}" for w in report["warnings"]]
    return "\n".join(L)


def _check_lines(zoom: dict | None, cc: dict | None) -> list[str]:
    """The post-pass validators in words: zoom-state rule and cuts that fall in (or clip) a word."""
    out = []
    if zoom:
        out.append(f"Zoom: {zoom['zoom_changes']} changes ({zoom['ins']} in, {zoom['outs']} out), shortest interval {zoom['min_interval_s']}s, "
                   f"dwell limit {zoom['dwell_s']:g}s: {zoom['short_dwell']} too-quick changes, {zoom['in_out_in']} in-out-in, "
                   f"{zoom['multiple_ins']} shots with more than {zoom['max_ins_per_shot']} punch-in" + ("" if zoom["ok"] else "  <- CHECK"))
    if cc:
        out.append(f"Cuts in words: {len(cc['inside_word'])} slice through a transcript word ({len(cc.get('stubs') or [])} leave a short stub of a removed word), "
                   f"{len(cc['hot_edges'])} at a word edge with speech continuing"
                   + ("" if cc.get("audio_checked") else " (audio not checked)"))
    return out


def _pacing_lines(p: dict | None) -> list[str]:
    """The pacing pass in words: what it inserted, how the shot rhythm changed, and every hold it could not fix."""
    if not p:
        return []
    if not p.get("enabled"):
        return ["Pacing: off (policy pacing.enabled or --no-pacing)"]
    b, a = p["before"], p["after"]
    out = [f"Pacing: {p['inserted']} reaction shots ({', '.join(f'{k} x{v}' for k, v in sorted(p['by_mask'].items())) or 'none'}); "
           f"holds over {p['max_hold_s']:g}s {b['holds_over_limit']} -> {a['holds_over_limit']}, average shot {b['avg_shot_s']}s -> {a['avg_shot_s']}s, "
           f"cuts per minute {b['cuts_per_min']} -> {a['cuts_per_min']}"]
    out += [f"  {r['id']} {r['o0']:.2f}-{r['o1']:.2f}s  {r['mask']} on {r['camera']}  conf {r['confidence']:.2f}  {r['reason']}" for r in p["decisions"][:40]]
    if len(p["decisions"]) > 40:
        out.append(f"  ... and {len(p['decisions']) - 40} more in work/cleanup.json")
    out += [f"  STILL OVER THE LIMIT {h['o0']:.1f}-{h['o1']:.1f}s ({h['length_s']:.1f}s{', within max_hold_s + reaction.max_s' if h.get('within_tolerance') else ''}): {h['why']}"
            for h in p["still_over_limit"][:40]]
    return out


def _line(d: dict, cutmap: dict) -> str:
    txt = f' "{d["text"]}"' if d.get("text") else ""
    s = f'  {d["id"]} {d["type"]}{txt} {d["e0"]:.2f}-{d["e1"]:.2f}s  {d["action"].upper()}  conf {d["confidence"]:.2f}  {d["reason"]}'
    k = cutmap.get(d.get("cut") or "")
    if k:
        s += f'\n      splice {k["id"]} at {k["e0"]:.3f}-{k["e1"]:.3f}s (xfade {k["xfade_ms"]} ms, {k["context"]}); mask: {k["mask"]["type"]}: {k["mask"]["reason"]}'
    return s
