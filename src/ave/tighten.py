"""Tighten suggestions for one clip: what could be cut to make it shorter without hurting it. SUGGESTIONS ONLY, never applied.

Conservative on purpose. A suggestion is a run of whole words (never inside a word) from one of:

  filler    lexical fillers and runs of hedging ("you know, I mean, like,") that carry no content
  repeat    "the the", "we need to we need to": the earlier copy
  restart   an abandoned clause ("wh-", "we-") followed by the restart
  aside     a listener interjection in the middle of the speaker's thought, or a comma-bounded "which ..." clause
  tangent   a whole sentence in the middle of the clip that announces itself as an aside ("by the way") or shares no content
            with the hook and the payoff

and each one must pass the same three safety rules:

  1. it never touches the hook or the payoff: no whole-sentence or clause cut inside the opening sentence, the sentence that
     holds the hook, the last sentence or the sentence that holds the end quote, and no cut of any kind inside the hook or end
     quote themselves;
  2. the seam it leaves must read as speech: the words on either side of the cut are checked together (no stutter, no "the is",
     no sentence that now starts on "and"), and the whole set is re-checked after overlapping cuts merge;
  3. the clip must stay a clip: all suggestions together save at most `tighten.max_fraction` of its length and never leave it
     under the rubric's `limits.hard_min_s`.

Detection of fillers, repeats and false starts reuses `ave.cleanup` (the same detectors the rough cut uses), so the same
numbers decide. Times are EPISODE seconds (`start`/`end`, per the round-2 contract) plus the OUTPUT seconds of the clip's own
timeline (`o_start`/`o_end`). Every number and word list lives in the rubric's `tighten` section.
"""

from __future__ import annotations

import copy
import functools
from typing import Any

from . import cleanup, recall, segment

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "min_confidence": 0.4,        # drop suggestions below this
    "max_fraction": 0.35,         # all suggestions together save at most this share of the clip
    "hedges": ["you know what i mean", "you know", "i mean", "sort of", "kind of", "kinda", "sorta", "i guess", "basically",
               "literally", "honestly", "or something like that", "or something", "or whatever", "and stuff like that", "and stuff",
               "like", "right"],
    "hedge_run_min": 2,           # a hedge phrase is cut only as part of a run of this many (or as one long phrase of 3+ words)
    "hedge_run_gap_words": 1,     # hedge phrases at most this many words apart form one run
    "aside_max_words": 4,         # a listener interjection this short inside the speaker's thought...
    "aside_words": ["yeah", "yes", "yep", "mm-hmm", "mm-hm", "mhm", "uh-huh", "mm", "hmm", "right", "okay", "ok", "sure", "exactly",
                    "wow", "oh", "ah", "cool", "totally", "absolutely", "true", "i see", "for sure", "of course", "got it", "interesting",
                    "that's right", "that's true", "ha", "haha", "yeah yeah", "no way", "really"],   # ...made only of these (a one-word diarization glitch is NOT an aside)
    "chatter": ["sorry", "hang on", "hold on", "one second", "one sec", "can you hear", "can you see", "the mic", "the camera", "are we recording",
                "let me just", "bear with me"],   # off-topic housekeeping: a sentence containing one of these is a tangent
    "subclause": {"starts": ["which", "who", "whose"], "min_words": 3, "max_words": 14, "min_words_left": 6},
    # use_overlap: also call a sentence a tangent when it shares no content word with the rest of the clip. OFF by default: on the real
    # session it flagged joke set-ups and on-topic lines (word overlap is not relevance). Markers and housekeeping chatter are reliable.
    "tangent": {"min_sentences": 4, "min_words": 8, "marker_confidence": 0.6, "use_overlap": False, "overlap_confidence": 0.4},
    "stopwords": ["about", "after", "again", "also", "because", "been", "being", "could", "does", "doing", "from", "have", "having",
                  "here", "just", "like", "more", "much", "only", "other", "really", "should", "some", "that", "their", "them",
                  "then", "there", "these", "they", "thing", "things", "this", "those", "very", "want", "were", "what", "when",
                  "where", "which", "while", "will", "with", "would", "your", "kind", "going", "know", "think", "okay", "yeah"],
}
_PRIORITY = {"filler": 0, "restart": 1, "repeat": 2, "aside": 4, "tangent": 5}
_RIGHT_BAD = {"is", "are", "was", "were", "be", "been", "am", "and", "but", "or", "so", "because", "which", "that", "to", "of", "in",
              "on", "at", "for", "with", "if", "then", "than", "as"}


