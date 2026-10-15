"""T1: the FFmpeg engine. Every assertion is a REAL measurement of rendered media (ffprobe, pixels, samples, loudness)."""

from __future__ import annotations

import json
import subprocess
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from PIL import Image

from ave import compile as comp
from ave import editdoc, ffutil, presets
from ave.episode import Episode
from conftest import needs_ffmpeg

pytestmark = needs_ffmpeg
SR = 48000


# ---- helpers ------------------------------------------------------------------------------------------------------

def ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True)


def frames_rgb(path: Path, size=(16, 9)) -> np.ndarray:
    """Decode EVERY frame, downscaled, as an (n, h, w, 3) array: lets us assert per-frame content exactly."""
    w, h = size
    out = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-vf", f"scale={w}:{h}:flags=area", "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(out, np.uint8).reshape(-1, h, w, 3)


def frame_at(path: Path, t: float, fps=30) -> np.ndarray:
    """Full-resolution frame number round(t*fps) as (h, w, 3)."""
    p = ffutil.probe(path)
    n = int(round(t * fps))
    out = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-vf", f"select=eq(n\\,{n})", "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(out, np.uint8).reshape(p["h"], p["w"], 3)


def dominant(frame: np.ndarray) -> str:
    m = frame.reshape(-1, 3).mean(axis=0)
    return "blue" if m[2] > 150 and m[0] < 100 else "red" if m[0] > 150 and m[2] < 100 else f"other{tuple(int(x) for x in m)}"


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return data, sr


def fresh(ep: Episode, **over) -> dict:
    return editdoc.clone(editdoc.load_latest(ep, "assembly")) | over


def solid_doc(ep: Episode, src: str, segs: list[tuple[float, float, float]], fps=30.0, canvas=(640, 360), dur=None) -> dict:
    """segs: (o0, o1, e0) video AND dialogue from one source. Returns a valid doc."""
    dur = dur or segs[-1][1]
    d = editdoc.new_doc("rough", fps, canvas, {src: {"file": ep_src_file(ep, src), "kind": "video", "start_offset": 0.0}}, dur)
    for i, (a, b, e) in enumerate(segs):
        d["tracks"]["video"].append({"id": f"v{i}", "src": src, "o0": a, "o1": b, "e0": e, "crop": None, "zoom": 1.0})
        d["tracks"]["dialogue"].append({"id": f"d{i}", "src": src, "o0": a, "o1": b, "e0": e, "xfade_ms": 0, "gain_db": 0.0})
    return d


def ep_src_file(ep, src):
    return "raw/camA.mp4"


@pytest.fixture(scope="module")
def rendered(synth_root, tmp_path_factory):
    """One master render of the pristine synthetic assembly, shared by the read-only assertions."""
    import shutil
    dst = tmp_path_factory.mktemp("rendered") / "episode"
    shutil.copytree(synth_root, dst)
    ep = Episode(dst)
    doc = editdoc.load_latest(ep, "assembly")
    out = dst / "exports" / "assembly.mp4"
    report = comp.render(ep, doc, out)
    return ep, doc, out, report


# ---- ffutil ------------------------------------------------------------------------------------------------------

def test_fps_fraction_and_frame_rounding():
    assert ffutil.fps_fraction(30.0) == Fraction(30)
    assert ffutil.fps_fraction(29.97) == Fraction(30000, 1001)
    assert ffutil.fps_fraction(23.976) == Fraction(24000, 1001)
    assert ffutil.fps_fraction(25) == Fraction(25)
    f = Fraction(30)
    assert [ffutil.frame_of(t, f) for t in (0, 0.0166, 0.0167, 0.05, 1.0)] == [0, 0, 1, 2, 30]
    # one rounding rule: a boundary shared by two segments lands on the same frame
    assert ffutil.frame_of(7.75, f) == ffutil.frame_of(7.75 + 1e-12, f)


def test_probe_and_loudness_of_known_sine(tmp_path):
    p = tmp_path / "sine.wav"
    ff("-f", "lavfi", "-i", "sine=f=1000:d=5:sample_rate=48000", "-af", "volume=0.8", "-ac", "1", str(p))
    info = ffutil.probe(p)
    assert info["has_audio"] and not info["has_video"] and info["sample_rate"] == 48000
    assert info["duration"] == pytest.approx(5.0, abs=0.01)
    m = ffutil.measure_loudness(p)
    assert m["lufs"] == pytest.approx(-23.0, abs=0.4)       # 0.1 amplitude sine ~ -23 dBFS RMS ~ -23 LUFS
    assert m["true_peak_db"] == pytest.approx(-20.0, abs=0.3)


