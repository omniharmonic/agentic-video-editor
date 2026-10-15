"""The quote reel: animated quote cards on a long loop, no speaker audio. Its own product, never part of a clip.

Creator feedback 2026-10-14: a motion-graphic quote card BEFORE the speaker says the same quote is awkward, but the quote-card motion
itself is loved. So the quote card lives here, as a looping reel (a screen for a lobby, a story background, a social loop), and
`ave shorts` never adds one to a short (`opening.refuse_quote_cards` rejects it). A short opens with a title instead (`opening.py`).

  build(ep, quotes, out)   renders each quote with the `quote-card` template (alpha), lays it over the brand ink plate, joins the cards.
                           The cards fade in from and out to the plate, so the last card runs into the first: it loops without a seam.
                           Video only (`-an`): there is no speaker audio to be out of sync with. Verbatim quotes only (checked against the
                           transcript), each starting on a complete sentence/phrase (`content.sources.quote_completeness`).
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

from . import motion
from .episode import Episode

KIND = "quote_reel"


def check_quotes(ep: Episode, quotes: list[dict], max_chars: int = 220) -> list[str]:
    """Every problem in the list (empty = fine): verbatim in the transcript, complete start, within the template's length."""
    from .content import sources as S
    words = S.transcript_words(ep)
    text = " ".join(w["w"] for w in words)
    problems = []
    if not quotes:
        return ["no quotes"]
    for i, q in enumerate(quotes, 1):
        t = str(q.get("quote") or "").strip()
        tag = f"quote {i}"
        if not t:
            problems.append(f"{tag}: empty")
            continue
        if len(t) > max_chars:
            problems.append(f"{tag}: {len(t)} characters (max {max_chars})")
        if words:
            problems += [f"{tag}: {x}" for x in S.quote_problems(t, text)]
        problems += [f"{tag}: {x}" for x in S.quote_completeness(ep, t, words)]
    return problems


def next_path(out_dir: Path, stem: str) -> Path:
    """`<stem>.v1.mp4`, `.v2`...: a reel is never overwritten."""
    out_dir.mkdir(parents=True, exist_ok=True)
    taken = [int(m[1]) for f in out_dir.glob(f"{stem}.v*.mp4") if (m := re.search(r"\.v(\d+)\.mp4$", f.name))]
    return out_dir / f"{stem}.v{max(taken, default=0) + 1}.mp4"


def build(ep: Episode, quotes: list[dict], out: Path | None = None, card_s: float = 6.0, canvas: tuple[int, int] = (1080, 1920),
          fps: float = 30.0, name: str = "quote-reel", loops: int = 1) -> dict:
    """Render the reel. `quotes`: [{"quote", "attribution", "title"?}]. `loops` repeats the sequence that many times in the file (a
    long loop: 4 cards x 3 = 72 s at 6 s a card); the file itself also loops seamlessly."""
    problems = check_quotes(ep, quotes)
    if problems:
        raise SystemExit("the quote reel was not built:\n  - " + "\n  - ".join(problems))
    out = Path(out) if out else next_path(ep.exports / "quote_reels", name)
    if out.exists():
        raise SystemExit(f"{out} exists; a reel is never overwritten (pass a new --out or let the tool pick the next version)")
    out.parent.mkdir(parents=True, exist_ok=True)
    tokens = motion.load_tokens()
    ink = tokens["colors"]["ink"].lstrip("#")
    w, h = canvas
    with tempfile.TemporaryDirectory(prefix="ave-reel-", dir=out.parent) as td:
        td = Path(td)
        parts = []
        for i, q in enumerate(quotes, 1):
            props = {"quote": q["quote"].strip(), "attribution": q.get("attribution", ""), "title": q.get("title", ""), "backdrop": "solid"}
            spec = motion.normalize_spec({"template": "quote-card", "props": props, "duration": card_s, "fps": fps, "canvas": (w, h)})
            mov = td / f"card{i}.mov"
            motion.render(spec, mov)
            part = td / f"card{i}.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=0x{ink}:s={w}x{h}:r={fps:g}:d={card_s}", "-i", str(mov),
                            "-filter_complex", "[0][1]overlay=0:0:format=auto,format=yuv420p", "-an", "-c:v", "libx264", "-crf", "18", "-preset", "medium",
                            "-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-r", f"{fps:g}",
                            str(part)], check=True)
            parts.append(part)
        listing = td / "list.txt"
        listing.write_text("".join(f"file '{p}'\n" for _ in range(max(1, loops)) for p in parts))
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-an", "-c:v", "libx264", "-crf", "18",
                        "-preset", "medium", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-r", f"{fps:g}", str(out)], check=True)
    info = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json", str(out)],
                          capture_output=True, text=True, check=True)
    meta = json.loads(info.stdout)
    side = out.with_suffix(".json")
    rec = {"kind": KIND, "draft": True, "file": str(out), "duration": round(float(meta["format"]["duration"]), 3), "card_s": card_s,
           "cards": len(quotes), "loops_in_file": max(1, loops), "canvas": [w, h], "fps": fps, "audio": "none",
           "loops_seamlessly": True, "quotes": quotes, "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "note": "animated quote cards on a loop, no speaker audio. Not a clip opening and never prepended to one (see skills/ave-shorts, docs/motion.md)"}
    side.write_text(json.dumps(rec, indent=2, ensure_ascii=False))
    streams = [s["codec_type"] for s in meta.get("streams", [])]
    rec["streams"] = streams
    return rec
