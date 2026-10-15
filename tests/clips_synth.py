"""A longer, punctuated synthetic transcript (about 2.5 minutes) for the clips tests. No media is needed for the
logic tests: only transcript.json and EditDocs. Sentence k of SENTENCES starts at the time returned by `layout()`."""

from __future__ import annotations

from pathlib import Path

from ave import editdoc
from ave.episode import Episode, write_json

SENTENCES = [
    ("S1", "Welcome back to the show everyone."),
    ("S1", "Today we are digging into why most projects quietly die before anyone notices."),
    ("S2", "Thanks for having me, I have been thinking about this for years."),
    ("S2", "Um, so the thing I wanted to say is that systems beat goals every single time."),
    ("S1", "Have you ever wondered why that is?"),
    ("S2", "Because a goal is a wish and a system is a habit that keeps running when you are tired."),
    ("S2", "And that is why the first week matters more than the last month."),
    ("S1", "That is exactly what I meant earlier about momentum."),
    ("S1", "Tell me about the worst failure you had."),
    ("S2", "We spent eighteen months building a product nobody had asked for."),
    ("S2", "I remember the day we shipped it, the silence was louder than any applause."),
    ("S2", "Nobody clicked, nobody wrote, nobody cared."),
    ("S1", "That sounds brutal."),
    ("S2", "It was, but it taught me to ask one question before writing a line of code."),
    ("S2", "Who is already paying someone to solve this problem?"),
    ("S1", "I love that, it flips the whole approach."),
    ("S2", "If nobody pays today, you are guessing, and guessing is expensive."),
    ("S1", "So what do you do on day one?"),
    ("S2", "You call ten people and you listen more than you talk."),
    ("S2", "By the fifth call you hear the same sentence repeated, and that sentence is your product."),
    ("S1", "Does that work for creative work too?"),
    ("S2", "Absolutely, a song or a book is a product when someone is waiting for it."),
    ("S2", "The waiting is the signal, and most people ignore it."),
    ("S1", "We should wrap up soon, but one last thing."),
    ("S1", "What would you tell someone starting today?"),
    ("S2", "Start smaller than feels respectable, and finish before you feel ready."),
    ("S2", "Finishing is a skill, and it compounds faster than talent ever will."),
    ("S1", "Thank you, that was a great conversation."),
    ("S2", "Thanks, this was fun."),
]
WORD_STEP = 0.30       # seconds per word start
WORD_LEN = 0.25
GAP = 0.45             # pause between sentences
START = 1.0


def layout() -> list[tuple[str, str, float, float]]:
    """[(speaker, text, start, end)] in episode time."""
    t = START
    out = []
    for spk, text in SENTENCES:
        n = len(text.split())
        end = t + (n - 1) * WORD_STEP + WORD_LEN
        out.append((spk, text, round(t, 3), round(end, 3)))
        t = end + GAP
    return out


def transcript(cam_a: str = "raw/camA.mp4") -> dict:
    words, utts = [], []
    for spk, text, a, b in layout():
        first = len(words)
        for k, tok in enumerate(text.split()):
            s = a + k * WORD_STEP
            words.append({"w": tok, "start": round(s, 3), "end": round(s + WORD_LEN, 3), "conf": 0.95, "speaker": spk})
        utts.append({"speaker": spk, "start": a, "end": b, "first_word": first, "last_word": len(words) - 1, "text": text})
    return {"source": cam_a, "engine": {"asr": "synthetic"}, "duration": layout()[-1][3] + 1.0,
            "speakers": {"S1": {"name": "Host"}, "S2": {"name": "Guest"}}, "words": words, "utterances": utts}


def sources(raw: str = "raw") -> dict:
    return {"A": {"file": f"{raw}/camA.mp4", "kind": "video", "start_offset": 0.0, "role": "host"},
            "B": {"file": f"{raw}/camB.mp4", "kind": "video", "start_offset": 1.0, "role": "guest"}}


def build_long_episode(root: Path, remove: tuple[float, float] | None = None, stage: str = "assembly", layout: str = "flat") -> Episode:
    """Episode with the long transcript and one EditDoc. `remove=(e0, e1)` cuts that episode range out (output time shifts)."""
    ep = Episode(root)
    ep.create(title="long synthetic", layout=None if layout == "flat" else layout)
    raw = ep.rel(ep.raw)
    tr = transcript(f"{raw}/camA.mp4")
    write_json(ep.artifact("transcript.json"), tr)
    dur = tr["duration"]
    keeps = [(0.0, dur)] if not remove else [(0.0, remove[0]), (remove[1], dur)]
    doc = editdoc.new_doc(stage, 30, (640, 360), sources(raw), sum(b - a for a, b in keeps))
    o = 0.0
    for n, (a, b) in enumerate(keeps):
        doc["tracks"]["dialogue"].append({"id": f"d{n + 1}", "src": "A", "o0": o, "o1": o + b - a, "e0": a, "xfade_ms": 0, "gain_db": 0.0, "reason": "keep"})
        mid = a + (b - a) / 2
        for k, (x, y, cam) in enumerate(((a, mid, "A"), (mid, b, "B"))):
            doc["tracks"]["video"].append({"id": f"v{n + 1}{k}", "src": cam, "o0": o + (x - a), "o1": o + (y - a), "e0": x,
                                           "crop": None, "zoom": 1.0, "reason": "test", "confidence": 1.0})
        o += b - a
    doc["tracks"]["room_tone"] = {"src": "A", "e0": 0.0, "e1": 0.9}
    editdoc.save_new_version(ep, doc)
    return ep


def pick(first: int, last: int, title: str = "A clip", **extra) -> dict:
    """A well-formed pick over sentences s{first}..s{last}."""
    p = {"title": title, "hook": "Opens on a strong claim.", "sentence_ids": [f"s{k:03d}" for k in range(first, last + 1)],
         "scores": {"hook": 8, "arc": 7, "insight": 7, "emotion": 6, "shareability": 7, "ending": 7, "speech_quality": 8},
         "rationale": "Complete idea with a payoff."}
    p.update(extra)
    return p