def test_loudness_of_silence_is_finite(tmp_path):
    p = tmp_path / "sil.wav"
    ff("-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "2", str(p))
    m = ffutil.measure_loudness(p)
    assert m["lufs"] <= -70 and m["true_peak_db"] <= -70 and np.isfinite(m["lufs"])


def test_presets_cover_the_charter():
    for name in ("master", "youtube", "proxy", "audio_only"):
        assert name in presets.PRESETS
    assert presets.PRESETS["master"]["lufs"] == -16 and presets.PRESETS["youtube"]["lufs"] == -14
    assert presets.PRESETS["proxy"]["height"] == 540
    with pytest.raises(ValueError, match="unknown preset"):
        presets.get("nope")


# ---- the acceptance render ---------------------------------------------------------------------------------------

def test_render_assembly_report_and_streams(rendered):
    ep, doc, out, rep = rendered
    assert rep["ok"] and rep["preset"] == "master" and Path(rep["out"]) == out
    assert rep["warnings"] == [] or all(isinstance(w, str) for w in rep["warnings"])
    info = ffutil.probe(out)
    assert info["has_video"] and info["has_audio"]
    assert (info["w"], info["h"]) == (640, 360) and info["fps"] == pytest.approx(30.0)
    assert abs(info["duration"] - doc["duration"]) <= 1 / 30
    assert ffutil.count_video_packets(out) == 900                  # exactly duration*fps frames, none dropped or duplicated
    assert rep["expected_duration"] == pytest.approx(30.0) and rep["fps"] == pytest.approx(30.0) and rep["segments"]["video"] == 4
    assert info["pix_fmt"] == "yuv420p" and info["codec"] == "h264"


def test_render_shows_cam_a_then_cam_b(rendered):
    ep, doc, out, rep = rendered
    assert dominant(frame_at(out, 2.0)) == "blue"     # camera A while S1 talks
    assert dominant(frame_at(out, 10.0)) == "red"     # camera B while S2 talks
    assert dominant(frame_at(out, 20.0)) == "blue"


def test_render_loudness_and_true_peak_meet_the_preset(rendered):
    ep, doc, out, rep = rendered
    m = ffutil.measure_loudness(out)                  # measured on the FINAL encoded file, not on an intermediate
    assert abs(m["lufs"] - (-16.0)) <= 1.0
    assert m["true_peak_db"] <= -1.0
    assert rep["lufs"] == pytest.approx(m["lufs"], abs=0.2) and rep["true_peak_db"] == pytest.approx(m["true_peak_db"], abs=0.2)


def test_youtube_preset_targets_minus_14(synth_episode):
    doc = editdoc.load_latest(synth_episode, "assembly")
    rep = comp.render(synth_episode, doc, synth_episode.root / "y.mp4", preset="youtube")
    assert abs(ffutil.measure_loudness(rep["out"])["lufs"] - (-14.0)) <= 1.0
    assert ffutil.measure_loudness(rep["out"])["true_peak_db"] <= -1.0


def test_proxy_is_540p_and_keeps_timing(synth_episode):
    doc = editdoc.load_latest(synth_episode, "assembly")
    doc["canvas"] = {"w": 1920, "h": 1080}               # 640x360 sources are letterboxed up into a 1080p canvas
    rep = comp.render(synth_episode, doc, synth_episode.root / "p.mp4", preset="proxy")
    info = ffutil.probe(rep["out"])
    assert info["h"] == 540 and info["w"] == 960 and abs(info["duration"] - 30.0) <= 1 / 30
    assert dominant(frame_at(Path(rep["out"]), 10.0)) == "red"


def test_audio_only_preset(synth_episode):
    doc = editdoc.load_latest(synth_episode, "assembly")
    rep = comp.render(synth_episode, doc, synth_episode.root / "a.m4a", preset="audio_only")
    info = ffutil.probe(rep["out"])
    assert info["has_audio"] and not info["has_video"] and abs(info["duration"] - 30.0) <= 1 / 30
    assert abs(ffutil.measure_loudness(rep["out"])["lufs"] + 16) <= 1.0


def test_refuses_to_overwrite_and_validates(synth_episode):
    doc = editdoc.load_latest(synth_episode, "assembly")
    out = synth_episode.root / "o.mp4"
    out.write_bytes(b"precious")
    with pytest.raises(FileExistsError):
        comp.render(synth_episode, doc, out, preset="proxy")
    assert out.read_bytes() == b"precious"
    bad = editdoc.clone(doc)
    bad["tracks"]["video"].pop()
    with pytest.raises(ValueError, match="invalid EditDoc"):
        comp.render(synth_episode, bad, synth_episode.root / "bad.mp4", preset="proxy")


def test_failure_cleans_up_partials(synth_episode):
    doc = editdoc.load_latest(synth_episode, "assembly")
    doc["sources"]["B"]["file"] = "raw/missing.mp4"
    out = synth_episode.root / "x.mp4"
    with pytest.raises(FileNotFoundError, match="missing.mp4"):
        comp.render(synth_episode, doc, out, preset="proxy")
    assert not out.exists()
    wd = synth_episode.work / "render"
    assert not wd.exists() or not any(wd.iterdir())


def test_a_transient_segment_failure_is_retried_not_fatal(synth_episode, monkeypatch):
    """Real 22-min render: one of 255 segment encodes exited 255 with empty stderr and the whole 10-minute render died (the same
    command passed alone). A transient failure must cost one retry, a persistent one must still fail loudly."""
    doc = editdoc.load_latest(synth_episode, "assembly")
    real, calls = ffutil.run, {"n": 0}

    def flaky(args, **kw):
        if "-frames:v" in args and calls["n"] == 0:          # the first segment encode fails once
            calls["n"] += 1
            raise ffutil.FFmpegError("ffmpeg failed (255): \n")
        return real(args, **kw)

    monkeypatch.setattr(ffutil, "run", flaky)
    monkeypatch.setattr(comp.time, "sleep", lambda s: None)
    rep = comp.render(synth_episode, doc, synth_episode.root / "retry.mp4", preset="proxy")
    assert rep["ok"] and calls["n"] == 1

    def always(args, **kw):
        if "-frames:v" in args:
            raise ffutil.FFmpegError("ffmpeg failed (255): \n")
        return real(args, **kw)

    monkeypatch.setattr(ffutil, "run", always)
    with pytest.raises(comp.RenderError, match="encoding video segment"):
        comp.render(synth_episode, doc, synth_episode.root / "never.mp4", preset="proxy")
    assert not (synth_episode.root / "never.mp4").exists()


# ---- video: frame exactness, source clock, crop/zoom/letterbox ----------------------------------------------------

@pytest.fixture(scope="module")
def palette(tmp_path_factory):
    """12 half-second colors (15 frames each): frame k of the file has color k//15. Lets us prove frame-exact seeking."""
    d = tmp_path_factory.mktemp("palette") / "episode"
    (d / "raw").mkdir(parents=True)
    cols = ["red", "green", "blue", "yellow", "cyan", "magenta", "white", "orange", "purple", "pink", "brown", "gray"]
    ins = []
    for c in cols:
        ins += ["-f", "lavfi", "-i", f"color=c={c}:s=320x180:r=30:d=0.5"]
    ff(*ins, "-f", "lavfi", "-i", "sine=f=440:d=6:sample_rate=48000", "-filter_complex",
       "".join(f"[{i}:v]" for i in range(12)) + "concat=n=12:v=1:a=0[v]", "-map", "[v]", "-map", "12:a", "-c:v", "libx264",
       "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(d / "raw" / "pal.mp4"))
    ep = Episode(d)
    ep.create("pal")
    return ep, cols


def pal_doc(ep, segs, fps=30.0, offset=0.0):
    d = editdoc.new_doc("rough", fps, (320, 180), {"P": {"file": "raw/pal.mp4", "kind": "video", "start_offset": offset}},
                        segs[-1][1])
    for i, (a, b, e) in enumerate(segs):
        d["tracks"]["video"].append({"id": f"v{i}", "src": "P", "o0": a, "o1": b, "e0": e, "crop": None, "zoom": 1.0})
        d["tracks"]["dialogue"].append({"id": f"d{i}", "src": "P", "o0": a, "o1": b, "e0": e, "xfade_ms": 0, "gain_db": 0.0})
    return d


def color_idx(frame_small: np.ndarray, cols_rgb) -> int:
    m = frame_small.reshape(-1, 3).mean(axis=0)
    return int(np.argmin([np.abs(m - c).sum() for c in cols_rgb]))


PAL_RGB = [(255, 0, 0), (0, 128, 0), (0, 0, 255), (255, 255, 0), (0, 255, 255), (255, 0, 255), (255, 255, 255),
           (255, 165, 0), (128, 0, 128), (255, 192, 203), (165, 42, 42), (128, 128, 128)]


def test_video_source_seek_is_frame_exact(palette, tmp_path):
    ep, cols = palette
    # out [0,1) shows source [3.0,4.0): color index 6 for 15 frames then 7 for 15 frames. A 1-frame seek error flips the first frame.
    doc = pal_doc(ep, [(0.0, 1.0, 3.0), (1.0, 1.5, 0.0)])
    out = tmp_path / "v.mp4"
    comp.compile_video(ep, doc, out)
    idx = [color_idx(f, PAL_RGB) for f in frames_rgb(out)]
    assert len(idx) == 45
    assert idx[:15] == [6] * 15 and idx[15:30] == [7] * 15 and idx[30:45] == [0] * 15


def test_source_clock_uses_start_offset(palette, tmp_path):
    ep, cols = palette
    # start_offset 1.0 means episode time 4.0 is file time 3.0 -> color 6.
    doc = pal_doc(ep, [(0.0, 0.5, 4.0)], offset=1.0)
    out = tmp_path / "off.mp4"
    comp.compile_video(ep, doc, out)
    assert {color_idx(f, PAL_RGB) for f in frames_rgb(out)} == {6}


def test_video_boundaries_snap_to_frames_without_drift(palette, tmp_path):
    ep, cols = palette
    # Boundaries at ms times that are not frame multiples. Total must stay exactly round(dur*fps) frames.
    segs = [(0.0, 0.1234, 0.0), (0.1234, 0.4567, 0.5), (0.4567, 1.0, 1.0)]   # third: source 1.0-1.53 s, so the last frame is color 3
    doc = pal_doc(ep, segs)
    out = tmp_path / "snap.mp4"
    comp.compile_video(ep, doc, out)
    n = len(frames_rgb(out))
    assert n == 30
    idx = [color_idx(f, PAL_RGB) for f in frames_rgb(out)]
    # frame boundaries: round(.1234*30)=4, round(.4567*30)=14
    assert idx[:4] == [0] * 4 and idx[4:14] == [1] * 10 and idx[14:29] == [2] * 15 and idx[29] == 3


def test_non_integer_fps_is_exact(palette, tmp_path):
    ep, cols = palette
    doc = pal_doc(ep, [(0.0, 2.0, 0.0)], fps=29.97)
    out = tmp_path / "ntsc.mp4"
    comp.compile_video(ep, doc, out)
    assert ffutil.count_video_packets(out) == round(2.0 * 30000 / 1001)
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", str(out)],
                       capture_output=True, text=True).stdout.strip()
    assert r == "30000/1001"


def test_source_before_file_start_is_black_and_warned(palette, tmp_path):
    ep, cols = palette
    doc = pal_doc(ep, [(0.0, 1.0, 0.0)], offset=0.5)      # source time = e - 0.5 < 0 for the first half second
    out = tmp_path / "pre.mp4"
    warnings: list[str] = []
    comp.compile_video(ep, doc, out, warnings=warnings)
    fr = frames_rgb(out)
    assert fr[:14].mean() < 12 and color_idx(fr[20], PAL_RGB) == 0
    assert any("before the start" in w for w in warnings)


@pytest.fixture(scope="module")
def split_src(tmp_path_factory):
    """640x360: left half red, right half green, with a blue 40x40 marker at the exact center."""
    d = tmp_path_factory.mktemp("split") / "episode"
    (d / "raw").mkdir(parents=True)
    ff("-f", "lavfi", "-i", "color=c=red:s=640x360:r=30:d=2", "-f", "lavfi", "-i", "sine=f=300:d=2",
       "-vf", "drawbox=x=320:y=0:w=320:h=360:color=green:t=fill,drawbox=x=300:y=160:w=40:h=40:color=blue:t=fill",
       "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(d / "raw" / "split.mp4"))
    ep = Episode(d)
    ep.create("split")
    return ep


def split_doc(canvas, crop=None, zoom=1.0):
    d = editdoc.new_doc("rough", 30, canvas, {"S": {"file": "raw/split.mp4", "kind": "video", "start_offset": 0.0}}, 1.0)
    d["tracks"]["video"].append({"id": "v", "src": "S", "o0": 0, "o1": 1.0, "e0": 0, "crop": crop, "zoom": zoom})
    d["tracks"]["dialogue"].append({"id": "d", "src": "S", "o0": 0, "o1": 1.0, "e0": 0, "xfade_ms": 0, "gain_db": 0.0})
    return d


def px(frame, x, y):
    return tuple(int(v) for v in frame[y, x])


def near(c, target, tol=40):
    return all(abs(a - b) <= tol for a, b in zip(c, target))


def test_crop_selects_source_pixels(split_src, tmp_path):
    out = tmp_path / "crop.mp4"
    comp.compile_video(split_src, split_doc((640, 360), crop=[320, 0, 320, 360]), out)   # right half only, 320x360 -> pillarboxed
    f = frame_at(out, 0.5)
    assert near(px(f, 320, 100), (0, 128, 0)) or near(px(f, 320, 100), (0, 255, 0), 80)   # center is green, never red
    assert not near(px(f, 320, 100), (255, 0, 0), 60)
    assert near(px(f, 10, 180), (0, 0, 0), 20) and near(px(f, 630, 180), (0, 0, 0), 20)    # pillarbox bars, no distortion


def test_letterbox_never_distorts(split_src, tmp_path):
    out = tmp_path / "lb.mp4"
    comp.compile_video(split_src, split_doc((640, 640)), out)    # 16:9 source into a square canvas
    f = frame_at(out, 0.5)
    assert near(px(f, 320, 10), (0, 0, 0), 20) and near(px(f, 320, 630), (0, 0, 0), 20)   # black bars top/bottom
    assert near(px(f, 100, 320), (255, 0, 0), 60) and not near(px(f, 100, 320), (0, 0, 0), 60)   # content fills the middle band
    # the marker stays square: 40px wide marker in a 640-wide fit stays 40x40 (no stretch)
    blue = np.argwhere((f[:, :, 2] > 200) & (f[:, :, 0] < 60) & (f[:, :, 1] < 60))
    h = blue[:, 0].max() - blue[:, 0].min() + 1
    w = blue[:, 1].max() - blue[:, 1].min() + 1
    assert abs(h - w) <= 3


def test_zoom_punches_in_centered(split_src, tmp_path):
    a, b = tmp_path / "z1.mp4", tmp_path / "z2.mp4"
    comp.compile_video(split_src, split_doc((640, 360), zoom=1.0), a)
    comp.compile_video(split_src, split_doc((640, 360), zoom=2.0), b)

    def marker(path):
        f = frame_at(path, 0.5)
        blue = np.argwhere((f[:, :, 2] > 200) & (f[:, :, 0] < 60) & (f[:, :, 1] < 60))
        return blue[:, 1].max() - blue[:, 1].min() + 1, blue[:, 1].mean(), blue[:, 0].mean()

    (w1, cx1, cy1), (w2, cx2, cy2) = marker(a), marker(b)
    assert w2 == pytest.approx(2 * w1, abs=4)
    assert abs(cx2 - 320) < 4 and abs(cy2 - 180) < 4        # zoom is about the center
    assert 28 <= 40 * 0.0 + w1 <= 44


# ---- audio: sample accuracy, crossfade, room tone, music ducking --------------------------------------------------

def make_click_episode(tmp_path, sr=48000, click_sample=None, total=4.0):
    d = tmp_path / "click" / "episode"
    (d / "raw").mkdir(parents=True)
    n = int(total * sr)
    x = np.zeros(n, np.float32)
    click_sample = click_sample if click_sample is not None else int(1.2345 * sr)
    x[click_sample] = 0.9
    sf.write(str(d / "raw" / "click.wav"), x, sr, subtype="PCM_24")
    ep = Episode(d)
    ep.create("click")
    return ep, click_sample


def audio_doc(src_file, offset, segs):
    """segs: (o0, o1, e0, xfade_ms)"""
    d = editdoc.new_doc("rough", 30, (640, 360), {"M": {"file": src_file, "kind": "audio", "start_offset": offset}}, segs[-1][1])
    d["tracks"]["video"] = []
    for i, (a, b, e, x) in enumerate(segs):
        d["tracks"]["dialogue"].append({"id": f"d{i}", "src": "M", "o0": a, "o1": b, "e0": e, "xfade_ms": x, "gain_db": 0.0})
    return d


@pytest.mark.parametrize("sr,tol", [(48000, 0), (44100, 2)])
def test_dialogue_is_sample_accurate(tmp_path, sr, tol):
    ep, click = make_click_episode(tmp_path, sr=sr)
    offset = 0.25
    # file time of click = click/sr; episode time = +offset; segment starts at e0=1.25 -> out time = click/sr + 0.25 - 1.25
    doc = audio_doc("raw/click.wav", offset, [(0.0, 2.0, 1.25, 0)])
    out = tmp_path / "a.wav"
    comp.compile_audio(ep, doc, out)
    data, osr = read_wav(out)
    assert osr == SR and data.shape[0] == 2 * SR
    expected = int(round((click / sr + offset - 1.25) * SR))
    assert abs(int(np.argmax(np.abs(data[:, 0]))) - expected) <= tol


def test_output_length_is_exact_samples(tmp_path):
    ep, _ = make_click_episode(tmp_path)
    doc = audio_doc("raw/click.wav", 0.0, [(0.0, 1.00033, 0.0, 0), (1.00033, 2.5, 1.0, 0)])
    out = tmp_path / "a.wav"
    comp.compile_audio(ep, doc, out)
    data, _ = read_wav(out)
    assert data.shape[0] == round(2.5 * SR)


def test_segment_gain_applies(tmp_path):
    ep, _ = make_click_episode(tmp_path)
    doc = audio_doc("raw/click.wav", 0.0, [(0.0, 2.0, 0.0, 0)])
    doc["tracks"]["dialogue"][0]["gain_db"] = -6.0
    comp.compile_audio(ep, doc, tmp_path / "g.wav")
    data, _ = read_wav(tmp_path / "g.wav")
    assert np.abs(data).max() == pytest.approx(0.9 * 10 ** (-6 / 20), rel=0.01)


def _tone_ep(tmp_path):
    d = tmp_path / "tones" / "episode"
    (d / "raw").mkdir(parents=True)
    t = np.arange(8 * SR) / SR
    x = np.where(t < 4, 0.5 * np.sin(2 * np.pi * 220 * t), 0.5 * np.sin(2 * np.pi * 330 * t)).astype(np.float32)
    sf.write(str(d / "raw" / "tones.wav"), x, SR, subtype="PCM_24")
    ep = Episode(d)
    ep.create("tones")
    return ep


def _max_step(x, center, half=2400):
    seg = x[center - half:center + half, 0]
    return float(np.abs(np.diff(seg)).max())


def test_crossfaded_cut_has_no_click_and_hard_cut_does(tmp_path):
    ep = _tone_ep(tmp_path)
    # Cut from 220 Hz (file 1.0-2.0) to 330 Hz (file 5.0-6.0): contiguous in output at 1.0 s.
    results = {}
    for xf in (0, 20):
        doc = audio_doc("raw/tones.wav", 0.0, [(0.0, 1.0, 1.0011, 0), (1.0, 2.0, 5.0011, xf)])
        out = tmp_path / f"x{xf}.wav"
        comp.compile_audio(ep, doc, out)
        data, _ = read_wav(out)
        assert data.shape[0] == 2 * SR
        results[xf] = _max_step(data, SR)
    natural = 2 * np.pi * 330 / SR * 0.5            # steepest slope of the louder tone
    assert results[20] <= 2.0 * natural             # smooth: no step beyond what the tones themselves do
    assert results[0] > 4 * natural                 # the test is sensitive: the same cut without a fade does click


def test_crossfade_is_equal_power_and_centered(tmp_path):
    ep = _tone_ep(tmp_path)
    doc = audio_doc("raw/tones.wav", 0.0, [(0.0, 1.0, 1.0, 0), (1.0, 2.0, 5.0, 40)])
    comp.compile_audio(ep, doc, tmp_path / "e.wav")
    data, _ = read_wav(tmp_path / "e.wav")
    x = data[:, 0]
    a = SR - 960                                       # fade is centered on the seam: [1.0-20ms, 1.0+20ms]
    rms = lambda s: float(np.sqrt(np.mean(s ** 2)))
    full = 0.5 / np.sqrt(2)
    assert rms(x[a - 2400:a - 100]) == pytest.approx(full, rel=0.03)            # untouched before the fade
    assert rms(x[a + 1920 + 100:a + 1920 + 2400]) == pytest.approx(full, rel=0.03)
    mid = rms(x[a:a + 1920])                                                    # equal power: no 3 dB dip across the fade (uncorrelated tones)
    assert mid == pytest.approx(full, rel=0.08)


def test_crossfade_with_real_overlap_keeps_duration(tmp_path):
    ep = _tone_ep(tmp_path)
    doc = audio_doc("raw/tones.wav", 0.0, [(0.0, 1.02, 1.0, 0), (1.0, 2.0, 5.0, 20)])   # b starts 20 ms before a ends
    comp.compile_audio(ep, doc, tmp_path / "o.wav")
    data, _ = read_wav(tmp_path / "o.wav")
    assert data.shape[0] == 2 * SR
    assert _max_step(data, SR + 480) < 0.08


@pytest.fixture
def quiet_ep(tmp_path):
    """Room tone: stationary pink-ish noise at a known level, plus speech-like bursts elsewhere."""
    d = tmp_path / "rt" / "episode"
    (d / "raw").mkdir(parents=True)
    rng = np.random.default_rng(3)
    noise = rng.standard_normal(10 * SR).astype(np.float32)
    noise = np.convolve(noise, np.ones(8) / 8, mode="same") * 0.01        # low-passed, ~ -40 dBFS rms
    t = np.arange(10 * SR) / SR
    speech = 0.3 * np.sin(2 * np.pi * 220 * t) * ((t < 3) | (t > 8))
    sf.write(str(d / "raw" / "rt.wav"), (noise + speech).astype(np.float32), SR, subtype="FLOAT")
    ep = Episode(d)
    ep.create("rt")
    return ep


def test_room_tone_fill_is_exact_loopfree_and_level_matched(quiet_ep, tmp_path):
    doc = audio_doc("raw/rt.wav", 0.0, [(0.0, 3.0, 0.0, 0), (3.0, 9.0, 0.0, 0), (9.0, 10.0, 9.0, 0)])
    doc["tracks"]["dialogue"][1].pop("e0")
    doc["tracks"]["dialogue"][1].update({"fill": "room_tone", "e0": 0.0})
    doc["tracks"]["room_tone"] = {"src": "M", "e0": 4.0, "e1": 5.2}       # 1.2 s source for a 6 s fill
    comp.compile_audio(quiet_ep, doc, tmp_path / "rt.wav")
    data, _ = read_wav(tmp_path / "rt.wav")
    assert data.shape[0] == 10 * SR
    fill = data[3 * SR:9 * SR, 0]
    src = sf.read(str(quiet_ep.raw / "rt.wav"), dtype="float32")[0][int(4.0 * SR):int(5.2 * SR)]
    rms = lambda s: float(np.sqrt(np.mean(s ** 2)))
    assert 20 * np.log10(rms(fill) / rms(src)) == pytest.approx(0.0, abs=1.0)
    # loop-free: the fill never repeats the 1.2 s source verbatim (a loop would correlate ~1.0 at lag = loop length)
    for lag in (int(1.2 * SR), int(0.6 * SR), int(1.0 * SR)):
        a, b = fill[:SR], fill[lag:lag + SR]
        assert abs(np.corrcoef(a, b)[0, 1]) < 0.5
    # no clicks inside the fill (crossfaded joins): steps stay near the noise's own step size
    steps = np.abs(np.diff(fill))
    assert steps.max() < 8 * steps.std() + 1e-6
    # deterministic: same doc -> byte-identical fill
    comp.compile_audio(quiet_ep, doc, tmp_path / "rt2.wav")
    assert np.array_equal(read_wav(tmp_path / "rt2.wav")[0], data)


def test_fill_without_room_tone_is_a_clear_error(quiet_ep, tmp_path):
    doc = audio_doc("raw/rt.wav", 0.0, [(0.0, 3.0, 0.0, 0)])
    doc["tracks"]["dialogue"][0]["fill"] = "room_tone"
    with pytest.raises(ValueError, match="room_tone"):
        comp.compile_audio(quiet_ep, doc, tmp_path / "x.wav")


def _tone_amp(x, freq, sr=SR):
    """Amplitude of one frequency in x (single-bin DFT), robust against the other tones in the mix."""
    t = np.arange(len(x)) / sr
    return 2 * abs(np.sum(x * np.exp(-2j * np.pi * freq * t))) / len(x)


@pytest.fixture
def music_ep(synth_episode):
    sf.write(str(synth_episode.root / "raw" / "music.wav"), (0.3 * np.sin(2 * np.pi * 1000 * np.arange(40 * SR) / SR)).astype(np.float32),
             SR, subtype="FLOAT")
    return synth_episode


def music_item(**kw):
    m = {"id": "m1", "file": "raw/music.wav", "o0": 0.0, "o1": 30.0, "in": 0.0, "gain_db": -6.0, "duck_db": -8.0,
         "fade_in_s": 0.0, "fade_out_s": 0.0, "license": {}}
    return m | kw


def test_music_ducks_under_dialogue(music_ep, tmp_path):
    doc = editdoc.load_latest(music_ep, "assembly")
    doc["tracks"]["music"] = [music_item(duck_db=-8.0)]
    out = tmp_path / "m.wav"
    comp.compile_audio(music_ep, doc, out)
    x = read_wav(out)[0][:, 0]
    alone = _tone_amp(x[int(7.0 * SR):int(7.6 * SR)], 1000)       # dead air 6-8 s: music only
    under = _tone_amp(x[int(2.0 * SR):int(5.0 * SR)], 1000)       # S1 speaking
    assert alone == pytest.approx(0.3 * 10 ** (-6 / 20), rel=0.04)    # music at its gain when nothing ducks it
    assert 20 * np.log10(under / alone) == pytest.approx(-8.0, abs=1.5)
    # and with duck_db 0 there is no ducking at all
    doc["tracks"]["music"] = [music_item(duck_db=0.0)]
    comp.compile_audio(music_ep, doc, tmp_path / "m0.wav")
    x0 = read_wav(tmp_path / "m0.wav")[0][:, 0]
    assert 20 * np.log10(_tone_amp(x0[int(2 * SR):int(5 * SR)], 1000) / _tone_amp(x0[int(7.0 * SR):int(7.6 * SR)], 1000)) == pytest.approx(0, abs=0.3)


def test_music_fades_in_trim_and_position(music_ep, tmp_path):
    doc = editdoc.load_latest(music_ep, "assembly")
    doc["tracks"]["music"] = [music_item(o0=10.0, o1=20.0, duck_db=0.0, gain_db=0.0, fade_in_s=2.0, fade_out_s=2.0, **{"in": 5.0})]
    comp.compile_audio(music_ep, doc, tmp_path / "f.wav")
    x = read_wav(tmp_path / "f.wav")[0][:, 0]
    amp = lambda a, b: _tone_amp(x[int(a * SR):int(b * SR)], 1000)
    assert amp(8.0, 9.9) < 0.005                                    # silent before o0
    assert amp(10.0, 10.4) < 0.45 * 0.3 and amp(11.6, 12.0) > 0.8 * 0.3 * 0.9   # faded in
    assert amp(13.0, 17.0) == pytest.approx(0.3, rel=0.03)
    assert amp(19.6, 20.0) < 0.45 * 0.3                             # faded out
    assert amp(20.2, 22.0) < 0.005                                  # silent after o1


# ---- overlays and captions ---------------------------------------------------------------------------------------

def _overlay_png(path, rgba, size=(100, 80)):
    Image.new("RGBA", size, rgba).save(path)


def overlay(**kw):
    o = {"id": "o1", "file": "work/ov.png", "o0": 4.0, "o1": 6.0, "x": 50, "y": 40, "w": 100, "h": 80, "layer": 1, "kind": "motion", "alpha": True}
    return o | kw


def test_overlay_appears_only_inside_its_window(synth_episode):
    _overlay_png(synth_episode.work / "ov.png", (0, 255, 0, 255))
    doc = editdoc.load_latest(synth_episode, "assembly")
    doc["canvas"] = {"w": 1280, "h": 720}                # proxy -> 960x540, so overlay geometry must scale by 0.75
    doc["tracks"]["overlays"] = [overlay()]
    rep = comp.render(synth_episode, doc, synth_episode.root / "ov.mp4", preset="proxy")
    out = Path(rep["out"])
    assert ffutil.probe(out)["w"] == 960
    sc = 0.75

    def green_at(t):
        f = frame_at(out, t)
        c = f[int(80 * sc), int(100 * sc)]
        return bool(c[1] > 180 and c[0] < 90 and c[2] < 90)

    assert not green_at(3.9) and not green_at(1.0)
    assert green_at(4.0) and green_at(5.0) and green_at(5.95)       # first and last frame of the window
    assert not green_at(6.0) and not green_at(8.0)
    # geometry: outside the box stays the camera picture (proxy scales the overlay geometry with the picture)
    f = frame_at(out, 5.0)
    assert dominant(np.ascontiguousarray(f[int(300 * sc):, int(300 * sc):])) == "blue"


def test_overlay_alpha_blends_and_layers_order(synth_episode):
    _overlay_png(synth_episode.work / "ov.png", (255, 255, 255, 128))          # 50 % white
    _overlay_png(synth_episode.work / "top.png", (0, 255, 0, 255))
    doc = editdoc.load_latest(synth_episode, "assembly")
    doc["tracks"]["overlays"] = [
        overlay(id="top", file="work/top.png", layer=5, x=300, y=200, w=100, h=80),
        overlay(id="half", file="work/ov.png", layer=1, x=50, y=40),
        overlay(id="under", file="work/ov.png", layer=0, x=300, y=200, w=100, h=80)]
    rep = comp.render(synth_episode, doc, synth_episode.root / "al.mp4", preset="master")
    f = frame_at(Path(rep["out"]), 5.0)
    half = f[80, 100].astype(int)               # blue camera (32,64,255) mixed 50 % with white: much lighter, still bluish
    assert half[0] > 110 and half[2] > 240 and half[1] > 140
    top = f[240, 350].astype(int)               # layer 5 (opaque green) covers layer 0
    assert top[1] > 200 and top[0] < 90


def test_overlay_video_with_alpha_is_timed_from_its_start(synth_episode):
    # a 2 s ProRes 4444 clip: first second transparent, second second opaque yellow. Placed at o0=4 -> yellow only in [5,6)
    ff("-f", "lavfi", "-i", "color=c=0xffff00@0.0:s=100x80:r=30:d=1,format=yuva444p10le",
       "-f", "lavfi", "-i", "color=c=0xffff00@1.0:s=100x80:r=30:d=1,format=yuva444p10le",
       "-filter_complex", "[0][1]concat=n=2:v=1:a=0", "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
       str(synth_episode.work / "ov.mov"))
    doc = editdoc.load_latest(synth_episode, "assembly")
    doc["tracks"]["overlays"] = [overlay(file="work/ov.mov", o0=4.0, o1=6.0)]
    rep = comp.render(synth_episode, doc, synth_episode.root / "mov.mp4", preset="master")
    out = Path(rep["out"])

    def yellow(t):
        c = frame_at(out, t)[80, 100]
        return bool(c[0] > 180 and c[1] > 180 and c[2] < 100)

    assert not yellow(4.2) and not yellow(4.9) and yellow(5.2) and yellow(5.9) and not yellow(6.1)


def write_ass(path, start="0:00:03.00", end="0:00:05.00", w=640, h=360):
    path.write_text(f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Helvetica,60,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,4,0,2,20,20,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,{start},{end},Default,,0,0,0,,WORDS WORDS WORDS
""")


def test_caption_burn_only_during_cue_and_can_be_disabled(synth_episode):
    write_ass(synth_episode.root / "captions.ass")
    doc = editdoc.load_latest(synth_episode, "assembly")
    doc["captions"] = {"srt": None, "ass": "captions.ass", "burn": True}
    on = Path(comp.render(synth_episode, doc, synth_episode.root / "c_on.mp4", preset="master")["out"])
    off = Path(comp.render(synth_episode, doc, synth_episode.root / "c_off.mp4", preset="master", burn_captions=False)["out"])

    def diff(t):
        a, b = frame_at(on, t).astype(int), frame_at(off, t).astype(int)
        d = np.abs(a - b).sum(axis=2) > 120
        return d.sum(), d

    n_in, mask = diff(4.0)
    assert n_in > 500
    ys = np.where(mask.any(axis=1))[0]
    assert ys.min() > 360 * 0.55                       # bottom-aligned caption sits in the lower part of the frame
    assert diff(1.0)[0] < 30 and diff(6.0)[0] < 30     # nothing before / after the cue


def test_caption_burn_missing_file_is_reported(synth_episode):
    doc = editdoc.load_latest(synth_episode, "assembly")
    doc["captions"] = {"srt": None, "ass": "captions/nope.ass", "burn": True}
    with pytest.raises(FileNotFoundError, match="nope.ass"):
        comp.render(synth_episode, doc, synth_episode.root / "n.mp4", preset="proxy")


# ---- scale: 300 segments -----------------------------------------------------------------------------------------

def test_300_segment_doc_renders_frame_exact(palette, tmp_path):
    ep, cols = palette
    n = 300
    segs = []
    for i in range(n):
        o0 = round(i * 0.1, 6)
        segs.append((o0, round(o0 + 0.1, 6), (i % 12) * 0.5 + 0.1))     # each shows 3 frames of palette color i % 12
    doc = pal_doc(ep, segs)
    for i, s in enumerate(doc["tracks"]["dialogue"]):                    # keep dialogue simple: a handful of long segments
        pass
    doc["tracks"]["dialogue"] = [{"id": "d", "src": "P", "o0": 0.0, "o1": 30.0, "e0": 0.0, "xfade_ms": 0, "gain_db": 0.0}]
    assert editdoc.validate(doc) == []
    rep = comp.render(ep, doc, tmp_path / "300.mp4", preset="proxy")
    out = Path(rep["out"])
    assert ffutil.count_video_packets(out) == 900 and abs(ffutil.probe(out)["duration"] - 30.0) <= 1 / 30
    idx = [color_idx(f, PAL_RGB) for f in frames_rgb(out)]
    assert idx == [(i // 3) % 12 for i in range(900)]                    # every frame, every segment, correct
    assert rep["segments"]["video"] == 300


# ---- CLI ----------------------------------------------------------------------------------------------------------

def test_cli_render_stage_resolution(synth_episode, capsys):
    from ave.cli import main
    rc = main(["render", str(synth_episode.root), "--stage", "assembly", "--preset", "proxy"])
    assert rc == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["ok"] and rep["out"].endswith(".mp4") and "assembly" in Path(rep["out"]).name
    assert Path(rep["out"]).parent == synth_episode.exports
    # same output exists now: refuses unless --overwrite
    with pytest.raises(SystemExit):
        main(["render", str(synth_episode.root), "--stage", "assembly", "--preset", "proxy"])
    assert main(["render", str(synth_episode.root), "--stage", "assembly", "--proxy", "--overwrite"]) == 0
    with pytest.raises(SystemExit, match="No rough"):
        main(["render", str(synth_episode.root), "--stage", "rough"])
    with pytest.raises(SystemExit, match="no version 9"):
        main(["render", str(synth_episode.root), "--stage", "assembly", "--version", "9"])


# ---- verification and failure paths -------------------------------------------------------------------------------

def test_verify_rejects_wrong_duration_fps_and_missing_audio(synth_episode, tmp_path):
    doc = editdoc.load_latest(synth_episode, "assembly")
    good = tmp_path / "good.mp4"
    comp.render(synth_episode, doc, good, preset="proxy")
    fps = ffutil.fps_fraction(30.0)
    pr = presets.get("proxy")
    comp._verify(good, doc, pr, fps, (640, 360))                                   # sanity: passes on the real thing
    longer = editdoc.clone(doc) | {"duration": 31.0}
    with pytest.raises(comp.RenderError, match="frames|duration"):
        comp._verify(good, longer, pr, fps, (640, 360))
    with pytest.raises(comp.RenderError, match="fps"):
        comp._verify(good, doc, pr, ffutil.fps_fraction(25.0), (640, 360))
    with pytest.raises(comp.RenderError, match="picture is"):
        comp._verify(good, doc, pr, fps, (1280, 720))
    silent_video = tmp_path / "nov.mp4"
    ff("-i", str(good), "-an", "-c:v", "copy", str(silent_video))
    with pytest.raises(comp.RenderError, match="no audio"):
        comp._verify(silent_video, doc, pr, fps, (640, 360))


def test_encode_failure_leaves_no_output_or_partials(synth_episode):
    (synth_episode.root / "raw" / "camB.mp4").write_bytes(b"this is not a video")
    doc = editdoc.load_latest(synth_episode, "assembly")
    out = synth_episode.root / "bad.mp4"
    with pytest.raises(Exception):
        comp.render(synth_episode, doc, out, preset="proxy")
    assert not out.exists() and not list(synth_episode.root.glob("*.partial*"))
    wd = synth_episode.work / "render"
    assert not wd.exists() or not any(wd.iterdir())


def test_stereo_channels_are_preserved(tmp_path):
    d = tmp_path / "st" / "episode"
    (d / "raw").mkdir(parents=True)
    t = np.arange(2 * SR) / SR
    sf.write(str(d / "raw" / "st.wav"), np.stack([0.5 * np.sin(2 * np.pi * 440 * t), np.zeros_like(t)], 1).astype(np.float32), SR, subtype="FLOAT")
    ep = Episode(d)
    ep.create("st")
    comp.compile_audio(ep, audio_doc("raw/st.wav", 0.0, [(0.0, 2.0, 0.0, 0)]), tmp_path / "st.wav")
    data, _ = read_wav(tmp_path / "st.wav")
    assert np.abs(data[:, 0]).max() == pytest.approx(0.5, rel=0.01) and np.abs(data[:, 1]).max() == 0


def test_audio_only_render_needs_no_video_track(tmp_path):
    ep = _tone_ep(tmp_path)
    doc = audio_doc("raw/tones.wav", 0.0, [(0.0, 3.0, 0.0, 0), (3.0, 6.0, 1.0, 20)])
    rep = comp.render(ep, doc, tmp_path / "t.m4a", preset="audio_only")
    assert rep["ok"] and abs(rep["duration"] - 6.0) < 0.034 and rep["fps"] is None


def test_unreachable_loudness_is_a_warning_not_a_lie(tmp_path):
    """A lone click can never reach -16 LUFS under a -1.5 dBTP ceiling. The report must say so, not pretend."""
    ep, _ = make_click_episode(tmp_path)
    doc = audio_doc("raw/click.wav", 0.0, [(0.0, 4.0, 0.0, 0)])
    rep = comp.render(ep, doc, tmp_path / "c.m4a", preset="audio_only")
    assert rep["true_peak_db"] <= -1.0
    assert any("loudness" in w for w in rep["warnings"]) and rep["lufs"] < -17


def test_room_tone_fill_with_crossfades_keeps_length_and_is_smooth(quiet_ep, tmp_path):
    doc = audio_doc("raw/rt.wav", 0.0, [(0.0, 3.0, 0.0, 0), (3.0, 9.0, 0.0, 40), (9.0, 10.0, 9.0, 40)])
    doc["tracks"]["dialogue"][1]["fill"] = "room_tone"
    doc["tracks"]["room_tone"] = {"src": "M", "e0": 4.0, "e1": 5.2}
    comp.compile_audio(quiet_ep, doc, tmp_path / "rtx.wav")
    data, _ = read_wav(tmp_path / "rtx.wav")
    assert data.shape[0] == 10 * SR
    # the fade out of the 220 Hz speech into room tone: no step larger than the speech's own slope
    assert _max_step(data, 3 * SR, 1500) < 2 * np.pi * 220 / SR * 0.3 * 2.5   # a hard cut would step ~0.3


def test_master_300_segments_timing_is_sane(palette, tmp_path):
    """300 segments in master quality must finish in reasonable time (per-segment parallel encodes, no giant graph)."""
    ep, cols = palette
    segs = [(round(i * 0.1, 6), round(i * 0.1 + 0.1, 6), (i % 12) * 0.5 + 0.1) for i in range(300)]
    doc = pal_doc(ep, segs)
    doc["tracks"]["dialogue"] = [{"id": "d", "src": "P", "o0": 0.0, "o1": 30.0, "e0": 0.0, "xfade_ms": 0, "gain_db": 0.0}]
    t0 = __import__("time").time()
    rep = comp.render(ep, doc, tmp_path / "m300.mp4", preset="master")
    assert rep["ok"] and __import__("time").time() - t0 < 120


@pytest.mark.parametrize("dust", [-1e-6, -1e-9, 0.0, 1e-9, 1e-6])
def test_half_frame_tie_does_not_add_a_leading_frame(synth_episode, tmp_path, dust):
    """A cut whose source time sits EXACTLY between two source frames (camera B starts 1.0 s late, so e0 = 25.25 -> file time 24.25 =
    727.5 frames) used to emit an extra frame at pts -1 for some float dust, which an edit list hid: the picture came out one frame
    short, the container's video duration was 2 frames short, and verification failed at random (about one run in four)."""
    ep = synth_episode
    e0 = 25.25 + dust
    d = editdoc.new_doc("rough", 30, (640, 360), {"B": {"file": "raw/camB.mp4", "kind": "video", "start_offset": 1.0}}, 3.0)
    d["tracks"]["video"].append({"id": "v1", "src": "B", "o0": 0.0, "o1": 3.0, "e0": e0, "crop": None, "zoom": 1.0})
    d["tracks"]["dialogue"].append({"id": "d1", "src": "B", "o0": 0.0, "o1": 3.0, "e0": e0, "xfade_ms": 0, "gain_db": 0.0})
    out = comp.compile_video(ep, d, tmp_path / "tie.mp4")
    pts = [float(x) for x in subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=pts_time", "-of", "csv=p=0",
                                             str(out)], capture_output=True, text=True, check=True).stdout.split()]
    assert min(pts) >= -1e-9, "no frame may sit before the segment start"
    info = ffutil.probe(out)
    assert info["video_duration"] == pytest.approx(3.0, abs=1 / 30 + 1e-6)
    assert ffutil.count_video_packets(out) == 90


def make_spiky_speech_episode(tmp_path, sr=48000, total=40.0):
    """Mimics the real master: speech-like noise bursts around -26 dBFS RMS with sparse near-full-scale transients
    (a crest factor of ~26 dB). Reaching -16 LUFS needs ~+10 dB of gain, which a gain-only stage turns into +10 dBFS spikes."""
    d = tmp_path / "spiky" / "episode"
    (d / "raw").mkdir(parents=True)
    rng = np.random.default_rng(5)
    n = int(total * sr)
    x = np.zeros(n, np.float32)
    pos = 0
    while pos < n:                                          # syllable-like bursts separated by short pauses
        dur = int(rng.uniform(0.1, 0.4) * sr)
        burst = rng.standard_normal(min(dur, n - pos)).astype(np.float32)
        x[pos:pos + len(burst)] = burst * 0.07 * rng.uniform(0.5, 1.5)
        pos += dur + int(rng.uniform(0.05, 0.3) * sr)
    for t in np.arange(1.7, total - 1, 3.1):                # a claps-and-laughs spike every ~3 s, 5 ms wide
        i = int(t * sr)
        x[i:i + int(0.005 * sr)] = 0.999 * np.sign(rng.standard_normal(int(0.005 * sr))).astype(np.float32)
    sf.write(str(d / "raw" / "spiky.wav"), x, sr, subtype="PCM_24")
    ep = Episode(d)
    ep.create("spiky")
    return ep


@pytest.mark.parametrize("preset,ext", [("audio_only", ".m4a"), ("podcast_mp3", ".mp3")])
def test_spiky_speech_never_ends_above_the_true_peak_ceiling(tmp_path, preset, ext):
    """REAL-FOOTAGE REGRESSION (first real episode): a proxy render came out at +1.5 dBTP because gain-only loudnorm met -16 LUFS by
    pushing the transients past full scale. The delivered file must stay at or under the ceiling, and still land near the target."""
    ep = make_spiky_speech_episode(tmp_path)
    doc = audio_doc("raw/spiky.wav", 0.0, [(0.0, 38.0, 0.0, 0)])
    rep = comp.render(ep, doc, tmp_path / f"s{ext}", preset=preset)
    m = ffutil.measure_loudness(rep["out"])
    assert m["true_peak_db"] <= -1.0, f"true peak {m['true_peak_db']} dBTP: the delivered audio clips"
    assert abs(m["lufs"] - (-16.0)) <= 3.0, f"loudness {m['lufs']} LUFS is far from the target even though limiting should make it reachable"


def test_float_dust_before_the_start_of_a_file_is_not_a_black_frame(palette, tmp_path):
    """REAL-FOOTAGE REGRESSION: the first cut of a session asked a camera for src time -1e-9 s and got a black first frame plus a warning."""
    ep, cols = palette
    doc = pal_doc(ep, [(0.0, 1.0, 0.0)], offset=1e-9)                         # episode t=0 maps to source t=-1e-9
    rep = comp.render(ep, doc, tmp_path / "dust.mp4", preset="proxy")
    assert not any("before the start" in w for w in rep["warnings"]), rep["warnings"]
    first = frame_at(Path(rep["out"]), 0.0)
    assert first.mean() > 20, "the first frame is black"
