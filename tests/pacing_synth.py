"""Synthetic LONG-MONOLOGUE episode for the pacing pass (T12): speaker S1 talks for ~50 s without a break (one 50 s hold in the
assembly), then S2 answers. Optionally a third, wide camera (role `wide`). Small (320x180, 70 s) and fast to build.

Layout (episode seconds): S1 sentences of 6 words (3.0 s each, 0.3 s apart) from 0.5 to 49.7 | S2 from 50.2 to 66.4 (a 0.55 s gap: below pause.max_ms, so cleanup cuts nothing and output time == episode time).
Camera A (blue) frames S1, B (red) frames S2, W (green, optional) is the wide. All three carry the same audio; sync offsets are 0.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ave.episode import Episode, write_json

DURATION = 70.0
FPS = 30
SIZE = "320x180"
S1_SENTENCES = 15
S2_SENTENCES = 5


def sentences() -> list[tuple[str, float, float, str]]:
    out = []
    for k in range(S1_SENTENCES):
        t = 0.5 + 3.3 * k
        out.append(("S1", t, t + 3.0, " ".join(f"alpha{k}x{j}" for j in range(6))))
    for k in range(S2_SENTENCES):
        t = 50.2 + 3.3 * k
        out.append(("S2", t, t + 3.0, " ".join(f"beta{k}x{j}" for j in range(6))))
    return out


def words() -> list[dict]:
    out = []
    for spk, a, b, text in sentences():
        toks = text.split()
        for j, tok in enumerate(toks):
            out.append({"w": tok + ("." if j == len(toks) - 1 else ""), "start": round(a + 0.5 * j, 3), "end": round(a + 0.5 * j + 0.45, 3),
                        "conf": 0.95, "speaker": spk, "speaker_margin_db": 20.0, "speaker_conf": 1.0})
    return out


def transcript(source: str) -> dict:
    ws = words()
    utts = []
    for spk, a, b, text in sentences():
        idx = [i for i, w in enumerate(ws) if w["speaker"] == spk and a - 1e-6 <= w["start"] < b]
        utts.append({"speaker": spk, "start": a, "end": b - 0.05, "first_word": idx[0], "last_word": idx[-1],
                     "text": text, "confidence": 1.0})
    return {"source": source, "engine": {"asr": "synthetic", "diarization": "synthetic"}, "duration": DURATION,
            "speakers": {"S1": {"talk_seconds": 50.0, "name": "Host"}, "S2": {"talk_seconds": 17.0, "name": "Guest"}},
            "words": ws, "utterances": utts}


def _run(*cmd: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *cmd], check=True)


def _audio_expr() -> str:
    parts = []
    for spk, a, b, _ in sentences():
        parts.append(f"0.4*sin(2*PI*{220 if spk == 'S1' else 330}*t)*between(t,{a},{b - 0.05})")
    return "+".join(parts)


def build_media(raw: Path, wide: bool) -> None:
    raw.mkdir(parents=True, exist_ok=True)
    wav = raw / "_base.wav"
    _run("-f", "lavfi", "-i", f"aevalsrc='{_audio_expr()}':s=16000:d={DURATION}", "-ac", "1", str(wav))
    noise = ["-f", "lavfi", "-i", f"anoisesrc=color=pink:amplitude=0.004:sample_rate=16000:d={DURATION}"]
    cams = [("camA", "0x2040ff"), ("camB", "0xff2020")] + ([("camW", "0x20c040")] if wide else [])
    for name, color in cams:
        _run("-f", "lavfi", "-i", f"color=c={color}:s={SIZE}:r={FPS}:d={DURATION}", "-i", str(wav), *noise,
             "-filter_complex", "[1:a][2:a]amix=inputs=2:duration=first:normalize=0[a]", "-map", "0:v", "-map", "[a]",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-shortest", str(raw / f"{name}.mp4"))
    wav.unlink()


def build_episode(root: Path, wide: bool = False) -> Episode:
    """An episode at stage 'assembly' (cut list planned, assembly.v1 saved)."""
    ep = Episode(root)
    ep.create(title="monologue")
    build_media(ep.raw, wide)
    from ave import assemble, audio, cutplan, editdoc, media
    media.ingest(ep)
    audio.extract_all(ep)
    a, b = ep.rel(ep.raw / "camA.mp4"), ep.rel(ep.raw / "camB.mp4")
    write_json(ep.artifact("transcript.json"), transcript(a))
    files = {a: {"start_offset": 0.0, "psr": None, "drift_ms": 0.0}, b: {"start_offset": 0.0, "psr": 999.0, "drift_ms": 0.0}}
    meta = json.loads(ep.meta_path.read_text())
    meta["cameras"] = {"A": {"file": a, "role": "host"}, "B": {"file": b, "role": "guest"}}
    if wide:
        w = ep.rel(ep.raw / "camW.mp4")
        files[w] = {"start_offset": 0.0, "psr": 999.0, "drift_ms": 0.0}
        meta["cameras"]["W"] = {"file": w, "role": "wide"}
    write_json(ep.artifact("sync.json"), {"reference": a, "warnings": [], "files": files})
    meta["speakers"] = {"S1": {"name": "Host", "camera": "A"}, "S2": {"name": "Guest", "camera": "B"}}
    meta["audio_source"] = a
    write_json(ep.meta_path, meta)
    cutplan.build_cutlist(ep)
    editdoc.save_new_version(ep, assemble.build(ep, (320, 180)))
    return ep
