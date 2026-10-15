import subprocess
from pathlib import Path
import numpy as np
from ave import compile as comp, editdoc, ffutil
from test_compile import _overlay_png, overlay


def px(path, n=150, x=350, y=240):
    out = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-vf", f"select=eq(n\\,{n}),format=rgb24,crop=1:1:{x}:{y}", "-frames:v", "1", "-f", "rawvideo", "-"], capture_output=True).stdout
    return out.hex()


def test_dbg(synth_episode):
    cap = {}
    orig = ffutil.run
    def spy(args, **kw):
        if any("overlay" in str(a) for a in args):
            cap["args"] = list(args); cap["cwd"] = kw.get("cwd")
        return orig(args, **kw)
    ffutil.run = spy
    try:
        _overlay_png(synth_episode.work / "ov.png", (255, 255, 255, 128))
        _overlay_png(synth_episode.work / "top.png", (0, 255, 0, 255))
        doc = editdoc.load_latest(synth_episode, "assembly")
        doc["tracks"]["overlays"] = [
            overlay(id="top", file="work/top.png", layer=5, x=300, y=200, w=100, h=80),
            overlay(id="half", file="work/ov.png", layer=1, x=50, y=40),
            overlay(id="under", file="work/ov.png", layer=0, x=300, y=200, w=100, h=80)]
        rep = comp.render(synth_episode, doc, synth_episode.root / "al.mp4", preset="master", keep_intermediates=True)
    finally:
        ffutil.run = orig
    print("ARGS", cap["args"])
    print("REAL", px(rep["out"]), "half-pt", px(rep["out"], 150, 100, 80))
    joined = Path(cap["args"][2])
    print("joined exists", joined.exists(), "pixel in joined", px(joined))
    a = cap["args"]
    fc = a[a.index("-filter_complex") + 1]
    outp = a[-1]
    def variant(name, args):
        o = joined.parent / f"var_{name}.mp4"
        args = list(args); args[-1] = str(o)
        r = subprocess.run([ffutil._bin("ffmpeg"), "-nostdin", "-v", "error", *args], capture_output=True, text=True)
        print("VAR", name, r.returncode, r.stderr[-200:], px(o) if o.exists() else None)
    def px2(o): return px(o) + " " + px(o, 150, 100, 80) + " f130:" + px(o, 130)
    def variant(name, args, fcfn=None):
        o = joined.parent / f"var_{name}.mp4"
        args = list(args); args[-1] = str(o)
        if fcfn:
            k = args.index("-filter_complex") + 1; args[k] = fcfn(args[k])
        r = subprocess.run([ffutil._bin("ffmpeg"), "-nostdin", "-v", "error", *args], capture_output=True, text=True)
        print("VAR", name, r.returncode, r.stderr[-300:], px2(o) if o.exists() else None, flush=True)
    variant("asis", a)
    variant("no_enable", a, lambda f: f.replace(":enable='gte(n,120)*lt(n,180)'", ""))
    variant("eof_repeat", a, lambda f: f.replace("eof_action=pass", "eof_action=repeat"))
    variant("fc_threads1", ["-filter_complex_threads", "1"] + a)
    variant("fmt_yuv420", a, lambda f: f.replace("format=auto", "format=yuv420"))
    variant("fmt_yuv420p_main", a, lambda f: "[0:v]format=yuva420p[m];" + f.replace("[0:v][ov1]", "[m][ov1]"))
    variant("fps_main", a, lambda f: "[0:v]fps=30[m];" + f.replace("[0:v][ov1]", "[m][ov1]"))
    variant("no_framerate", [x for j, x in enumerate(a) if not (x in ("-framerate",) or (j > 0 and a[j-1] == "-framerate"))])
    variant("shortest0", a, lambda f: f.replace("eof_action=pass", "eof_action=pass:shortest=0"))
    variant("tq", [y for j, x in enumerate(a) for y in ((["-thread_queue_size", "1024"] if x == "-loop" else []) + [x])])
    # split into 3 chained post passes (files)
    assert False, "dbg"
