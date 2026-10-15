import subprocess
from pathlib import Path
import numpy as np
from ave import compile as comp, editdoc, ffutil
from test_compile import _overlay_png, overlay, frame_at


def test_dbg(synth_episode, capsys):
    orig = ffutil.run
    def spy(args, **kw):
        if any("overlay" in str(a) for a in args):
            print("POST ARGS:", args, flush=True)
        return orig(args, **kw)
    ffutil.run = spy
    try:
        _overlay_png(synth_episode.work / "top.png", (0, 255, 0, 255))
        doc = editdoc.load_latest(synth_episode, "assembly")
        doc["tracks"]["overlays"] = [overlay(id="top", file="work/top.png", layer=5, x=300, y=200, w=100, h=80)]
        rep = comp.render(synth_episode, doc, synth_episode.root / "al.mp4", preset="master", keep_intermediates=True)
    finally:
        ffutil.run = orig
    out = Path(rep["out"])
    for (x, y) in [(350, 240), (10, 10)]:
        f = frame_at(out, 5.0)
        print("RGB", x, y, f[y, x])
        yuv = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(out), "-vf", f"select=eq(n\\,150),crop=2:2:{x}:{y}", "-frames:v", "1", "-pix_fmt", "yuv420p", "-f", "rawvideo", "-"], capture_output=True).stdout
        print("YUV", x, y, yuv.hex())
    print(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=pix_fmt,color_space,color_range,color_primaries,color_transfer", "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout)
    assert False, "dbg"
