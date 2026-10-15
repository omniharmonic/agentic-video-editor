"""Where can a clip begin and end? Boundary judgement for the clip finder (pure functions over a word list, no I/O).

The ASR's punctuation is unreliable: on the real 103-minute session only 6% of words carry terminal punctuation and the
chunks it does punctuate often start in lowercase in the middle of a sentence. So "is this a sentence start?" cannot be read
from one signal. This module combines the cheap ones, each explainable:

  * speaker turns, pause lengths, terminal punctuation (when present), capitalisation of the first word;
  * discourse openers ("and", "but", "so", "because", "which", "like"...) CONTINUE the previous thought: a clip must not begin
    there unless the previous sentence is part of it;
  * a lowercase first word (not "i") is the middle of a sentence;
  * dangling endings (ends on "and", "the", "but", "so", a preposition, a comma, a cut-off dash) are unfinished;
  * a first sentence that opens on an unresolved referent ("that's", "it", "they"...) is self-contained only if its antecedent
    is inside the clip, and at the start of a clip nothing can be: it is flagged `needs_context`.

`Segmenter` answers for ONE word index: `start_info(a)` (can a clip begin on word a?), `end_info(b)` (can it end on word b?),
`needs_context(a)`. Fillers (um, uh) at an edge are looked past, exactly as `Sentences.snap` trims them. Every number and word
list lives in the rubric's `completeness` section (the defaults below apply when an older policy file has none).
"""

from __future__ import annotations

import copy
import re
from typing import Any

_TERMINAL = (".", "?", "!", "…")
_CLOSERS = "\"')]}”’"
_OPENERS = "\"'([{“‘"
_DASHES = ("-", "–", "—", "--")

DEFAULTS: dict[str, Any] = {
    "expand": True,                 # snap a window OUT to the nearest complete thought start/end instead of cutting in
    "expand_picks": True,           # the same for the agent's picks (reported on the clip as `expanded`, never silent)
    "max_expand_back": 8,           # at most this many sentences earlier / later (a speaker can chain "And ... And ... And ..." for a while)
    "max_expand_forward": 4,
    "prefer_complete_margin": 15,   # in a cluster of overlapping windows, a complete statement within this many points of the best wins
    "pause_s": 0.8,                 # a pause this long is a break even without punctuation...
    "merge_gap_s": 2.5,             # ...unless the speaker was mid-clause (last word dangling or a comma) and resumes in lowercase within this long
    "interjection_max_words": 3,    # a short line by the OTHER speaker (mm-hm, yeah) is skipped over when walking to a thought's edge
    "context_sentences": 2,         # context_before / context_after
    "context_chars": 320,
    "variant_max_back": 2,          # how far back the "needs_context" expanded variant may reach
    "continuation_openers": ["and", "but", "so", "because", "cuz", "cause", "which", "then", "or", "nor", "also", "plus", "like",
                             "since", "however", "although", "though", "whereas"],
    "dangling_endings": ["and", "but", "or", "so", "because", "cuz", "cause", "which", "that", "who", "whose", "where", "when", "while",
                         "the", "a", "an", "of", "to", "in", "on", "at", "for", "with", "from", "by", "as", "if", "than", "then",
                         "like", "i", "we", "he", "she", "they", "is", "are", "was", "were", "be", "been", "am", "have", "has", "had",
                         "do", "does", "did", "not", "just", "really", "very", "my", "your", "our", "their", "his", "her", "its",
                         "these", "those", "kind", "sort", "about", "into", "onto", "over", "under", "through", "between", "would",
                         "could", "should", "will", "can", "gonna", "wanna", "i'm", "i'd", "i've", "i'll", "we're", "we've", "we'd",
                         "we'll", "you're", "you've", "you'd", "they're", "they've", "it's", "that's", "there's", "what's", "he's",
                         "she's", "let's", "some", "any", "every", "each", "other", "more", "most", "such", "also"],
    "referent_subjects": ["it", "it's", "they", "they're", "they've", "them", "he", "he's", "she", "she's"],
    "referent_demonstratives": ["that", "this", "these", "those"],
    "referent_verbs": ["is", "was", "are", "were", "means", "will", "would", "can", "could", "has", "have", "does", "did", "isn't",
                       "wasn't", "doesn't", "didn't", "won't", "might", "must", "should", "which", "to", "'s", "makes", "feels", "seems"],
    "referent_contractions": ["that's", "this's", "that'll", "that'd", "those're"],
}


def config(rubric: dict) -> dict:
    """Packaged defaults under the rubric's `completeness` section (an older policy file without one still works)."""
    out = copy.deepcopy(DEFAULTS)
    out.update({k: v for k, v in (rubric.get("completeness") or {}).items() if k in DEFAULTS})
    out["continuation_openers"] = sorted(set(out["continuation_openers"]) | set((rubric.get("heuristics") or {}).get("mid_thought_starts", [])))
    return out


