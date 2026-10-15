"""Hand-built transcripts for the clip-completeness tests: each line is (speaker, text) in the ASR's own messy style, modelled on a
real podcast transcript but written from scratch (lowercase continuations, "And ..." chains, chunks that stop on "and I"). `make_episode` writes
transcript.json and a one-segment assembly EditDoc; nothing needs media."""

from __future__ import annotations

from pathlib import Path

from ave import editdoc
from ave.episode import Episode, write_json

from clips_synth import WORD_LEN, WORD_STEP, sources

GAP = 0.45

# (speaker, text). Synthetic wording in the shape of real ASR output; ids below are for the tests.
REAL = [
    ("S1", "Welcome."),                                                                              # 0
    ("S1", "That's not gonna be how it starts."),                                                    # 1  opens on "That's": needs context
    ("S1", "The funny thing about keeping a garden is that I feel like it never really ends."),   # 2
    ("S1", "I think there was something underneath the noise, and I set out to find what that was, and I"),  # 3  ends on "and I"
    ("S2", "mm-hmm."),                                                                               # 4  listener interjection
    ("S1", "continued to follow the trail while simultaneously feeling like there was something missing from the map."),  # 5  lowercase continuation
    ("S1", "And there's things that happen in the kitchen that the guests are never shown."),   # 6  opener
    ("S1", "So no further amount of counting the trees one by one is going to help us see the forest."),  # 7  opener
    ("S2", "Have you tried feeling in your body what might be going on?"),                           # 8  clean start
    ("S1", "You actually can't separate yourself from everything that's happening."),                        # 9  clean start
    ("S1", "I would say the problem is that we cut the cloth into pieces and then wonder why it does not fit in the"),  # 10 ends on "in the"
    ("S2", "Right."),                                                                                # 11
]


def make_episode(root: Path, lines: list[tuple[str, str]], gaps: dict[int, float] | None = None, stage: str = "assembly") -> Episode:
    """Episode whose transcript is `lines` (one entry per ASR chunk). `gaps[k]` overrides the pause before line k (default 0.45 s)."""
    ep = Episode(root)
    ep.create(title="completeness synthetic")
    raw = ep.rel(ep.raw)
    words, utts, t = [], [], 1.0
    for k, (spk, text) in enumerate(lines):
        t += (gaps or {}).get(k, GAP) if k else 0.0
        first = len(words)
        a = t
        for n, tok in enumerate(text.split()):
            s = t + n * WORD_STEP
            words.append({"w": tok, "start": round(s, 3), "end": round(s + WORD_LEN, 3), "conf": 0.95, "speaker": spk})
        t += (len(text.split()) - 1) * WORD_STEP + WORD_LEN
        utts.append({"speaker": spk, "start": a, "end": t, "first_word": first, "last_word": len(words) - 1, "text": text})
    duration = t + 1.0
    write_json(ep.artifact("transcript.json"),
               {"source": f"{raw}/camA.mp4", "engine": {"asr": "synthetic"}, "duration": duration,
                "speakers": {"S1": {"name": "Host"}, "S2": {"name": "Guest"}}, "words": words, "utterances": utts})
    doc = editdoc.new_doc(stage, 30, (640, 360), sources(raw), duration)
    doc["tracks"]["dialogue"].append({"id": "d1", "src": "A", "o0": 0.0, "o1": duration, "e0": 0.0, "xfade_ms": 0, "gain_db": 0.0, "reason": "keep"})
    doc["tracks"]["video"].append({"id": "v1", "src": "A", "o0": 0.0, "o1": duration, "e0": 0.0, "crop": None, "zoom": 1.0, "reason": "test", "confidence": 1.0})
    doc["tracks"]["room_tone"] = {"src": "A", "e0": 0.0, "e1": 0.9}
    editdoc.save_new_version(ep, doc)
    return ep


def episode_from_words(root: Path, words: list[dict], stage: str = "assembly") -> Episode:
    """Episode over an explicit word list ({w,start,end,speaker}); used with the real-transcript excerpt in data_assets."""
    ep = Episode(root)
    ep.create(title="real excerpt")
    raw = ep.rel(ep.raw)
    duration = words[-1]["end"] + 1.0
    write_json(ep.artifact("transcript.json"),
               {"source": f"{raw}/camA.mp4", "engine": {"asr": "excerpt"}, "duration": duration,
                "speakers": {"S1": {"name": "Host"}, "S2": {"name": "Guest"}}, "words": words, "utterances": []})
    doc = editdoc.new_doc(stage, 30, (640, 360), sources(raw), duration)
    doc["tracks"]["dialogue"].append({"id": "d1", "src": "A", "o0": 0.0, "o1": duration, "e0": 0.0, "xfade_ms": 0, "gain_db": 0.0, "reason": "keep"})
    doc["tracks"]["video"].append({"id": "v1", "src": "A", "o0": 0.0, "o1": duration, "e0": 0.0, "crop": None, "zoom": 1.0, "reason": "test", "confidence": 1.0})
    doc["tracks"]["room_tone"] = {"src": "A", "e0": 0.0, "e1": 0.9}
    editdoc.save_new_version(ep, doc)
    return ep
