"""A long synthetic episode for recall-first clip tests: plain conversation with clip-worthy blocks planted at known times.

`build_marathon(root, blocks=40, minutes=110)` returns (episode, [planted block start times in seconds]). No media is needed:
only transcript.json and an assembly EditDoc, exactly like `clips_synth.build_long_episode`.
"""

from __future__ import annotations

from pathlib import Path

from ave import editdoc
from ave.episode import Episode, write_json

from clips_synth import GAP, WORD_LEN, WORD_STEP, sources

BLOCK = [
    "Have you ever wondered why most teams never finish what they start?",
    "I remember the day we shipped nothing after eighteen months of work.",
    "The problem is that nobody asked who was already paying for the answer.",
    "Finishing always beats starting, and that is the whole point of shipping.",
    "A small thing that is done is worth more than a big plan.",
]
PLAIN = [
    "The agenda for this part has a few small items on it.",
    "Please check the shared document before the next section starts.",
    "The next topic is about how the schedule changes during the winter.",
    "Each person can add notes to the document whenever they find time.",
    "We will return to the open questions once the group has looked.",
    "The group meets on the second floor near the east entrance.",
]


def build_marathon(root: Path, blocks: int = 40, minutes: float = 110.0) -> tuple[Episode, list[float]]:
    ep = Episode(root)
    ep.create(title="marathon")
    total = minutes * 60.0
    per = total / blocks
    words, utts, planted = [], [], []
    t, n_plain = 1.0, 0

    def add(text: str, spk: str) -> None:
        nonlocal t
        first = len(words)
        a = t
        for k, tok in enumerate(text.split()):
            s = t + k * WORD_STEP
            words.append({"w": tok, "start": round(s, 3), "end": round(s + WORD_LEN, 3), "conf": 0.95, "speaker": spk})
        t = t + (len(text.split()) - 1) * WORD_STEP + WORD_LEN
        utts.append({"speaker": spk, "start": a, "end": t, "first_word": first, "last_word": len(words) - 1, "text": text})
        t += GAP

    for b in range(blocks):
        block_start = 1.0 + b * per
        while t < block_start + per * 0.55:
            add(PLAIN[n_plain % len(PLAIN)], "S1" if (n_plain // 3) % 2 == 0 else "S2")
            n_plain += 1
        planted.append(round(t, 1))
        for k, text in enumerate(BLOCK):
            add(text, "S1" if k == 0 else "S2")
        while t < block_start + per:
            add(PLAIN[n_plain % len(PLAIN)], "S1" if (n_plain // 3) % 2 == 0 else "S2")
            n_plain += 1
    duration = t + 1.0
    write_json(ep.artifact("transcript.json"),
               {"source": "raw/camA.mp4", "engine": {"asr": "synthetic"}, "duration": duration,
                "speakers": {"S1": {"name": "Host"}, "S2": {"name": "Guest"}}, "words": words, "utterances": utts})
    doc = editdoc.new_doc("assembly", 30, (640, 360), sources("raw"), duration)
    doc["tracks"]["dialogue"].append({"id": "d1", "src": "A", "o0": 0.0, "o1": duration, "e0": 0.0, "xfade_ms": 0, "gain_db": 0.0, "reason": "keep"})
    doc["tracks"]["video"].append({"id": "v1", "src": "A", "o0": 0.0, "o1": duration, "e0": 0.0, "crop": None, "zoom": 1.0, "reason": "test", "confidence": 1.0})
    doc["tracks"]["room_tone"] = {"src": "A", "e0": 0.0, "e1": 0.9}
    editdoc.save_new_version(ep, doc)
    return ep, planted
