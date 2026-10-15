"""Deterministic check: no cut lands INSIDE a spoken word.

A cut boundary is where the compiled EditDoc jumps in the SOURCE: the end of one dialogue segment and the start of the next when
they are not contiguous in episode time (a splice), and the same for video segments. Boundaries are converted to the source
file's own clock with `editdoc.src_time` (never a hand-written formula; drift and sync maps are honoured) and compared with the
word times in work/transcript.json (which are on the dialogue source's clock). A hit is a boundary `t` with
`start + margin < t < end - margin` for some word.

ASR word ends are sometimes early (a clipped tail): the check therefore reports how deep into the word the cut sits (`depth_s`
from the word's start, `to_end_s` before its end) so the caller can tell a real slice from timing slop (`min_depth_s`)."""
from __future__ import annotations

import bisect

from . import editdoc

EPS = 1e-6
DEFAULT_MARGIN_S = 0.04        # tolerance at each word edge: Parakeet word times are on an 80 ms grid (all 18932 words of the first real episode), so +-40 ms


def boundaries(doc: dict, tracks: tuple[str, ...] = ("dialogue", "video")) -> list[dict]:
    """Every splice in the doc as source-clock cut points: {track, id, o, side: out|in, e, src, t}.

    `out` = the last moment kept before the splice, `in` = the first moment kept after it. Only discontinuities in
    episode time count (two segments that continue each other are not a cut)."""
    out: list[dict] = []
    for tr in tracks:
        segs = sorted(doc["tracks"].get(tr) or [], key=lambda s: s["o0"])
        for a, b in zip(segs, segs[1:]):
            if a.get("src") != b.get("src") and tr == "video":
                continue                        # a camera change is a picture cut over continuous audio, not a cut in the speech
            if "e0" not in a or "e0" not in b or a.get("fill") or b.get("fill"):
                continue
            ea = a["e0"] + (a["o1"] - a["o0"])
            if abs(ea - b["e0"]) <= 1e-4:
                continue
            for seg, e, side in ((a, ea, "out"), (b, b["e0"], "in")):
                src = doc["sources"].get(seg["src"])
                if not src:
                    continue
                out.append({"track": tr, "id": seg["id"], "o": round(b["o0"], 4), "side": side, "e": round(e, 4), "src": seg["src"],
                            "t": float(editdoc.src_time(src, e))})
    return out


def check(doc: dict, words: list[dict], margin_s: float = DEFAULT_MARGIN_S, min_depth_s: float = 0.0,
          tracks: tuple[str, ...] = ("dialogue",), stub_max_s: float = 0.15) -> list[dict]:
    """Cut points that fall inside a transcript word. `words` need `w`, `start`, `end` (the transcript's `words`).

    Returns hits sorted by output time: {o, e, t, side, track, id, word, start, end, depth_s, to_end_s, kind, kept_fragment_s}.
    `kind` is `stub` when the fragment of the word that stays in the programme is at most `stub_max_s` and the smaller part of the
    word (it is the tail/onset of a word that was removed on purpose, left by the cleanup's valley pad) or `sliced` otherwise
    (a word the speaker was saying is cut through). A hit whose `depth_s` (how far past the word's start the cut is) is below
    `min_depth_s` is ignored. The same splice is reported once per side."""
    ws = sorted((w for w in words if w.get("end", 0) > w.get("start", 0)), key=lambda w: w["start"])
    starts = [w["start"] for w in ws]
    hits, seen = [], set()
    for p in boundaries(doc, tracks):
        # candidates: the word starting last before t, plus a few before it (long words / overlapping speakers)
        i = bisect.bisect_right(starts, p["t"])
        for w in ws[max(0, i - 4):i]:
            if w["start"] + margin_s < p["t"] < w["end"] - margin_s and p["t"] - w["start"] >= min_depth_s:
                key = (p["track"], p["o"], p["side"], w["start"])
                if key in seen:
                    continue
                seen.add(key)
                depth, to_end = p["t"] - w["start"], w["end"] - p["t"]
                frag = depth if p["side"] == "out" else to_end        # the part of the word that stays in the programme
                kind = "stub" if frag <= stub_max_s and frag < (w["end"] - w["start"]) - frag else "sliced"
                hits.append({"o": p["o"], "e": p["e"], "t": round(p["t"], 4), "side": p["side"], "track": p["track"], "id": p["id"],
                             "word": w["w"], "start": w["start"], "end": w["end"], "speaker": w.get("speaker"), "kind": kind,
                             "depth_s": round(depth, 4), "to_end_s": round(to_end, 4), "kept_fragment_s": round(frag, 4)})
    hits.sort(key=lambda h: (h["o"], h["side"]))
    return hits


