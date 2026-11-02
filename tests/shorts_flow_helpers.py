"""The synthetic demo episode taken all the way to an approved, built and exported short, through the REAL commands (`ave demo`, `ave clips ...`,
`ave shorts ...`). No footage, no network. `demo_with_short` is slow (about 12 s): build it once per module and copy it per test."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
from pathlib import Path

import pytest

from ave import cli, humanpresence
from ave.commands import demo
from ave.episode import Episode

NO_FACE = lambda img: []  # noqa: E731


def ave(*argv: str, expect: int | None = 0):
    """Run `ave <argv>` in-process; returns (exit code, parsed JSON stdout or text)."""
    buf, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = cli.main(list(argv))
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1
        if expect is not None and rc != expect:
            raise AssertionError(f"ave {' '.join(argv)} stopped: {e.code!r}\n{err.getvalue()}") from e
        return rc, {"error": str(e.code), "stderr": err.getvalue()}
    out = buf.getvalue().strip()
    if expect is not None and rc != expect:
        raise AssertionError(f"ave {' '.join(argv)} exited {rc} (wanted {expect}):\n{out[-800:]}\n{err.getvalue()[-800:]}")
    try:
        return rc, json.loads(out)
    except ValueError:
        return rc, out


def stub_title_overlay(mp: pytest.MonkeyPatch) -> None:
    """The title opening renders a motion template in Chromium (seconds per call); these tests are not about the picture of the title, so (like
    tests/conftest.py `_fast_title_overlay`, which only covers function-scoped tests) it becomes a tiny transparent .mov keyed by its props."""
    import hashlib
    import subprocess
    from ave import opening

    def fake(ep, props, duration, canvas, fps):
        key = hashlib.sha256(json.dumps([props, duration, list(canvas), fps], sort_keys=True).encode()).hexdigest()[:10]
        rel = ep.work.relative_to(ep.root) / "motion" / f"short-title-stub-{key}.mov"
        target = ep.root / rel
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            w, h = canvas
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=black:s={w}x{h}:r={fps}:d={duration}",
                            "-vf", "format=rgba,colorchannelmixer=aa=0,format=yuva444p10le", "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
                            str(target)], check=True)
        return rel
    mp.setattr(opening, "render_overlay", fake)


def demo_episode(root: Path) -> Episode:
    with contextlib.redirect_stdout(io.StringIO()):
        demo.run(root)
    return Episode(root)


PUNCTUATED = {  # the demo's tones carry a known transcript with no punctuation; ASR output has it, and the complete-statement gate reads it
    "welcome to the show today we are talking about building things that last": "Welcome to the show today we are talking about building things that last.",
    "thanks for having me it is great to be here with you": "Thanks for having me, it is great to be here with you.",
    "yeah": "Yeah.", "and the thing is": "And the thing is.", "most people never finish what they start": "Most people never finish what they start.",
    "that is exactly right and it is why we need systems that carry the work forward": "That is exactly right, and it is why we need systems that carry the work forward.",
    "absolutely and that is what this whole conversation is about": "Absolutely, and that is what this whole conversation is about.",
}


def punctuate(ep: Episode) -> None:
    """Rewrite work/transcript.json with capitals and full stops (same words, same times)."""
    from ave import synth
    from ave.episode import read_json, write_json
    tr = read_json(ep.artifact("transcript.json"))
    i = 0
    for spk, a, b, text in synth.UTTERANCES:
        toks = PUNCTUATED.get(text, text).split()
        base = text.split()
        assert len(toks) == len(base)
        for k, t in enumerate(toks):
            tr["words"][i + k]["w"] = t
        i += len(base)
    for u, (spk, a, b, text) in zip(tr["utterances"], synth.UTTERANCES):
        u["text"] = PUNCTUATED.get(text, text)
    write_json(ep.artifact("transcript.json"), tr)


def demo_with_short(root: Path, *, platforms: str = "reels,youtube,facebook", decided_by: str = "Maren", tty: bool = True,
                    min_clip_s: float | None = 6.0, punctuated: bool = True) -> Episode:
    """`ave demo`, then (optionally) punctuate the transcript, clips prepare/propose, a keep decision (as a human at a terminal when `tty`), clips build,
    shorts build, shorts export. `min_clip_s` lowers the rubric's hard minimum for THIS episode (a 30 s demo has little to trim)."""
    ep = demo_episode(root)
    if punctuated:
        punctuate(ep)
    if min_clip_s is not None:
        import yaml
        from ave.clips import DEFAULT_RUBRIC
        r = yaml.safe_load(Path(DEFAULT_RUBRIC).read_text())
        r["limits"]["hard_min_s"] = min_clip_s
        (ep.root / "policy").mkdir(exist_ok=True)
        (ep.root / "policy" / "clips.rubric.yaml").write_text(yaml.safe_dump(r, sort_keys=False))
    ave("clips", "prepare", str(root), "--min-s", "10", "--max-s", "26")
    ave("clips", "propose", str(root))
    mp = pytest.MonkeyPatch()
    try:
        if tty:
            mp.setattr(humanpresence, "is_human_terminal", lambda: True)
        ave("clips", "decide", str(root), "c01", "--verdict", "keep", "--by", decided_by)
    finally:
        mp.undo()
    ave("clips", "build", str(root), "--approved-only")
    from ave import shorts
    cfg = shorts.load_config(ep)
    clip = shorts.select_clips(ep, "c01")[0]
    (ep.work / "shorts").mkdir(parents=True, exist_ok=True)
    (ep.work / "shorts" / "titles.json").write_text(json.dumps({"c01": "Why finishing is hard"}))   # the clip's own title is the speaker's words: a topic title is the agent's draft
    mp = pytest.MonkeyPatch()
    try:
        stub_title_overlay(mp)
        shorts.build_short(ep, clip, cfg, detector=NO_FACE)
        shorts.export_short(ep, clip, platforms.split(","), cfg)
    finally:
        mp.undo()
    return ep


def copy_episode(src: Path, dst: Path) -> Episode:
    shutil.copytree(src, dst)
    return Episode(dst)
