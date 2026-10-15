"""T11 (field note N4, corrected): a stereo master can be speaker-per-channel (two independent mics, one host each).
Classify the layout from the WHOLE file, SUM instead of gating, and read speakers off the channels."""
import json
import os
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from ave import audio, editdoc, media, state, transcribe
from ave import compile as comp
from ave.episode import Episode, read_json

from conftest import needs_ffmpeg

SR = 8000
BLEED = 10 ** (-26 / 20)        # the other host's voice in a mic, 26 dB down
TURNS = [("A", 0, 6), ("B", 6, 12), ("A", 12, 18), ("B", 18, 24), ("A", 24, 30), ("B", 30, 36)]


def voice(seconds, seed, level=0.2):
    """Speech-like bursts (on/off at ~1.3 Hz, pauses well under 1 s) of independent noise."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    env = (np.sin(2 * np.pi * np.arange(n) / SR * 1.3) > -0.3).astype(np.float32)
    return (rng.standard_normal(n) * level * env).astype(np.float32)


def two_mics(turns=TURNS, total=36, bleed=BLEED, level=0.2):
    """A on LEFT, B on RIGHT, independent voices, each leaking `bleed` into the other mic. Returns (stereo, a, b)."""
    a, b = np.zeros(total * SR, np.float32), np.zeros(total * SR, np.float32)
    va, vb = voice(total, 1, level), voice(total, 2, level)
    for who, t0, t1 in turns:
        sl = slice(int(t0 * SR), int(t1 * SR))
        (a if who == "A" else b)[sl] = (va if who == "A" else vb)[sl]
    return np.stack([a + bleed * b, b + bleed * a], axis=1), a, b


def scan(tmp_path, x, win=0.5):
    p = tmp_path / "t.wav"
    sf.write(p, x, SR, subtype="FLOAT")
    levels, corr = media.channel_scan(p, 0, win)
    return media.classify_layout(levels, corr, win, len(x) / SR)


# ---- 1. layout classification ---------------------------------------------------------------------------------------

@needs_ffmpeg
def test_speaker_per_channel_is_recognised_with_a_single_sum_range(tmp_path):
    x, _, _ = two_mics()
    r = scan(tmp_path, x)
    assert r["channel_layout"] == "speaker_per_channel"
    assert [(m["start"], m["end"], m["use"]) for m in r["channel_map"]] == [(0.0, 36.0, "sum")]
    ev = r["evidence"]
    assert abs(ev["correlation"]) < 0.3
    assert ev["left_dominant_share"] >= 0.1 and ev["right_dominant_share"] >= 0.1 and ev["alternations"] >= 4


@needs_ffmpeg
def test_one_dead_mic_for_the_whole_file_is_dead_channel_not_speaker_per_channel(tmp_path):
    rng = np.random.default_rng(5)
    left = voice(36, 3)
    x = np.stack([left, (rng.standard_normal(36 * SR) * 0.0005).astype(np.float32)], axis=1)   # R: independent hiss, ~50 dB down
    r = scan(tmp_path, x)
    assert r["channel_layout"] == "dead_channel"
    assert r["channel_map"][0]["use"] == "left"


@needs_ffmpeg
def test_dead_left_then_dead_right_copy_is_dead_channel(tmp_path):
    sig = voice(40, 4)
    x = np.zeros((40 * SR, 2), np.float32)
    x[:20 * SR, 1] = sig[:20 * SR]; x[:20 * SR, 0] = sig[:20 * SR] * 0.01
    x[20 * SR:, 0] = sig[20 * SR:]; x[20 * SR:, 1] = sig[20 * SR:] * 0.01
    r = scan(tmp_path, x)
    assert r["channel_layout"] == "dead_channel"
    assert [m["use"] for m in r["channel_map"]] == ["right", "left"]


@needs_ffmpeg
def test_a_true_stereo_mix_is_neither(tmp_path):
    centre = voice(36, 6)
    side = voice(36, 7, 0.03)
    x = np.stack([centre + side, centre - side], axis=1)          # correlated centre voice, both channels equally live
    r = scan(tmp_path, x)
    assert r["channel_layout"] == "stereo_mix"
    assert all(m["use"] == "mix" for m in r["channel_map"])


@needs_ffmpeg
def test_mono_file_is_mono_with_no_map(tmp_path):
    ep = Episode(tmp_path / "ep"); ep.create()
    sf.write(ep.raw / "m.wav", voice(10, 8), SR, subtype="PCM_16")
    f = media.ingest(ep)["files"][0]
    assert f["channel_layout"] == "mono" and "channel_map" not in f


@needs_ffmpeg
def test_ingest_records_layout_evidence_and_one_info_line_instead_of_range_warnings(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "CHANNEL_WINDOW_S", 0.5)
    ep = Episode(tmp_path / "ep"); ep.create()
    x, _, _ = two_mics()
    sf.write(ep.raw / "master.wav", x, SR, subtype="PCM_16")
    r = media.ingest(ep)
    f = r["files"][0]
    assert f["channel_layout"] == "speaker_per_channel" and f["channel_map"][0]["use"] == "sum"
    assert f["channel_evidence"]["alternations"] >= 4
    assert not any("dead" in w for w in r["warnings"])
    assert len(r["info"]) == 1 and "one host per channel" in r["info"][0] and "master.wav" in r["info"][0]
    assert read_json(ep.artifact("media.json"))["files"][0]["channel_layout"] == "speaker_per_channel"
    assert "master.wav: speaker_per_channel" in state.briefing(ep)


# ---- 2. sum, don't gate ---------------------------------------------------------------------------------------------

@needs_ffmpeg
def test_sum_keeps_level_and_both_hosts_in_the_analysis_audio(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "CHANNEL_WINDOW_S", 0.5)
    ep = Episode(tmp_path / "ep"); ep.create()
    x, a, b = two_mics()
    sf.write(ep.raw / "master.wav", x, SR, subtype="PCM_16")
    media.ingest(ep)
    y, sr = sf.read(str(audio.extract_all(ep)[0]), dtype="float32")
    rms = lambda s: np.sqrt(np.mean(np.asarray(s, np.float64) ** 2))
    for (who, t0, t1), ref in zip(TURNS[:2], (a, b)):                    # one A turn, one B turn
        got = rms(y[int((t0 + 1) * sr):int((t1 - 1) * sr)])
        want = rms(ref[int((t0 + 1) * SR):int((t1 - 1) * SR)])
        assert abs(20 * np.log10(got / want)) < 1.0, f"host {who}: the sum must keep full level (average would be -6 dB), not gate"


@needs_ffmpeg
def test_sum_never_clips_even_when_both_mics_are_hot(tmp_path):
    ep = Episode(tmp_path / "ep"); ep.create()
    t = np.arange(10 * SR) / SR
    x = np.stack([0.7 * np.sin(2 * np.pi * 200 * t), 0.7 * np.sin(2 * np.pi * 200.5 * t)], axis=1).astype(np.float32)
    sf.write(ep.raw / "hot.wav", x, SR, subtype="PCM_16")
    media.ingest(ep)
    p = ep.artifact("media.json")
    m = json.loads(p.read_text())
    m["files"][0].update(channel_layout="speaker_per_channel", channel_map=[{"start": 0.0, "end": 10.0, "use": "sum"}])
    p.write_text(json.dumps(m))
    y, _ = sf.read(str(audio.extract_all(ep, force=True)[0]), dtype="float32")
    assert np.abs(y).max() < 1.0 and np.abs(y).max() > 0.85, "peaks of the sum are limited smoothly, not hard clipped"


def test_soft_limit_is_transparent_below_the_knee_and_bounded_above():
    v = np.linspace(-3, 3, 10001, dtype=np.float32)
    out = audio.soft_limit(v)
    inside = np.abs(v) <= audio.LIMIT_KNEE
    assert np.array_equal(out[inside], v[inside])
    assert np.abs(out).max() <= 1.0 and np.all(np.diff(out) >= 0)


@needs_ffmpeg
def test_compile_sums_a_sum_source_to_dual_mono_with_both_hosts(tmp_path):
    ep = Episode(tmp_path / "ep"); ep.create()
    x, a, b = two_mics()
    sf.write(ep.raw / "master.wav", x, SR, subtype="PCM_16")
    cm = [{"start": 0.0, "end": 36.0, "use": "sum"}]
    src = {"M": {"file": "raw/master.wav", "kind": "audio", "start_offset": 0.0, "channel_map": cm}}
    doc = editdoc.new_doc("rough", 30, (64, 36), {**src, "V": {"file": "raw/master.wav", "kind": "video", "start_offset": 0.0}}, 36.0)
    doc["tracks"]["dialogue"].append({"id": "d1", "src": "M", "o0": 0.0, "o1": 36.0, "e0": 0.0, "xfade_ms": 0, "gain_db": 0.0})
    assert not [p for p in editdoc.validate(doc) if "channel_map" in p]
    y, sr = sf.read(str(comp.compile_audio(ep, doc, tmp_path / "d.wav")), dtype="float32", always_2d=True)
    rms = lambda s: np.sqrt(np.mean(np.asarray(s, np.float64) ** 2))
    for (t0, t1), ref in (((1, 5), a), ((7, 11), b)):
        L, R = y[t0 * sr:t1 * sr, 0], y[t0 * sr:t1 * sr, 1]
        assert rms(L - R) < 0.02 * rms(L), "both ears carry the same dual-mono signal"
        assert abs(20 * np.log10(rms(L) / rms(ref[t0 * SR:t1 * SR]))) < 1.0
    doc["sources"]["M"].pop("channel_map")                              # prove the test can fail: unmapped, host A is one-eared
    y2, _ = sf.read(str(comp.compile_audio(ep, doc, tmp_path / "d2.wav")), dtype="float32", always_2d=True)
    assert rms(y2[sr:5 * sr, 1]) < 0.1 * rms(y2[sr:5 * sr, 0])


@needs_ffmpeg
def test_assemble_copies_a_sum_map_and_handoff_warns_about_raw_stereo_sources(synth_episode):
    from ave import assemble, handoff
    ep = synth_episode
    p = ep.artifact("media.json")
    m = json.loads(p.read_text())
    cm = [{"start": 0.0, "end": 30.0, "use": "sum"}]
    for f in m["files"]:
        if f["file"].endswith(("recorder.wav", "camA.mp4")):
            f["channel_map"] = cm
    p.write_text(json.dumps(m))
    doc = assemble.build(ep, (640, 360))
    assert doc["sources"]["A"]["channel_map"] == cm and not editdoc.validate(doc)
    plan = handoff.plan(ep, doc, name="t")
    assert any("channel_map" in w and "dual mono" in w for w in plan["warnings"])


# ---- 3. speakers from channels --------------------------------------------------------------------------------------

def words_for(spans):
    return [{"w": f"w{i}", "start": s, "end": e, "conf": 1.0} for i, (s, e, _) in enumerate(spans)]


def test_words_are_labelled_by_the_louder_channel_including_a_short_interjection_on_the_quiet_channel():
    hop, n = 0.01, 3600
    L, R = np.full(n, 1e-5), np.full(n, 1e-5)        # envelopes (RMS per 10 ms)
    L[0:2000] = 0.2; R[0:2000] = 0.2 * BLEED          # A talks 0-20 s; B's mic holds only A's bleed
    L[1000:1070] = 1e-5; R[1010:1050] = 0.15          # A pauses 10.0-10.7 s and B says "yeah" for 0.4 s on the quiet (right) channel
    R[2000:3600] = 0.2; L[2000:3600] = 0.2 * BLEED    # B talks 20-36 s
    truth = [(1.0, 1.5, "S1"), (9.0, 9.9, "S1"), (10.1, 10.5, "S2"), (10.8, 11.5, "S1"), (22.0, 22.4, "S2"), (30.0, 30.5, "S2")]
    words = [{"w": "x", "start": s_, "end": e, "conf": 1.0} for s_, e, _ in truth]
    transcribe.label_words_by_channel(words, L, R, hop)
    assert [w["speaker"] for w in words] == [t[2] for t in truth]
    assert all(w["speaker_margin_db"] >= 6 for w in words)


def test_a_word_in_silence_takes_the_nearest_labelled_neighbour_with_low_confidence():
    hop, n = 0.01, 1000
    L, R = np.full(n, 1e-5), np.full(n, 1e-5)
    L[0:300] = 0.2
    R[600:1000] = 0.2
    words = [{"w": "a", "start": 1.0, "end": 2.0}, {"w": "um", "start": 4.0, "end": 4.2}, {"w": "b", "start": 7.0, "end": 8.0}]
    transcribe.label_words_by_channel(words, L, R, hop)
    assert [w["speaker"] for w in words] == ["S1", "S1", "S2"]       # 4.0 is nearer to the A word (end 2.0, gap 2) than B (start 7.0, gap 2.8)
    assert words[1]["speaker_margin_db"] is None and words[0]["speaker_margin_db"] > 20


def _stub_asr(monkeypatch, words):
    monkeypatch.setattr(transcribe, "run_fluidaudio_asr", lambda *a, **k: [dict(w) for w in words])


@needs_ffmpeg
def test_transcribe_derives_speakers_from_channels_with_no_diarizer(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "CHANNEL_WINDOW_S", 0.5)
    ep = Episode(tmp_path / "ep"); ep.create()
    x, _, _ = two_mics()
    sf.write(ep.raw / "master.wav", x, SR, subtype="PCM_16")
    media.ingest(ep)
    audio.extract_all(ep)
    ws = [{"w": f"w{i}", "start": t0 + 1.0, "end": t0 + 1.4, "conf": 0.9} for i, (_, t0, _) in enumerate(TURNS)]
    _stub_asr(monkeypatch, ws)
    monkeypatch.setattr(transcribe, "run_fluidaudio_diarization", lambda *a, **k: pytest.fail("the diarizer must not run in channels mode"))
    tr = transcribe.transcribe(ep)
    assert tr["engine"]["diarization"] == "channels"
    assert [w["speaker"] for w in tr["words"]] == ["S1", "S2", "S1", "S2", "S1", "S2"]
    assert tr["speakers"]["S1"]["channel"] == "left" and tr["speakers"]["S2"]["channel"] == "right"
    assert tr["speakers"]["S1"]["name"] is None
    assert all(0 < u["confidence"] <= 1 for u in tr["utterances"])
    assert not ep.artifact("speakers.json").exists()                     # embeddings are optional now
    assert read_json(ep.artifact("transcript.json"))["engine"]["diarization"] == "channels"


@needs_ffmpeg
def test_check_diarization_runs_the_diarizer_and_reports_agreement(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "CHANNEL_WINDOW_S", 0.5)
    ep = Episode(tmp_path / "ep"); ep.create()
    x, _, _ = two_mics()
    sf.write(ep.raw / "master.wav", x, SR, subtype="PCM_16")
    media.ingest(ep)
    audio.extract_all(ep)
    _stub_asr(monkeypatch, [{"w": "a", "start": 1.0, "end": 1.4, "conf": 1.0}, {"w": "b", "start": 7.0, "end": 7.4, "conf": 1.0}])
    # the diarizer calls the LEFT host "X" and the RIGHT host "Y" (its own ids), and gets one 4 s segment wrong
    segs = [{"speakerId": "X", "startTimeSeconds": t0, "endTimeSeconds": t1, "embedding": [0.1]} for w, t0, t1 in TURNS if w == "A"]
    segs += [{"speakerId": "Y", "startTimeSeconds": t0, "endTimeSeconds": t1, "embedding": [0.2]} for w, t0, t1 in TURNS if w == "B"]
    segs[0]["speakerId"] = "Y"
    monkeypatch.setattr(transcribe, "run_fluidaudio_diarization", lambda *a, **k: {"segments": segs})
    tr = transcribe.transcribe(ep, check_diarization=True)
    chk = tr["diarization_check"]
    assert chk["segments"] == 6 and chk["agree"] == 5 and abs(chk["agreement"] - 5 / 6) < 1e-3
    assert chk["mapping"] == {"X": "S1", "Y": "S2"}
    assert tr["engine"]["diarization"] == "channels"                     # labels still come from the channels


@needs_ffmpeg
def test_speakers_from_channels_on_a_non_speaker_per_channel_file_fails_clearly(tmp_path):
    ep = Episode(tmp_path / "ep"); ep.create()
    sf.write(ep.raw / "m.wav", voice(10, 9), SR, subtype="PCM_16")
    media.ingest(ep)
    audio.extract_all(ep)
    with pytest.raises(SystemExit, match="speaker_per_channel"):
        transcribe.transcribe(ep, speakers_from="channels")


# ---- real data (skipped when the drive / scratch diarization are absent) --------------------------------------------

_NONE = Path("/nonexistent/ave-real-footage-not-configured")


def _real(var: str) -> Path:
    """Optional real-footage inputs come from an env var; unset means the dependent tests skip (nothing is ever probed on a hard-coded drive)."""
    v = os.environ.get(var)
    return Path(v).expanduser() if v else _NONE

# AVE_TEST_REAL_MASTER: a real stereo recorder .wav; AVE_TEST_REAL_DIARIZATION: its diarization JSON
REAL = _real("AVE_TEST_REAL_MASTER")
DIAR = _real("AVE_TEST_REAL_DIARIZATION")


@needs_ffmpeg
@pytest.mark.skipif(not (REAL.exists() and DIAR.exists()), reason="real master / scratch diarization not available")
def test_real_master_is_speaker_per_channel_and_channels_agree_with_diarization():
    levels, corr = media.channel_scan(REAL, 0)
    r = media.classify_layout(levels, corr, media.CHANNEL_WINDOW_S, 8437.6)
    assert r["channel_layout"] == "speaker_per_channel"
    env_l, env_r = media.channel_envelopes(REAL, 0)
    segs = [s for s in json.loads(DIAR.read_text())["segments"] if s["endTimeSeconds"] - s["startTimeSeconds"] > 3.0]
    chk = transcribe.diarization_agreement(env_l, env_r, media.ENVELOPE_HOP_S, segs)
    print("real-data check:", {k: v for k, v in chk.items() if k != "per_speaker"}, r["evidence"])
    assert chk["agreement"] >= 0.98