def format_hits(hits: list[dict], limit: int = 20) -> list[str]:
    out = [f"cut inside a word: \"{h['word'].strip()}\" ({h['start']:.2f}-{h['end']:.2f}s on the source clock) is sliced at {h['t']:.3f}s "
           f"({h['side']} side of the splice at output {h['o']:.2f}s, episode {h['e']:.2f}s), {h['depth_s'] * 1000:.0f} ms in, {h['to_end_s'] * 1000:.0f} ms before its end"
           for h in hits[:limit]]
    if len(hits) > limit:
        out.append(f"... and {len(hits) - limit} more cut-inside-a-word hits")
    return out


def hot_edges(doc: dict, words: list[dict], env, hot_db: float = 12.0, max_pad_s: float = 0.020,
              tracks: tuple[str, ...] = ("dialogue",)) -> list[dict]:
    """Cuts where speech probably continues ACROSS the cut although the transcript says a word ends/starts exactly there.

    ASR word ends run early (a clipped tail) and starts run late, so the span test in `check` can miss a sliced word. The audio
    tells: a cut point within `hot_db` dB of the typical speech level (`env.speech_db`, the p90 of the recording) that sits within
    `max_pad_s` of a word edge on the KEPT side has no tail/lead pad and is in the middle of loud sound. `env` is cleanup.Envelope.
    Hits: {o, e, side, db, speech_db, pad_s, word}."""
    ws = sorted((w for w in words if w.get("end", 0) > w.get("start", 0)), key=lambda w: w["start"])
    starts = [w["start"] for w in ws]
    out = []
    for p in boundaries(doc, tracks):
        i = bisect.bisect_right(starts, p["t"])
        if p["side"] == "out":
            prev = [w for w in ws[max(0, i - 6):i] if w["end"] <= p["t"] + 0.03]
            if not prev:
                continue
            w = max(prev, key=lambda x: x["end"])
            pad = p["t"] - w["end"]
        else:
            j = bisect.bisect_left(starts, p["t"] - 0.03)
            if j >= len(ws):
                continue
            w = ws[j]
            pad = w["start"] - p["t"]
        db = env.db_at(p["e"])
        if db > env.speech_db - hot_db and pad <= max_pad_s:
            out.append({"o": p["o"], "e": p["e"], "side": p["side"], "db": round(db, 1), "speech_db": round(env.speech_db, 1),
                        "pad_s": round(pad, 4), "word": w["w"]})
    out.sort(key=lambda h: (h["o"], h["side"]))
    return out


def run_checks(doc: dict, words: list[dict], env, pol: dict) -> dict:
    """Both cut-in-a-word checks for a compiled doc, with ready-made warning lines. `env` may be None (no analysis audio): only
    the transcript test runs. Never edits anything."""
    ck = (pol or {}).get("checks") or {}
    pa = (pol or {}).get("pause") or {}
    stub = max(float(pa.get(k, 0)) for k in ("word_tail_pad_ms", "word_lead_pad_ms", "word_tail_pad_ghost_ms", "word_lead_pad_ghost_ms")) / 1000.0 + 0.02
    allh = check(doc, words, float(ck.get("word_margin_s", DEFAULT_MARGIN_S)), stub_max_s=stub)
    hits = [h for h in allh if h["kind"] == "sliced"]
    stubs = [h for h in allh if h["kind"] == "stub"]
    hot =(hot_edges(doc, words, env, float(ck.get("hot_db_below_speech", 12.0)), float(ck.get("hot_max_edge_pad_ms", 20.0)) / 1000.0)
           if env is not None else [])
    warns = format_hits(hits)
    for h in hot[:12]:
        warns.append(f"cut at a word edge with speech continuing: after \"{h['word'].strip()}\" the {h['side']} side of the splice at output {h['o']:.2f}s "
                     f"(episode {h['e']:.2f}s) is {h['db']:.0f} dB against typical speech {h['speech_db']:.0f} dB with {h['pad_s'] * 1000:.0f} ms of pad: "
                     "listen, the word may be clipped")
    if len(hot) > 12:
        warns.append(f"... and {len(hot) - 12} more cuts at a word edge with speech continuing")
    return {"inside_word": hits, "stubs": stubs, "hot_edges": hot, "audio_checked": env is not None, "warnings": warns}
