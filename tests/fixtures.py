"""Test fixtures. The two-camera generator lives in src/ave/synth.py (shared with `ave demo`); multi-session lives here."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from ave.episode import Episode, write_json
from ave.synth import (B_OFFSET, DEAD_AIR, DURATION, FILLERS, FPS, M_OFFSET, SIZE, UTTERANCES, _audio_expr, _run,  # noqa: F401
                       build_media, transcript, words)


def build_episode(root: Path, upto: str = "map", layout: str | dict = "flat") -> Episode:
    """Create an episode folder with media and artifacts up to `upto` ('media'|'map'|'cutplan'|'assembly').

    `layout` is "flat" (legacy; no layout key is written, exactly like old episodes) or "numbered" (cameras in 02_FOOTAGE,
    the recorder in 03_AUDIO) or an explicit dict. All stored paths are relative to the episode root."""
    ep = Episode(root)
    ep.create(title="synthetic", layout=None if layout == "flat" else layout)
    audio_dir = ep.raw_dirs[1] if len(ep.raw_dirs) > 1 else None
    build_media(ep.raw, audio_dir)
    a, b = ep.rel(ep.raw / "camA.mp4"), ep.rel(ep.raw / "camB.mp4")
    rec = ep.rel((audio_dir or ep.raw) / "recorder.wav")
    from ave import audio, media
    media.ingest(ep)
    audio.extract_all(ep)
    write_json(ep.artifact("transcript.json"), transcript(a))
    write_json(ep.artifact("sync.json"), {"reference": a, "warnings": [], "files": {
        a: {"start_offset": 0.0, "psr": None, "drift_ms": 0.0},
        b: {"start_offset": B_OFFSET, "psr": 999.0, "drift_ms": 0.0},
        rec: {"start_offset": M_OFFSET, "psr": 999.0, "drift_ms": 0.0}}})
    if upto == "media":
        return ep
    meta = json.loads(ep.meta_path.read_text())
    meta["cameras"] = {"A": {"file": a, "role": "host"}, "B": {"file": b, "role": "guest"}}
    meta["speakers"] = {"S1": {"name": "Host", "camera": "A"}, "S2": {"name": "Guest", "camera": "B"}}
    meta["audio_source"] = a
    write_json(ep.meta_path, meta)
    if upto == "map":
        return ep
    from ave import cutplan
    cutplan.build_cutlist(ep)
    if upto == "cutplan":
        return ep
    from ave import assemble, editdoc
    editdoc.save_new_version(ep, assemble.build(ep, (640, 360)))
    return ep


# ======================================================================================================================
# MULTI-SESSION episode (field notes N1-N4): 2 sessions x 2 cameras + ONE recorder file spanning both, small and fast.
#
# Episode time = the recorder's timeline (195 s). Cameras start late and keep SLOW clocks (30-80 ppm), the recorder is stereo with
# ONE live channel per session (left dead until 97 s, right dead after), and 8 s of dead space sits between the sessions:
#
#   0-3 lead-in (chatter, no camera) | session 1 3-93 | 93-111 dead space (chatter at 94.5-97, no camera) | session 2 111-191 | 191-195 tail
#
# Camera truth: file t=0 sits at `offset` on the recorder timeline and the camera clock is `ppm` slow (e = offset + (1+ppm*1e-6)*t).
# ======================================================================================================================

MS_SR = 16000
MS_FPS = 30
MS_TOTAL = 195.0
MS_SWITCH = 97.0                 # recorder channel switch (left dead before, right dead after), inside the dead space
MS_CAMS = {
    #  key: (session, role,    colour,   offset, ppm, file seconds, creation_time)
    "A1": ("s1", "host",  "0x2040ff", 3.0, 80.0, 90.0, "2026-10-12T22:07:45Z"),
    "B1": ("s1", "guest", "0xff2020", 3.6, 30.0, 89.0, "2026-10-12T22:07:46Z"),
    "A2": ("s2", "host",  "0x2040ff", 111.0, 70.0, 80.0, "2026-10-13T00:32:47Z"),
    "B2": ("s2", "guest", "0xff2020", 111.7, 50.0, 79.0, "2026-10-13T00:32:48Z"),
}
# (speaker, start, end) on the recorder timeline. Lead-in and dead-space chatter has no camera under it.
MS_UTTS = [
    ("S1", 0.5, 2.5), ("S1", 5.0, 14.0), ("S2", 14.5, 23.0), ("S1", 24.0, 33.0), ("S2", 33.5, 45.0), ("S1", 46.0, 55.0),
    ("S2", 55.5, 66.0), ("S1", 69.0, 78.0), ("S2", 78.5, 92.0), ("S2", 94.5, 97.0),
    ("S1", 113.0, 125.0), ("S2", 125.5, 140.0), ("S1", 141.0, 155.0), ("S2", 155.5, 170.0), ("S1", 171.0, 188.0),
]
MS_VOCAB = "we build tools that help people tell the truth about what they see and make things that last for the whole community".split()


def ms_truth() -> dict[str, tuple[float, float]]:
    """camera key -> (offset seconds, drift ppm)"""
    return {k: (v[3], v[4]) for k, v in MS_CAMS.items()}


def _ms_speech(rng) -> "np.ndarray":
    """Mono speech-like signal on the recorder timeline: band-limited noise syllables inside each utterance, silence elsewhere."""
    import numpy as np
    from scipy.signal import butter, lfilter
    x = np.zeros(int(MS_TOTAL * MS_SR), np.float32)
    for _, a, b in MS_UTTS:
        pos, end = int(a * MS_SR), int(b * MS_SR)
        while pos < end:
            dur = int(rng.uniform(0.08, 0.4) * MS_SR)
            gap = int(rng.uniform(0.04, 0.25) * MS_SR)
            burst = rng.standard_normal(min(dur, end - pos)).astype(np.float32)
            bb, aa = butter(2, [rng.uniform(150, 400), rng.uniform(1500, 3500)], btype="band", fs=MS_SR)
            x[pos:pos + len(burst)] = lfilter(bb, aa, burst) * rng.uniform(0.3, 1.0)
            pos += dur + gap
    return x / np.abs(x).max() * 0.5


def _ms_warp(x, offset: float, ppm: float, seconds: float) -> "np.ndarray":
    """What a camera that starts at `offset` and keeps `ppm`-slow time records of the master signal `x`: file sample n is master
    time offset + (1+b)*n/sr."""
    import numpy as np
    n = int(seconds * MS_SR)
    pos = offset * MS_SR + (1.0 + ppm * 1e-6) * np.arange(n)
    return np.interp(pos, np.arange(len(x)), x).astype(np.float32)


def ms_transcript(source: str) -> dict:
    ws, utts = [], []
    for spk, a, b in MS_UTTS:
        n = max(2, int((b - a) * 2.4))
        toks = [MS_VOCAB[(i * 7 + int(a)) % len(MS_VOCAB)] for i in range(n)]
        if spk == "S2" and a == 33.5:
            toks[3] = "um"                      # one filler for the cleanup flow
        first = len(ws)
        step = (b - a) / n
        for i, t in enumerate(toks):
            ws.append({"w": t, "start": round(a + i * step, 3), "end": round(a + (i + 1) * step - 0.05, 3), "conf": 0.95, "speaker": spk})
        utts.append({"speaker": spk, "start": a, "end": b, "first_word": first, "last_word": len(ws) - 1, "text": " ".join(toks)})
    return {"source": source, "engine": {"asr": "synthetic", "diarization": "synthetic"}, "duration": MS_TOTAL,
            "speakers": {"S1": {"talk_seconds": 40.0, "name": "Host"}, "S2": {"talk_seconds": 50.0, "name": "Guest"}},
            "words": ws, "utterances": utts}


def build_multisession(root: Path, upto: str = "map", layout: str | dict = "flat") -> Episode:
    """Create the multi-session episode up to 'media' (ingested + analysis audio), 'sync' (+ real sync.json + transcript) or 'map'
    (+ cameras/speakers in episode.json). Sync is the REAL algorithm run on the synthetic audio."""
    import numpy as np
    import soundfile as sf
    from scipy.signal import butter, lfilter
    ep = Episode(root)
    ep.create(title="multisession", layout=None if layout == "flat" else layout)
    audio_dir = ep.raw_dirs[1] if len(ep.raw_dirs) > 1 else ep.raw
    rng = np.random.default_rng(11)
    speech = _ms_speech(rng)
    floor = lambda n: (rng.standard_normal(n) * 0.003).astype(np.float32)  # noqa: E731
    sw = int(MS_SWITCH * MS_SR)
    left, right = speech.copy(), speech.copy()
    left[:sw] *= 0.01
    right[sw:] *= 0.01
    stereo = np.stack([left + floor(len(speech)), right + floor(len(speech))], axis=1)
    audio_dir.mkdir(parents=True, exist_ok=True)
    sf.write(audio_dir / "recorder.wav", stereo, MS_SR, subtype="PCM_16")
    lp, la = butter(2, 2500, btype="low", fs=MS_SR)
    for key, (_, _, color, off, ppm, secs, ct) in MS_CAMS.items():
        mic = lfilter(lp, la, _ms_warp(speech, off, ppm, secs)) * 0.7 + floor(int(secs * MS_SR))
        wav = ep.raw / f"_{key}.wav"
        sf.write(wav, mic.astype(np.float32), MS_SR, subtype="PCM_16")
        _run("-f", "lavfi", "-i", f"color=c={color}:s=64x36:r={MS_FPS}:d={secs}", "-i", str(wav), "-metadata", f"creation_time={ct}",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", "-shortest", str(ep.raw / f"cam{key}.mov"))
        wav.unlink()
    from ave import audio, media, sync
    media.ingest(ep)
    audio.extract_all(ep)
    rec = ep.rel(audio_dir / "recorder.wav")
    write_json(ep.artifact("transcript.json"), ms_transcript(rec))
    if upto == "media":
        return ep
    sync.sync_episode(ep, points=5)
    if upto == "sync":
        return ep
    meta = json.loads(ep.meta_path.read_text())
    meta["cameras"] = {k: {"file": ep.rel(ep.raw / f"cam{k}.mov"), "role": v[1], "session": v[0]} for k, v in MS_CAMS.items()}
    meta["speakers"] = {"S1": {"name": "Host", "camera": "A1", "cameras": {"s1": "A1", "s2": "A2"}},
                        "S2": {"name": "Guest", "camera": "B1", "cameras": {"s1": "B1", "s2": "B2"}}}
    meta["audio_source"] = rec
    write_json(ep.meta_path, meta)
    return ep