def norm_token(w: str) -> str:
    return re.sub(r"[^a-z0-9']", "", w.lower().replace("’", "'"))


def is_terminal(token: str, abbreviations: set[str]) -> bool:
    t = token.rstrip(_CLOSERS)
    if not t.endswith(_TERMINAL):
        return False
    if t.endswith("."):
        core = t[:-1].lower()
        if core in abbreviations or re.fullmatch(r"[a-z]", core) or re.fullmatch(r"(?:[a-z]\.)+[a-z]", core):
            return False
    return True


def starts_lower(token: str) -> bool:
    """The first LETTER of the raw word is lowercase (and it is not the pronoun "i"). Digits, quotes and symbols are not judged."""
    t = token.lstrip(_OPENERS)
    if not t or not t[0].isalpha():
        return False
    n = norm_token(t)
    if n == "i" or n.startswith("i'"):
        return False
    return t[0].islower()


def capitalisation_is_informative(words: list[dict], abbr: set[str]) -> bool:
    """Is the ASR's capitalisation worth reading? Judged on the transcript itself: after a word with terminal punctuation the next
    word should be capitalised. An all-lowercase transcript (some engines, some fixtures) says nothing, and then 'lowercase first
    word' is not evidence of a mid-sentence start."""
    after = upper = 0
    for a, b in zip(words, words[1:]):
        if is_terminal(a["w"], abbr):
            t = b["w"].lstrip(_OPENERS)
            if t and t[0].isalpha():
                after += 1
                upper += t[0].isupper() or norm_token(t) == "i" or norm_token(t).startswith("i'")
    if after >= 5:
        return upper / after >= 0.6
    letters = [w["w"].lstrip(_OPENERS)[:1] for w in words if w["w"].lstrip(_OPENERS)[:1].isalpha()]
    return bool(letters) and sum(c.isupper() for c in letters) / len(letters) >= 0.05


