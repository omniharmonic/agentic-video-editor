#!/usr/bin/env python3
"""Rebuild the UI screenshots in docs/images/ from a fully synthetic demo episode. No real footage, people or paths.

    python scripts/shoot_ui_screenshots.py <scratch-dir> [--out docs/images] [--keep-server]

What it does (everything stays inside <scratch-dir>, which must be new or empty):
  1. Builds a ~4 minute demo episode with `ave demo`'s own pipeline, but with a longer made-up conversation between two fictional hosts
     of a fictional show ("The Long Table"). The "footage" is a blue and a red test-pattern camera plus tones; the transcript is
     placeholder prose written for this script (see TALK). Nothing is transcribed and nothing is private.
  2. Proposes clips, renders review previews, records a few keep/maybe/skip decisions, renders quote-card drafts and queues one
     short for publishing (never approved, never sent).
  3. Starts `ave ui` on 127.0.0.1 and captures the dashboard, clips desk, narrow dark clips desk, content, publish and jobs pages with
     Playwright (Chromium). The UI prints the episode folder on some pages, so choose a neutral scratch folder such as /tmp/ave-demo; the
     script checks the captured page text for the OS user name and home-directory prefixes and aborts if any appears.

Needs: ffmpeg, Playwright with Chromium installed (`playwright install chromium`).
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from ave import synth  # noqa: E402

SHOW = "The Long Table"
TITLE = "The Long Table 12"
HOSTS = {"S1": {"name": "Maren Okafor", "camera": "A"}, "S2": {"name": "Tomas Reyes", "camera": "B"}}

# (speaker, text). Placeholder prose about a shared kitchen garden. Timings are generated from the word counts.
TALK = [
    ("S1", "Welcome back to the table. Today we are talking about the shared kitchen garden behind the library, and what it taught us about showing up."),
    ("S2", "Thanks for having me. I should say up front that I killed every plant I touched for the first two summers."),
    ("S1", "That is more common than people admit. What changed for you?"),
    ("S2", "Honestly, I stopped treating the garden as a project and started treating it as a habit. A project ends. A habit just continues, a little at a time."),
    ("S1", "Say more about that. Because most of us plan the big weekend and then nothing happens for a month."),
    ("S2", "Right, the big weekend is a trap. Ten minutes of watering every morning beats a heroic Saturday. The soil does not care how you feel about it, it only cares that you came back."),
    ("S1", "Yeah."),
    ("S2", "And the neighbours notice. Nobody joins a garden because of a plan on a spreadsheet. They join because they see the same person at the same bed every day."),
    ("S1", "So the work is mostly being visible and being reliable."),
    ("S2", "Mostly. The rest is compost. We argued for a whole season about where to put the compost bins, and I think that argument held the group together more than any harvest did."),
    ("S1", "Why would an argument hold a group together?"),
    ("S2", "Because it meant everyone cared enough to disagree out loud. A group that never argues has usually stopped paying attention."),
    ("S1", "That is a line worth keeping. Let us go back to the first summer, though. What did failing look like day to day?"),
    ("S2", "Um"),
    ("S2", "Mostly it looked like yellow leaves and guilt. I would buy a new seedling on Friday to feel better, and by Tuesday it would be dead again. The fix was boring: water, wait, and write down what happened."),
    ("S1", "You kept a notebook."),
    ("S2", "A very ugly notebook. Dates, weather, what I did, what I saw. After a year it told me things I could never have remembered, like which corner floods in April and which one bakes in August."),
    ("S1", "And the library gave you the plot, right? How did that start?"),
    ("S2", "A librarian asked if anyone wanted the strip of grass by the back door. Everyone looked at their shoes. I said yes before I could think about it, which is the only reliable way I have ever started anything."),
    ("S1", "So the lesson is to say yes before you think."),
    ("S2", "The lesson is to make the first step so small that thinking is not required. Dig one bed. Plant one row. The second row is easy once the first one exists."),
    ("S1", "I want to ask about the people side. Eight households shared that plot. How did you decide who did what?"),
    ("S2", "We did not decide, at first. We just wrote names on a calendar and let the gaps show. When a week was empty, somebody noticed, and somebody filled it. The calendar did the nagging so we did not have to."),
    ("S1", "Nobody had to be the boss."),
    ("S2", "Nobody had to be the boss, but somebody had to be the person who printed the calendar. That was me. It turns out that is a form of leadership nobody warns you about."),
    ("S1", "I love that. Before we wrap up, what would you tell someone who wants to start something shared, a garden, a kitchen, a repair shop, anything?"),
    ("S2", "Start smaller than feels respectable. Show up on the same day every week. Write down what happens. And let the first year be allowed to go badly."),
    ("S1", "Thank you, Tomas. This has been The Long Table. We will see you next time."),
]


def build_utterances() -> list[tuple[str, float, float, str]]:
    out, t = [], 1.0
    for spk, text in TALK:
        n = len(text.split())
        dur = 0.2 if text.lower() == "yeah." else (0.4 if text == "Um" else round(n * 0.43, 2))
        if text == "Um":
            out.append((spk, t, t + 0.4, "um"))
            t += 0.4
            continue
        out.append((spk, round(t, 2), round(t + dur, 2), text))
        t += dur + (1.6 if n > 30 and spk == "S1" else 0.45)
    return out


def patch_synth() -> None:
    utts = build_utterances()
    synth.UTTERANCES[:] = utts
    synth.FILLERS.clear()
    synth.FILLERS["um"] = next((a, b) for _, a, b, tx in utts if tx == "um")
    synth.DEAD_AIR[:] = []
    synth.DURATION = round(utts[-1][2] + 3.0, 1)


def ave(*argv: str) -> object:
    from ave import cli
    buf, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        try:
            rc = cli.main([str(a) for a in argv])
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 1
            if rc:
                raise SystemExit(f"ave {' '.join(map(str, argv))} stopped: {e.code or err.getvalue() or buf.getvalue()}")
    if rc:
        raise SystemExit(f"ave {' '.join(map(str, argv))} exited {rc}: {(err.getvalue() or buf.getvalue())[-500:]}")
    s = buf.getvalue().strip()
    try:
        return json.loads(s)
    except ValueError:
        return s


def build_episode(root: Path) -> None:
    from ave.commands import demo
    from ave.episode import Episode, read_json, write_json
    patch_synth()
    with contextlib.redirect_stdout(io.StringIO()):
        demo.run(root)
    ep = Episode(root)
    meta = read_json(ep.meta_path)
    meta["title"] = TITLE
    meta["show"] = SHOW
    meta["speakers"] = {k: dict(v, name=v["name"]) for k, v in HOSTS.items()}
    write_json(ep.meta_path, meta)
    tr = read_json(ep.artifact("transcript.json"))
    tr["speakers"] = {k: {"talk_seconds": 100.0, "name": v["name"]} for k, v in HOSTS.items()}
    write_json(ep.artifact("transcript.json"), tr)


def make_clips(root: Path) -> list[dict]:
    from ave.episode import Episode, read_json
    ave("clips", "prepare", root, "--min-s", "12", "--max-s", "45")
    ave("clips", "propose", root)
    ep = Episode(root)
    clips = read_json(ep.artifact("clips.json"))["clips"]
    clips.sort(key=lambda c: -(c.get("total") or 0))
    ave("clips", "previews", root, "--top", "6")
    return clips


def decide(root: Path, clips: list[dict]) -> None:
    verdicts = ["keep", "maybe", "skip", "keep"]
    for c, v in zip(clips[:4], verdicts):
        args = ["clips", "decide", root, c["id"], "--verdict", v, "--by", "Maren"]
        if v == "skip":
            args += ["--reason", "off_topic"]
        try:
            ave(*args)
        except SystemExit as e:
            print("decide skipped:", e)


def make_content(root: Path, clips: list[dict]) -> None:
    """Two or three quote-card drafts from verbatim lines of approved clips (the synthetic transcript, so no real speech)."""
    from ave.episode import Episode, read_json, write_json
    quotes = [
        ("A habit just continues, a little at a time.", "S2", "ink", "4x5"),
        ("A group that never argues has usually stopped paying attention.", "S2", "paper", "1x1"),
        ("The lesson is to make the first step so small that thinking is not required.", "S2", "clay", "9x16"),
    ]
    ep = Episode(root)
    clips_doc = read_json(ep.artifact("clips.json"))
    picks = []
    for i, (q, spk, style, fmt) in enumerate(quotes):
        cl = next((c for c in clips_doc["clips"] if q.lower().rstrip(".") in (c.get("text") or "").lower()), None)
        if not cl:
            print("no clip holds quote:", q)
            continue
        picks.append({"id": f"quote-q{i + 1}", "template": "quote-post", "style": style, "format": fmt, "source": {"clip": cl["id"]},
                      "slots": {"quote": q.rstrip("."), "attribution": HOSTS[spk]["name"], "show": SHOW},
                      "platforms": ["instagram"], "reason": "A short, self-contained line.", "confidence": 0.8})
    pf = root / "work" / "_picks.json"
    pf.write_text(json.dumps(picks))
    ave("content", "propose", root, pf, "--allow-unapproved")
    ave("content", "render", root)


def queue_short(root: Path) -> None:
    """One short in the publish queue, awaiting approval (the UI never sends anything)."""
    vid = root / "exports" / "shorts" / "habit-short.mp4"
    vid.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30:d=12",
                    "-f", "lavfi", "-i", "sine=f=330:d=12", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(vid)], check=True)
    ave("publish", "queue", root, "--video", vid, "--platforms", "youtube", "--title", "A habit just continues, a little at a time",
        "--description", "A placeholder description for the demo episode.", "--ai-disclosure", "no", "--visibility", "private")


def sequence_stills(root: Path, out: Path) -> None:
    """Title, host and credit cards of the opening/closing, rendered from the neutral show data (docs/sequence-stills/)."""
    from ave.episode import Episode, read_json, write_json
    ep = Episode(root)
    meta = read_json(ep.meta_path)
    meta["show"] = {"name": SHOW, "tagline": "Conversation around one table",
                    "hosts": [{"name": v["name"], "title": "Co-host"} for v in HOSTS.values()],
                    "links": [{"label": "Subscribe", "value": "thelongtable.example"}]}
    meta["number"] = "12"
    meta["episode_title"] = "The shared garden"
    write_json(ep.meta_path, meta)
    ave("sequence", "plan", root)
    ave("sequence", "preview", root)
    found = sorted((root / "work" / "sequence").rglob("*.png"))
    print("stills:", [str(f.relative_to(root)) for f in found])
    out.mkdir(parents=True, exist_ok=True)
    wanted = {"title": "opening-title.png", "host": "opening-hosts.png", "credits": "closing-credits.png"}
    for key, name in wanted.items():
        hit = next((f for f in found if key in f.name.lower()), None)
        if hit:
            shutil.copy(hit, out / name)
            print("wrote", out / name)
        else:
            print("no still matched", key)


def shoot(root: Path, out: Path, keep: bool) -> None:
    from playwright.sync_api import sync_playwright
    env = dict(os.environ, PYTHONPATH=str(REPO / "src"))
    proc = subprocess.Popen([sys.executable, "-c", "import sys;from ave import cli;sys.exit(cli.main(sys.argv[1:]))", "ui", str(root), "--by", "Maren"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    url = None
    t0 = time.time()
    while time.time() - t0 < 30:
        line = proc.stdout.readline()
        m = re.search(r"https?://127\.0\.0\.1:\d+/\S*", line or "")
        if m:
            url = m.group(0)
            break
    if not url:
        proc.kill()
        raise SystemExit("ave ui did not print its URL")
    base = url.split("?")[0].rstrip("/")
    leaks = [getpass.getuser(), "/Users/", "/home/", "claude-", "scratchpad"]
    try:
        with sync_playwright() as p:
            br = p.chromium.launch()

            def page(scheme: str, w: int, h: int):
                ctx = br.new_context(viewport={"width": w, "height": h}, color_scheme=scheme, device_scale_factor=1)
                pg = ctx.new_page()
                pg.goto(url)
                pg.wait_for_load_state("networkidle")
                return pg

            def snap(pg, hash_: str, name: str, wait: float = 1.0, action=None) -> None:
                pg.goto(f"{base}/#{hash_}")
                pg.reload()
                pg.wait_for_load_state("networkidle")
                time.sleep(wait)
                if action:
                    action(pg)
                    time.sleep(2.5)
                text = pg.inner_text("body")
                for bad in leaks:
                    if bad and bad in text:
                        raise SystemExit(f"{name}: page text contains {bad!r}")
                pg.screenshot(path=str(out / name))
                print("wrote", out / name)

            pg = page("light", 1440, 900)
            snap(pg, "/dashboard", "ui-dashboard.png")
            snap(pg, "/clips", "ui-clips-desk.png", 2.0)
            snap(pg, "/content", "ui-content.png")
            snap(pg, "/publish", "ui-publish.png", action=lambda q: q.get_by_text("awaiting_approval").first.click())
            snap(pg, "/jobs", "ui-jobs.png", action=lambda q: q.get_by_role("button", name="Run").first.click())
            pg2 = page("dark", 420, 900)
            snap(pg2, "/clips", "ui-clips-narrow-dark.png", 2.0)
            br.close()
    finally:
        if not keep:
            proc.terminate()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scratch")
    ap.add_argument("--out", default=str(REPO / "docs" / "images"))
    ap.add_argument("--stills-out", default=str(REPO / "docs" / "sequence-stills"))
    ap.add_argument("--keep-server", action="store_true")
    ap.add_argument("--stage", choices=["all", "build", "shoot", "stills"], default="all")
    a = ap.parse_args()
    scratch = Path(a.scratch).expanduser().resolve()
    root = scratch / "ep"
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    os.environ["AVE_CONTENT_ENABLED"] = "1"
    os.environ["AVE_CONFIG_DIR"] = str(scratch / "cfg")
    if a.stage in ("all", "build"):
        if scratch.exists() and any(scratch.iterdir()):
            raise SystemExit(f"{scratch} is not empty")
        scratch.mkdir(parents=True, exist_ok=True)
        build_episode(root)
        clips = make_clips(root)
        decide(root, clips)
        make_content(root, clips)
        queue_short(root)
    if a.stage in ("all", "shoot"):
        shoot(root, out, a.keep_server)
    if a.stage in ("all", "stills"):
        sequence_stills(root, Path(a.stills_out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
