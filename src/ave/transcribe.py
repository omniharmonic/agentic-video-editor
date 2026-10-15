"""Transcription + speaker diarization -> work/transcript.json (+ transcript.md).

Engines
  asr:         fluidaudio (Parakeet TDT v3, CoreML, ~200x realtime)  |  whisper (whisper.cpp, Metal)
  diarization: fluidaudio (pyannote segmentation + WeSpeaker embeddings + VBx clustering)
               | channels (speaker-per-channel masters: the louder mic over each word is the speaker; field note N4)

Episode time = the timeline of the transcribed source file. `ave sync` maps every
other camera onto it. All times are seconds (float).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np

from . import config, media
from .audio import analysis_wav_path
from .episode import Episode, read_json, write_json

# A word is attached to a diarization segment if its midpoint is inside it, or within
# this many seconds of its edge (diarization boundaries are fuzzy; word times are not).
SNAP_SECONDS = 0.6
# New utterance when the speaker changes, or the same speaker pauses this long.
UTTERANCE_GAP = 1.0


def pick_source(ep: Episode, explicit: str | None) -> str:
    """Choose which file's audio to transcribe: explicit > dedicated audio file > longest video."""
    media = read_json(ep.artifact("media.json"))["files"]
    usable = [m for m in media if m["audio"]]
    if not usable:
        raise SystemExit("No file with an audio track to transcribe.")
    if explicit:
        match = [m for m in usable if m["file"] == explicit or Path(m["file"]).name == explicit]
        if not match:
            raise SystemExit(f"--source {explicit!r} not found. Options: {[m['file'] for m in usable]}")
        return match[0]["file"]
    audio_only = [m for m in usable if m["kind"] == "audio"]
    pool = audio_only or usable
    return max(pool, key=lambda m: m["duration"])["file"]


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr[-2000:]}")


def run_fluidaudio_asr(wav: Path, out_json: Path, language: str | None) -> list[dict]:
    cli = config.fluidaudio_cli()
    if not cli:
        raise SystemExit("FluidAudio not built. Run scripts/build-fluidaudio.sh (or use --asr whisper).")
    cmd = [str(cli), "transcribe", str(wav), "--word-timestamps", "--output-json", str(out_json)]
    if language:
        cmd += ["--language", language]
    _run(cmd)
    data = json.loads(out_json.read_text())
    return [
        {"w": w["word"], "start": float(w["startTime"]), "end": float(w["endTime"]), "conf": float(w.get("confidence", 1.0))}
        for w in data["wordTimings"]
    ]


def run_whisper_asr(wav: Path, out_prefix: Path, language: str | None) -> list[dict]:
    cli, model = config.whisper_cli(), config.whisper_model()
    if not (cli and model):
        raise SystemExit("whisper-cli or its model is missing. Run scripts/fetch-whisper-model.sh.")
    # -ml 1 + -sow => one word per segment, giving word-level timestamps.
    cmd = [str(cli), "-m", str(model), "-f", str(wav), "-oj", "-of", str(out_prefix), "-ml", "1", "-sow", "-np"]
    if language:
        cmd += ["-l", language]
    _run(cmd)
    data = json.loads(out_prefix.with_suffix(".json").read_text())
    words = []
    for seg in data.get("transcription", []):
        text = seg["text"].strip()
        if not text:
            continue
        words.append({
            "w": text,
            "start": seg["offsets"]["from"] / 1000.0,
            "end": seg["offsets"]["to"] / 1000.0,
            "conf": 1.0,  # whisper.cpp JSON does not expose per-word confidence by default
        })
    return words


def run_fluidaudio_diarization(wav: Path, out_json: Path, num_speakers: int | None) -> dict:
    cli = config.fluidaudio_cli()
    if not cli:
        raise SystemExit("FluidAudio not built. Run scripts/build-fluidaudio.sh")
    cmd = [str(cli), "process", str(wav), "--mode", "offline", "--output", str(out_json)]
    if num_speakers:
        cmd += ["--min-speakers", str(num_speakers), "--max-speakers", str(num_speakers)]
    _run(cmd)
    return json.loads(out_json.read_text())