class Segmenter:
    """Boundary judgement over the mapped word list of one stage doc (`Sentences.words`)."""

    def __init__(self, words: list[dict], fillers: set[str], rubric: dict):
        self.words = words
        self.fillers = fillers
        self.cfg = config(rubric)
        self.abbr = {a.lower() for a in (rubric.get("sentences") or {}).get("abbreviations", [])}
        self.openers = set(self.cfg["continuation_openers"])
        self.dangling = set(self.cfg["dangling_endings"])
        self.ctx_phrases = [p.lower() for p in (rubric.get("heuristics") or {}).get("context_phrases", [])]
        self._start: dict[int, dict] = {}
        self._end: dict[int, dict] = {}
        self.use_case = self._capitalisation_is_informative()

    def _capitalisation_is_informative(self) -> bool:
        return capitalisation_is_informative(self.words, self.abbr)

    # -- helpers --------------------------------------------------------------------------------------------
    def _filler(self, k: int) -> bool:
        return norm_token(self.words[k]["w"]) in self.fillers

    def prev_word(self, a: int) -> int | None:
        k = a - 1
        while k >= 0 and self._filler(k):
            k -= 1
        return k if k >= 0 else None

    def next_word(self, b: int) -> int | None:
        k = b + 1
        while k < len(self.words) and self._filler(k):
            k += 1
        return k if k < len(self.words) else None

    def _break_before(self, a: int, p: int) -> str | None:
        """Why there is a real break between word p (the previous spoken word) and word a, or None if the speech runs on."""
        wp, wa = self.words[p], self.words[a]
        if is_terminal(wp["w"], self.abbr):
            return "terminal punctuation"
        sp, sa = wp.get("speaker"), wa.get("speaker")
        if sp is not None and sa is not None and sp != sa:
            return "speaker turn"
        if wa["start"] - wp["end"] >= self.cfg["pause_s"]:
            return "pause"
        return None

    # -- can a clip begin on word a? -------------------------------------------------------------------------
    def start_info(self, a: int) -> dict:
        got = self._start.get(a)
        if got is None:
            got = self._start[a] = self._start_info(a)
        return got

    def _start_info(self, a: int) -> dict:
        w = self.words[a]
        tok = norm_token(w["w"])
        p = self.prev_word(a)
        if p is None:
            return {"ok": True, "reasons": [], "kind": None}
        reasons: list[tuple[str, str]] = []
        if tok in self.openers:
            reasons.append(("opener", f"opens with '{w['w'].strip(',.')}', which continues the previous thought"))
        if self.use_case and starts_lower(w["w"]):
            reasons.append(("lowercase", f"starts in lowercase ('{w['w']}'): the middle of a sentence"))
        wp = self.words[p]
        ptok = norm_token(wp["w"])
        brk = self._break_before(a, p)
        if brk is None:
            reasons.append(("no_break", f"no sentence break before '{w['w']}' (the speaker runs on from '{wp['w']}')"))
        elif brk != "terminal punctuation" and not wp["w"].rstrip(_CLOSERS).endswith(_TERMINAL):
            # a turn / pause break after a word that never finished its clause: the thought is still open
            if ptok in self.dangling or wp["w"].rstrip().endswith(","):
                if self.words[p].get("speaker") == w.get("speaker"):
                    reasons.append(("continues", f"continues '{wp['w']}' (the previous clause is unfinished)"))
        return {"ok": not reasons, "reasons": [r for _, r in reasons], "kind": reasons[0][0] if reasons else None}

    # -- can a clip end on word b? ---------------------------------------------------------------------------
    def end_info(self, b: int) -> dict:
        got = self._end.get(b)
        if got is None:
            got = self._end[b] = self._end_info(b)
        return got

    def _end_info(self, b: int) -> dict:
        w = self.words[b]
        raw = w["w"].rstrip(_CLOSERS)
        tok = norm_token(w["w"])
        q = self.next_word(b)
        if q is None:
            return {"ok": True, "reasons": [], "kind": None}
        wq = self.words[q]
        reasons: list[tuple[str, str]] = []
        if raw.endswith(_DASHES):
            reasons.append(("cutoff", f"'{w['w']}' is cut off (a dash: the speaker abandoned the clause)"))
        elif is_terminal(w["w"], self.abbr):
            same = w.get("speaker") == wq.get("speaker")
            if same and wq["start"] - w["end"] < 0.5 and self.use_case and starts_lower(wq["w"]):
                reasons.append(("lowercase_next", f"'{wq['w']}' follows in lowercase: the sentence probably runs on"))
        else:
            if raw.endswith(","):
                reasons.append(("comma", f"ends on a comma after '{w['w'].rstrip(',')}': the clause is unfinished"))
            elif tok in self.dangling:
                reasons.append(("dangling", f"ends on '{w['w']}', a word that needs something after it"))
            sp, sq = w.get("speaker"), wq.get("speaker")
            ran_on = (sp is None or sq is None or sp == sq) and wq["start"] - w["end"] < self.cfg["pause_s"]
            if ran_on and not reasons:
                reasons.append(("no_break", f"no sentence break after '{w['w']}' (the speaker runs on into '{wq['w']}')"))
        return {"ok": not reasons, "reasons": [r for _, r in reasons], "kind": reasons[0][0] if reasons else None}

    # -- unresolved referent at the start ------------------------------------------------------------------------
    def needs_context(self, a: int) -> str | None:
        """Why the first sentence cannot stand alone (an unresolved 'it/they/that is...' or 'as I said'), else None."""
        toks = []
        k = a
        while k < len(self.words) and len(toks) < 8:
            if not self._filler(k):
                toks.append(norm_token(self.words[k]["w"]))
            k += 1
        if not toks:
            return None
        head = " ".join(toks)
        for ph in self.ctx_phrases:
            if re.search(rf"(?<![a-z']){re.escape(ph)}(?![a-z'])", head):
                return f"refers back to earlier talk ('{ph}')"
        t0 = toks[0]
        c = self.cfg
        if t0 in c["referent_subjects"]:
            return f"opens on '{self.words[a]['w'].strip(',.')}' with no antecedent in the clip"
        if t0 in c["referent_contractions"]:
            return f"opens on '{t0}' with no antecedent in the clip"
        if t0 in c["referent_demonstratives"] and len(toks) > 1 and toks[1] in c["referent_verbs"]:
            return f"opens on '{t0} {toks[1]}' with no antecedent in the clip"
        return None

    # -- one-line verdict ------------------------------------------------------------------------------------------
    def verdict(self, first: int, last: int) -> dict:
        """Completeness of a clip spanning words first..last."""
        s, e = self.start_info(first), self.end_info(last)
        notes = []
        if not s["ok"]:
            notes.append("starts mid-sentence: " + s["reasons"][0])
        if not e["ok"]:
            notes.append("ends mid-thought: " + e["reasons"][0])
        return {"start_complete": s["ok"], "end_complete": e["ok"], "complete_statement": s["ok"] and e["ok"],
                "completeness_note": "; ".join(notes) or None, "start_kind": s["kind"], "end_kind": e["kind"]}


def split_score(words: list[dict], k: int, dangling: set[str]) -> float:
    """How good a place to cut a too-long run is, between words k-1 and k: a longer pause, a comma and a capital are good; cutting
    right after a dangling word is bad. Used by `clips._split_long` for runs the ASR left unpunctuated."""
    a, b = words[k - 1], words[k]
    s = b["start"] - a["end"]
    if a["w"].rstrip().endswith(","):
        s += 0.4
    if b["w"][:1].isupper():
        s += 0.2
    if norm_token(a["w"]) in dangling:
        s -= 1.0
    return s
