"""Render ASS through ffmpeg and look at real pixels: captions must stay inside the safe zone at any canvas size."""

import json
import subprocess

import numpy as np
import pytest
from PIL import Image

from ave import captions
from conftest import needs_ffmpeg, needs_libass

pytestmark = needs_ffmpeg

GRAY = "0x606060"


def ff(*args):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *map(str, args)], check=True)


def make_bg(path, size, dur=3.0, with_audio=False):
    args = ["-f", "lavfi", "-i", f"color=c={GRAY}:s={size[0]}x{size[1]}:r=10:d={dur}"]
    if with_audio:
        args += ["-f", "lavfi", "-i", f"sine=f=440:d={dur}", "-c:a", "aac", "-shortest"]
    ff(*args, "-c:v", "libx264", "-pix_fmt", "yuv420p", path)


def frame(video, t, out):
    ff("-ss", t, "-i", video, "-frames:v", 1, out)
    return np.asarray(Image.open(out).convert("RGB")).astype(int)


def caption_bbox(plain, burned, thresh=40):
    """Bounding box (x0, y0, x1, y1) of pixels that differ between the plain and the captioned frame."""
    d = np.abs(plain - burned).max(axis=2) > thresh
    ys, xs = np.where(d)
    if not len(xs):
        return None
    return xs.min(), ys.min(), xs.max() + 1, ys.max() + 1


def render(tmp_path, style_name, size, cues, t=1.5, ep_fonts=None):
    style = captions.get_style(style_name)
    bg = tmp_path / f"bg_{size[0]}x{size[1]}.mp4"
    if not bg.exists():
        make_bg(bg, size)
    ass = tmp_path / f"{style_name}_{size[0]}x{size[1]}.ass"
    info = captions.write_ass(cues, style, size, ass)
    out = tmp_path / f"out_{style_name}_{size[0]}x{size[1]}.mp4"
    captions.burn(bg, ass, out, overwrite=True)
    plain = frame(bg, t, tmp_path / "plain.png")
    burned = frame(out, t, tmp_path / "burned.png")
    return style, info, caption_bbox(plain, burned), (plain, burned)


# Deliberately heavy: two full-width lines of wide capitals-and-descenders text.
def worst_case_cue(style_name, words=True):
    rules = captions.get_style(style_name)["rules"]
    n = rules["max_chars_per_line"]
    l1 = ("WIDE Mmmm Wow " * 10)[:n].rstrip()
    l2 = ("Jumping Quickly Gypsy " * 10)[:n].rstrip()
    toks = (l1 + " " + l2).split()
    ws = [{"w": w, "start": 0.2 + i * 0.12, "end": 0.3 + i * 0.12, "speaker": "S1"} for i, w in enumerate(toks)]
    c = captions.Cue(0.2, 2.8, l1 + "\n" + l2, "S1", ws)
    return c


@pytest.mark.parametrize("style_name", ["podcast", "short", "minimal"])
@pytest.mark.parametrize("size", [(1920, 1080), (1080, 1920)])
@needs_libass
def test_caption_pixels_stay_in_safe_zone(tmp_path, style_name, size):
    cue = worst_case_cue(style_name)
    style, info, bbox, _ = render(tmp_path, style_name, size, [cue])
    assert bbox is not None, "no caption pixels were drawn"
    W, H = size
    s = style["safe"]
    x0, y0, x1, y1 = bbox
    assert x0 >= s["left"] * W - 1 and x1 <= (1 - s["right"]) * W + 1, (bbox, size)
    assert y0 >= s["top"] * H - 1 and y1 <= (1 - s["bottom"]) * H + 1, (bbox, size)
    # centred horizontally (the safe zone is symmetric for every style)
    assert abs((x0 + x1) / 2 - W / 2) <= 0.03 * W
    # and big enough to read: text height at least 3% of the short side
    assert (y1 - y0) >= 0.03 * min(W, H)