def assign_speakers(words: list[dict], segments: list[dict]) -> None:
    """Attach `speaker` to every word in place. Unmatched words inherit the nearest neighbour."""
    segs = sorted(segments, key=lambda s: s["startTimeSeconds"])
    starts = np.array([s["startTimeSeconds"] for s in segs])
    ends = np.array([s["endTimeSeconds"] for s in segs])
    ids = [s["speakerId"] for s in segs]
    for w in words:
        mid = (w["start"] + w["end"]) / 2
        inside = np.where((starts <= mid) & (mid <= ends))[0]
        if len(inside):
            # Overlapping speech: prefer the segment whose center is closest to the word.
            best = min(inside, key=lambda i: abs((starts[i] + ends[i]) / 2 - mid))
            w["speaker"] = ids[best]
            continue
        gap = np.where(mid < starts, starts - mid, mid - ends)
        j = int(np.argmin(gap)) if len(gap) else -1
        w["speaker"] = ids[j] if j >= 0 and gap[j] <= SNAP_SECONDS else None
    # Fill remaining gaps from the previous attributed word, then the next.
    last = None
    for w in words:
        if w["speaker"] is None:
            w["speaker"] = last
        else:
            last = w["speaker"]
    nxt = None
    for w in reversed(words):
        if w["speaker"] is None:
            w["speaker"] = nxt
        else:
            nxt = w["speaker"]


# ---- speakers from channels (T11): two mics, one host each -------------------------------------------------------------------

CHANNEL_MARGIN_DB = 6.0                        # a word is labelled by a channel only if it is this much louder than the other
CHANNEL_SPEAKERS = {"left": "S1", "right": "S2"}   # fixed, so S1 means "the left mic" in every episode


def _span_db(env: np.ndarray, hop: float, a: float, b: float) -> float:
    """Mean-power level (dB) of a linear RMS envelope over [a, b] seconds (at least one frame)."""
    i, j = int(a / hop), max(int(np.ceil(b / hop)), int(a / hop) + 1)
    seg = env[i:j] if i < len(env) else env[-1:]
    return float(10 * np.log10(np.mean(np.asarray(seg, np.float64) ** 2) + 1e-18)) if len(seg) else -180.0


def label_span(env_l: np.ndarray, env_r: np.ndarray, hop: float, a: float, b: float,
               margin_db: float = CHANNEL_MARGIN_DB) -> tuple[str | None, float]:
    """(`left`|`right`|None, margin in dB) for a time span: the louder channel, None when they are within `margin_db`."""
    d = _span_db(env_l, hop, a, b) - _span_db(env_r, hop, a, b)
    return ("left" if d >= margin_db else "right" if d <= -margin_db else None), abs(d)


def _margin_conf(margin_db: float) -> float:
    """6 dB margin = 0.75, 12 dB or more = 1.0: how sure a channel verdict is."""
    return round(min(1.0, 0.5 + margin_db / 24.0), 3)


def label_words_by_channel(words: list[dict], env_l: np.ndarray, env_r: np.ndarray, hop: float,
                           fallback: list[str | None] | None = None, margin_db: float = CHANNEL_MARGIN_DB) -> None:
    """Set `speaker` (S1 = left mic, S2 = right mic), `speaker_margin_db` and `speaker_conf` on every word, in place.

    The louder channel over the word's span wins. A word within `margin_db` (overlap, silence, a breath) takes the
    diarizer's label when `fallback[i]` is given (already mapped to S1/S2), else the nearest labelled word in time."""
    labelled: list[int] = []
    for i, w in enumerate(words):
        side, m = label_span(env_l, env_r, hop, w["start"], w["end"], margin_db)
        if side:
            w["speaker"], w["speaker_margin_db"], w["speaker_conf"] = CHANNEL_SPEAKERS[side], round(m, 1), _margin_conf(m)
            labelled.append(i)
        else:
            w["speaker"], w["speaker_margin_db"], w["speaker_conf"] = None, None, 0.0
    for i, w in enumerate(words):
        if w["speaker"] is not None:
            continue
        if fallback and fallback[i]:
            w["speaker"], w["speaker_conf"] = fallback[i], 0.6
            continue
        mid = (w["start"] + w["end"]) / 2
        near = [(abs((words[j]["start"] + words[j]["end"]) / 2 - mid), j) for j in labelled]
        if near:
            w["speaker"], w["speaker_conf"] = words[min(near)[1]]["speaker"], 0.4
        else:
            w["speaker"], w["speaker_conf"] = "S1", 0.0