def config(rubric: dict) -> dict:
    out = copy.deepcopy(DEFAULTS)
    for k, v in (rubric.get("tighten") or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def _content(text: str, stop: set[str]) -> set[str]:
    return {t for t in (segment.norm_token(x) for x in text.split()) if len(t) >= 4 and t not in stop}


class _Ctx:
    """Working state for one clip: the clip's words, sentence spans in local word indexes, protected words and sentences."""

    def __init__(self, S: Any, i: int, j: int, sn: dict, clip: dict, rubric: dict):
        self.S, self.i, self.j = S, i, j
        self.cfg = config(rubric)
        self.markers = [" ".join(segment.norm_token(y) for y in p.split()) for p in recall.config(rubric)["tags"].get("tangent", [])]
        self.a, self.b = sn["first_word"], sn["last_word"]
        self.W = S.words[self.a:self.b + 1]
        self.n = len(self.W)
        self.tok = [segment.norm_token(w["w"]) for w in self.W]
        self.dangling = S.seg.dangling
        self.spans: dict[int, tuple[int, int]] = {}                       # sentence index -> (lo, hi) local, inclusive
        for k in range(i, j + 1):
            it = S.items[k]
            lo, hi = max(it["w0"], self.a) - self.a, min(it["w1"], self.b + 1) - self.a - 1
            if lo <= hi:
                self.spans[k] = (lo, hi)
        self.prot_words: set[int] = set()
        self.prot_sent: set[int] = {i, j}
        for key in ("hook_quote", "end_quote"):
            q = clip.get(key)
            if q:
                span = self._find(q)
                if span:
                    self.prot_words |= set(range(span[0], span[1] + 1))
                    self.prot_sent.add(self._sentence_of(span[0]))
                    self.prot_sent.add(self._sentence_of(span[1]))
        if not clip.get("hook_quote"):
            stags = recall.sentence_tags(S)
            self.prot_sent.add(next((k for k in range(i, j + 1) if stags[k] & {"hook", "claim", "quotable"}), i))
        self.hook_text = " ".join(str(clip.get(k) or "") for k in ("hook", "hook_quote", "end_quote"))

    def _find(self, quote: str) -> tuple[int, int] | None:
        q = [t for t in (segment.norm_token(x) for x in quote.split()) if t]
        if not q:
            return None
        for s in range(self.n - len(q) + 1):
            if self.tok[s:s + len(q)] == q:
                return s, s + len(q) - 1
        return None

    def _sentence_of(self, w: int) -> int:
        for k, (lo, hi) in self.spans.items():
            if lo <= w <= hi:
                return k
        return self.i

    def opens_with_marker(self, lo: int, hi: int, within: int = 5) -> bool:
        """The sentence announces itself as an aside ("by the way", "anyway", "side note"...) in its first few words. The same word at the end
        ("... and we shipped it anyway") is not an announcement."""
        head = " " + " ".join(self.tok[lo:min(hi + 1, lo + within)]) + " "
        return any(" " + m + " " in head for m in self.markers if m)

    def contiguous(self, lo: int, hi: int) -> bool:
        """The words lo..hi are one unbroken stretch of the episode (no cut seam inside), so episode seconds mean something."""
        for k in range(lo, hi):
            a, b = self.W[k], self.W[k + 1]
            if abs((b["e0"] - (a["e0"] + a["end"] - a["start"])) - (b["start"] - a["end"])) > 0.05:
                return False
        return True

    def seam_ok(self, left: int | None, right: int | None) -> bool:
        """Do kept words `left` and `right` read as speech once everything between them is gone?"""
        if left is None or right is None:
            return True
        lt, rt = self.tok[left], self.tok[right]
        if lt and lt == rt:
            return False                                                # a stutter
        if lt in self.dangling and rt in _RIGHT_BAD:
            return False                                                # "the is", "and and", "to of"
        if segment.is_terminal(self.W[left]["w"], self.S.seg.abbr):
            if rt in self.S.seg.openers or (self.S.seg.use_case and segment.starts_lower(self.W[right]["w"])):
                return False                                            # a sentence that now starts on "and"
        return True


# ---- candidate generators ----------------------------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _policy() -> dict:
    return cleanup.load_policy()


def _cleanup_candidates(x: _Ctx) -> list[dict]:
    pol = _policy()
    cw = [{"i": k, "ti": k, "w": w["w"], "n": cleanup.norm(w["w"]), "start": w["start"], "end": w["end"], "speaker": w.get("speaker"), "conf": 1.0}
          for k, w in enumerate(x.W)]
    kinds = {"filler": "filler", "false_start": "restart", "repeat": "repeat"}
    out = []
    for c in cleanup.detect_candidates(cw, pol):
        if c["type"] in kinds and c["action"] == "cut":
            out.append({"kind": kinds[c["type"]], "lo": min(c["idx"]), "hi": max(c["idx"]), "level": "word",
                        "confidence": float(c["confidence"]), "reason": c["reason"]})
    return out


def _hedge_candidates(x: _Ctx) -> list[dict]:
    phrases = sorted((p.split() for p in x.cfg["hedges"]), key=lambda p: -len(p))
    occ: list[tuple[int, int, str]] = []
    k = 0
    while k < x.n:
        hit = None
        for ph in phrases:
            if x.tok[k:k + len(ph)] == ph and k + len(ph) <= x.n:
                if len(ph) == 1 and ph[0] in ("like", "right"):
                    comma = x.W[k]["w"].endswith(",") or (k > 0 and x.W[k - 1]["w"].endswith(","))
                    if not comma:
                        continue
                hit = ph
                break
        if hit:
            occ.append((k, k + len(hit) - 1, " ".join(hit)))
            k += len(hit)
        else:
            k += 1
    runs: list[list[tuple[int, int, str]]] = []
    for o in occ:
        if runs and o[0] - runs[-1][-1][1] - 1 <= x.cfg["hedge_run_gap_words"]:
            runs[-1].append(o)
        else:
            runs.append([o])
    out = []
    for r in runs:
        lo, hi = r[0][0], r[-1][1]
        if any(segment.is_terminal(x.W[k]["w"], x.S.seg.abbr) for k in range(lo, hi)) or x._sentence_of(lo) != x._sentence_of(hi):
            continue                                                    # a run never crosses a sentence boundary
        if len(r) >= x.cfg["hedge_run_min"] or (len(r) == 1 and len(r[0][2].split()) >= 3):
            text = " ".join(w["w"] for w in x.W[lo:hi + 1])
            out.append({"kind": "filler", "lo": lo, "hi": hi, "level": "word", "confidence": 0.55,
                        "reason": f'hedging run "{text}": says nothing the sentence needs'})
    return out


def _sentence_candidates(x: _Ctx) -> list[dict]:
    S, cfg = x.S, x.cfg
    out: list[dict] = []
    ids = list(range(x.i, x.j + 1))
    stags = recall.sentence_tags(S)
    stop = set(cfg["stopwords"])
    aside_words = {" ".join(segment.norm_token(y) for y in p.split()) for p in cfg["aside_words"]}
    relevance = _content(x.hook_text, stop)
    for k in x.prot_sent:
        if k in x.spans:
            relevance |= _content(S.items[k]["text"], stop)
    n_sent = len(ids)
    for k in ids[1:-1]:
        if k in x.prot_sent or k not in x.spans:
            continue
        it, prev, nxt = S.items[k], S.items[k - 1], S.items[k + 1]
        lo, hi = x.spans[k]
        # a listener's interjection (mm-hm, yeah, right) in the middle of the speaker's thought. A short line from the other speaker that is
        # NOT a backchannel is usually a diarization glitch inside the speaker's own sentence: removing it would break the sentence.
        low = " ".join(x.tok[lo:hi + 1])
        if (it["speaker"] != prev["speaker"] and prev["speaker"] == nxt["speaker"] and it["features"]["n_words"] <= cfg["aside_max_words"]
                and low in aside_words):
            out.append({"kind": "aside", "lo": lo, "hi": hi, "level": "sentence", "confidence": 0.7,
                        "reason": f'{it["speaker"] or "the other speaker"} interjects "{it["text"]}" in the middle of the speaker\'s thought'})
            continue
        t = cfg["tangent"]
        if n_sent < t["min_sentences"] or not (prev["features"]["end_complete"] and nxt["features"]["start_complete"]):
            continue
        n_tok = hi - lo + 1
        lowtext = " " + " ".join(x.tok[lo:(hi + 1) if n_tok <= 8 else lo + 6]) + " "       # housekeeping opens a sentence or IS a short one
        if "tangent" in stags[k] and x.opens_with_marker(lo, hi):
            out.append({"kind": "tangent", "lo": lo, "hi": hi, "level": "sentence", "confidence": t["marker_confidence"],
                        "reason": "the speaker flags it as an aside (by the way / anyway / side note)"})
        elif any(" " + " ".join(segment.norm_token(y) for y in ph.split()) + " " in lowtext for ph in cfg["chatter"]):
            out.append({"kind": "tangent", "lo": lo, "hi": hi, "level": "sentence", "confidence": t["marker_confidence"],
                        "reason": "housekeeping chatter (sorry / hold on / can you see...), not part of the point"})
        elif t["use_overlap"] and it["features"]["n_words"] >= t["min_words"]:
            # the weakest evidence, so the lowest confidence: it shares no content word with ANY other sentence of the clip
            mine = _content(it["text"], stop)
            rest = set()
            for m in ids:
                if m != k:
                    rest |= _content(S.items[m]["text"], stop)
            if len(mine) >= 3 and not (mine & rest) and not (mine & relevance):
                out.append({"kind": "tangent", "lo": lo, "hi": hi, "level": "sentence", "confidence": t["overlap_confidence"],
                            "reason": "shares no content word with the rest of the clip"})
    # comma-bounded relative clauses: "X, which means Y, is Z" reads the same without "which means Y,"
    sc = cfg["subclause"]
    for k, (lo, hi) in x.spans.items():
        if k in x.prot_sent:
            continue
        for t in range(lo + 1, hi):
            if not (x.W[t - 1]["w"].endswith(",") and x.tok[t] in sc["starts"]):
                continue
            for u in range(t + sc["min_words"] - 1, min(hi, t + sc["max_words"] - 1)):
                if x.W[u]["w"].endswith(","):
                    if (hi - lo + 1) - (u - t + 1) >= sc["min_words_left"]:
                        out.append({"kind": "aside", "lo": t, "hi": u, "level": "clause", "confidence": 0.5,
                                    "reason": f'the "{x.tok[t]} ..." clause between commas can go; the sentence keeps its subject and verb'})
                    break
    return out


# ---- assembly ------------------------------------------------------------------------------------------------------------

def _saves(x: _Ctx, lo: int, hi: int) -> float:
    gap = max(0.0, x.W[lo]["start"] - x.W[lo - 1]["end"]) if lo > 0 else 0.0
    return round(x.W[hi]["end"] - x.W[lo]["start"] + gap, 3)


def suggest(S: Any, i: int, j: int, sn: dict, clip: dict, rubric: dict) -> dict:
    """{"tighten": [...], "tight_duration": float, "tight_text": str} for the clip over sentences i..j (snapped to `sn`)."""
    x = _Ctx(S, i, j, sn, clip, rubric)
    dur = float(sn["duration"])
    plain = {"tighten": [], "tight_duration": round(dur, 3), "tight_text": " ".join(w["w"] for w in x.W)}
    if not x.cfg["enabled"] or x.n < 6:
        return plain
    cands = _cleanup_candidates(x) + _hedge_candidates(x) + _sentence_candidates(x)
    keep: list[dict] = []
    for c in cands:
        span = set(range(c["lo"], c["hi"] + 1))
        if c["confidence"] < x.cfg["min_confidence"] or not x.contiguous(c["lo"], c["hi"]):
            continue
        if span & x.prot_words:
            continue                                                    # never inside the hook or end quote
        if c["level"] != "word" and any(c["lo"] <= x.spans[k][1] and x.spans[k][0] <= c["hi"] for k in x.prot_sent if k in x.spans):
            continue                                                    # whole-sentence / clause cuts never touch the hook or payoff sentences
        keep.append(c)
    keep.sort(key=lambda c: (_PRIORITY[c["kind"]], -c["confidence"], c["lo"]))
    chosen: list[dict] = []
    taken: set[int] = set()
    for c in keep:
        span = set(range(c["lo"], c["hi"] + 1))
        if span & taken:
            continue
        chosen.append(c)
        taken |= span
    limit = float(rubric.get("limits", {}).get("hard_min_s", 0.0))

    def seams_bad() -> list[dict] | None:
        removed = set().union(*[set(range(c["lo"], c["hi"] + 1)) for c in chosen]) if chosen else set()
        kept_idx = [k for k in range(x.n) if k not in removed]
        bad = []
        for l, r in zip(kept_idx, kept_idx[1:]):
            if r > l + 1 and not x.seam_ok(l, r):
                bad += [c for c in chosen if l < c["lo"] and c["hi"] < r]
        return bad or None

    while chosen:                                                       # drop the least trusted cut until the whole result reads
        bad = seams_bad()
        total = sum(_saves(x, c["lo"], c["hi"]) for c in chosen)
        if bad:
            chosen.remove(min(bad, key=lambda c: c["confidence"]))
        elif total > x.cfg["max_fraction"] * dur or dur - total < limit:
            chosen.remove(min(chosen, key=lambda c: c["confidence"]))
        else:
            break
    chosen.sort(key=lambda c: c["lo"])
    out = []
    for c in chosen:
        lo, hi = c["lo"], c["hi"]
        a, b = x.W[lo], x.W[hi]
        out.append({"start": round(a["e0"], 3), "end": round(b["e0"] + (b["end"] - b["start"]), 3),
                    "o_start": a["start"], "o_end": b["end"], "kind": c["kind"],
                    "text": " ".join(w["w"] for w in x.W[lo:hi + 1]), "saves_s": _saves(x, lo, hi),
                    "reason": c["reason"], "confidence": round(c["confidence"], 2)})
    removed = set().union(*[set(range(c["lo"], c["hi"] + 1)) for c in chosen]) if chosen else set()
    return {"tighten": out, "tight_duration": round(dur - sum(o["saves_s"] for o in out), 3),
            "tight_text": " ".join(w["w"] for k, w in enumerate(x.W) if k not in removed)}