@needs_libass
def test_short_style_sits_at_about_65_percent_height(tmp_path):
    for size in ((1080, 1920), (1920, 1080)):
        _, _, bbox, _ = render(tmp_path, "short", size, [worst_case_cue("short")])
        cy = (bbox[1] + bbox[3]) / 2 / size[1]
        assert 0.61 <= cy <= 0.69, (size, cy)


@needs_libass
def test_podcast_style_is_bottom_centred(tmp_path):
    _, _, bbox, _ = render(tmp_path, "podcast", (1920, 1080), [worst_case_cue("podcast")])
    assert bbox[3] / 1080 > 0.85 and bbox[1] / 1080 > 0.6


@needs_libass
def test_karaoke_highlight_progresses(tmp_path):
    """Early in the cue the first word is already in the highlight colour and the last is still white."""
    cue = captions.Cue(0.0, 2.4, "alpha bravo\ncharlie delta",
                       words=[{"w": w, "start": i * 0.6, "end": i * 0.6 + 0.5} for i, w in enumerate("alpha bravo charlie delta".split())])
    style, info, bbox, (plain, burned_mid) = render(tmp_path, "short", (1080, 1920), [cue], t=1.2)
    _, _, _, (_, burned_end) = render(tmp_path, "short", (1080, 1920), [cue], t=2.3)

    def amber(frame_):
        r, g, b = frame_[..., 0], frame_[..., 1], frame_[..., 2]
        return int(((r > 200) & (g > 150) & (g < 235) & (b < 120)).sum())
    assert amber(plain) == 0
    assert 0 < amber(burned_mid) < amber(burned_end)


@needs_libass
def test_burn_preserves_duration_audio_and_burns_captions(tmp_path):
    bg = tmp_path / "in put, 'x'.mp4"           # awkward filename: spaces, comma, quote
    make_bg(bg, (1280, 720), dur=3.0, with_audio=True)
    ass = tmp_path / "c, 'q'.ass"
    captions.write_ass([captions.Cue(0.2, 2.8, "Burned in")], captions.get_style("podcast"), (1280, 720), ass)
    out = tmp_path / "burned out.mp4"
    captions.burn(bg, ass, out)
    pr = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(out)],
                                   capture_output=True, text=True, check=True).stdout)
    kinds = sorted(s["codec_type"] for s in pr["streams"])
    assert kinds == ["audio", "video"]
    assert float(pr["format"]["duration"]) == pytest.approx(3.0, abs=0.1)
    v = next(s for s in pr["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (1280, 720)
    plain = frame(bg, 1.0, tmp_path / "p.png")
    burned = frame(out, 1.0, tmp_path / "b.png")
    assert caption_bbox(plain, burned) is not None
    # before the cue starts nothing is drawn
    assert caption_bbox(frame(bg, 0.05, tmp_path / "p0.png"), frame(out, 0.05, tmp_path / "b0.png")) is None


def test_burn_refuses_to_overwrite_and_cleans_up_on_failure(tmp_path):
    bg = tmp_path / "bg.mp4"
    make_bg(bg, (640, 360), dur=1.0)
    ass = tmp_path / "a.ass"
    captions.write_ass([captions.Cue(0, 1, "x")], captions.get_style("podcast"), (640, 360), ass)
    out = tmp_path / "o.mp4"
    out.write_bytes(b"precious")
    with pytest.raises(FileExistsError):
        captions.burn(bg, ass, out)
    assert out.read_bytes() == b"precious"
    with pytest.raises(FileNotFoundError):
        captions.burn(tmp_path / "missing.mp4", ass, tmp_path / "o2.mp4")
    (tmp_path / "junk.mp4").write_bytes(b"not a video")
    with pytest.raises(RuntimeError):
        captions.burn(tmp_path / "junk.mp4", ass, tmp_path / "o2.mp4")
    assert not list(tmp_path.glob("o2*"))