def diarization_agreement(env_l: np.ndarray, env_r: np.ndarray, hop: float, segments: list[dict],
                          margin_db: float = CHANNEL_MARGIN_DB) -> dict:
    """How well the diarizer's speaker ids match the channels, over the given segments (the caller filters by length).

    Each segment with a clear channel verdict votes for (diarizer id -> channel); each id maps to its majority channel;
    `agreement` = segments whose own channel equals their id's mapped channel / clear segments."""
    clear = []
    for s in segments:
        side, m = label_span(env_l, env_r, hop, s["startTimeSeconds"], s["endTimeSeconds"], margin_db)
        if side:
            clear.append((s["speakerId"], side))
    votes: dict[str, dict[str, int]] = {}
    for sid, side in clear:
        votes.setdefault(sid, {}).setdefault(side, 0)
        votes[sid][side] += 1
    chan_of = {sid: max(v, key=v.get) for sid, v in votes.items()}
    agree = sum(1 for sid, side in clear if chan_of[sid] == side)
    return {"segments": len(segments), "clear": len(clear), "agree": agree,
            "agreement": round(agree / len(clear), 4) if clear else None,
            "mapping": {sid: CHANNEL_SPEAKERS[c] for sid, c in chan_of.items()},
            "per_speaker": {sid: dict(v) for sid, v in votes.items()}}


def build_utterances(words: list[dict]) -> list[dict]:
    utts: list[dict] = []
    for i, w in enumerate(words):
        cur = utts[-1] if utts else None
        if cur and cur["speaker"] == w["speaker"] and w["start"] - cur["end"] <= UTTERANCE_GAP:
            cur["end"] = w["end"]
            cur["last_word"] = i
            cur["text"] += " " + w["w"]
        else:
            utts.append({"speaker": w["speaker"], "start": w["start"], "end": w["end"],
                         "first_word": i, "last_word": i, "text": w["w"]})
    for u in utts:     # channel-derived words carry a per-word confidence; the utterance's is their mean
        cs = [words[k]["speaker_conf"] for k in range(u["first_word"], u["last_word"] + 1) if "speaker_conf" in words[k]]
        if cs:
            u["confidence"] = round(sum(cs) / len(cs), 3)
    return utts


def speaker_centroids(segments: list[dict]) -> dict[str, dict]:
    """Duration-weighted mean voice embedding per speaker. Stored for cross-episode voice ID."""
    by: dict[str, list] = {}
    for s in segments:
        emb = s.get("embedding")
        if emb:
            by.setdefault(s["speakerId"], []).append((s["endTimeSeconds"] - s["startTimeSeconds"], np.array(emb)))
    out = {}
    for spk, items in by.items():
        weights = np.array([d for d, _ in items])
        mean = sum(d * e for d, e in items) / weights.sum()
        out[spk] = {"seconds": float(weights.sum()), "embedding": (mean / (np.linalg.norm(mean) or 1)).tolist()}
    return out


def fmt_ts(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:05.2f}"


def _source_item(ep: Episode, rel: str) -> dict:
    return next(m for m in read_json(ep.artifact("media.json"))["files"] if m["file"] == rel)


def transcribe(ep: Episode, source: str | None = None, asr: str = "fluidaudio",
               language: str | None = None, num_speakers: int | None = None, diarize: bool = True,
               speakers_from: str = "auto", check_diarization: bool = False) -> dict:
    """ASR + speakers -> work/transcript.json.

    speakers_from: `channels` labels each word by the louder mic (needs a `speaker_per_channel` source), `diarize` runs the
    diarizer, `auto` = channels when the source's layout allows, else diarize (unless diarize=False). `check_diarization`
    also runs the diarizer in channels mode and records how well it agrees (`diarization_check`)."""
    if speakers_from not in ("auto", "channels", "diarize"):
        raise SystemExit(f"--speakers-from must be auto, channels or diarize (got {speakers_from!r})")
    rel = pick_source(ep, source)
    item = _source_item(ep, rel)
    layout = item.get("channel_layout")
    if speakers_from == "channels" and layout != "speaker_per_channel":
        raise SystemExit(f"--speakers-from channels needs a speaker_per_channel source, but {rel} is {layout or 'unclassified'}. "
                         "Run `ave ingest` (classifies the layout from the whole file) or use --speakers-from diarize.")
    use_channels = speakers_from == "channels" or (speakers_from == "auto" and layout == "speaker_per_channel")
    wav = analysis_wav_path(ep, rel)
    if not wav.exists():
        raise SystemExit(f"Analysis audio missing: {wav}. Run: ave audio {ep.root}")
    asr_dir = ep.work / "asr"
    asr_dir.mkdir(exist_ok=True)

    words = (run_fluidaudio_asr(wav, asr_dir / "fluidaudio_asr.json", language) if asr == "fluidaudio"
             else run_whisper_asr(wav, asr_dir / "whisper", language))
    if not words:
        raise SystemExit("ASR returned no words. Is there speech in this audio?")

    segments: list[dict] = []
    check = None
    if use_channels:
        env_l, env_r = media.channel_envelopes(ep.root / rel, 0)
        fallback = None
        if check_diarization:
            segments = run_fluidaudio_diarization(wav, asr_dir / "fluidaudio_diar.json", num_speakers)["segments"]
            check = diarization_agreement(env_l, env_r, media.ENVELOPE_HOP_S, segments)
            probe = [dict(w) for w in words]
            assign_speakers(probe, segments)
            fallback = [check["mapping"].get(w["speaker"]) for w in probe]
        label_words_by_channel(words, env_l, env_r, media.ENVELOPE_HOP_S, fallback)
        engine_diar = "channels"
    elif diarize:
        diar = run_fluidaudio_diarization(wav, asr_dir / "fluidaudio_diar.json", num_speakers)
        segments = diar["segments"]
        assign_speakers(words, segments)
        engine_diar = "fluidaudio"
    else:
        for w in words:
            w["speaker"] = "S1"
        engine_diar = None

    utts = build_utterances(words)
    totals: dict[str, float] = {}
    for u in utts:
        totals[u["speaker"]] = totals.get(u["speaker"], 0.0) + (u["end"] - u["start"])
    speakers = {k: {"talk_seconds": round(v, 2), "name": None} for k, v in sorted(totals.items())}
    if use_channels:
        for k, v in speakers.items():
            v["channel"] = next(c for c, sid in CHANNEL_SPEAKERS.items() if sid == k)
    result = {
        "source": rel,
        "engine": {"asr": asr, "diarization": engine_diar},
        "duration": item["duration"],
        "speakers": speakers,
        "words": words,
        "utterances": utts,
    }
    if check:
        result["diarization_check"] = check
    write_json(ep.artifact("transcript.json"), result)
    if segments:       # embeddings are only available when the diarizer ran (optional in channels mode)
        write_json(ep.artifact("speakers.json"), speaker_centroids(segments))
    lines = [f"[{fmt_ts(u['start'])}] {u['speaker']}: {u['text']}" for u in utts]
    ep.artifact("transcript.md").write_text("\n".join(lines) + "\n")
    ep.mark_stage("transcribe", source=rel, asr=asr, speakers=len(totals), words=len(words))
    return result
