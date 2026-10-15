"""Review proxies: small intraframe copies of the footage an edit uses, so Resolve plays smoothly (W10).

4K HEVC from an ExFAT USB drive garbled audio in Resolve under load. An intraframe 1080p proxy (ProRes Proxy / DNxHR LB) next to
the footage fixes playback; `ave handoff --use-proxies` links them and the final renders still come from the ORIGINALS.

Safety rules baked in:
  - originals are only ever READ (ffmpeg `-i`); every output goes to a `proxies/` folder next to the footage;
  - an output is written as `<name>.mov.partial` and renamed only after ffmpeg exits 0, so a cut-off run never leaves a half file
    that looks finished; leftover `.partial` files are removed on the next run;
  - resumable: an existing proxy is kept when it verifies (right codec, duration within one source frame), else rebuilt;
  - disk is checked BEFORE the first byte: refuse when free space < SPACE_FACTOR x the size estimate.
Layout: a file under a raw dir (`02_FOOTAGE`, `raw`, ...) gets `<raw dir>/proxies/<same subpath>.mov`; any other file gets a
`proxies/` folder beside it. `Episode.media_files` skips every `proxies` folder so ingest never treats a proxy as footage.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from . import ffutil
from .episode import PROXY_DIRNAME, Episode, write_json

SPACE_FACTOR = 1.5
DEFAULT_HEIGHT = 1080
# Published bitrates for 1920x1080 at 30 fps, in Mbit/s (scaled by pixels and fps in `estimate_bytes`). Rough on purpose:
# the estimate only has to be good enough to refuse a drive that is clearly too full.
PROFILES = {
    "prores-proxy": {"mbps_1080p30": 45.0, "codec_name": "prores", "label": "ProRes Proxy"},
    "dnxhr-lb": {"mbps_1080p30": 36.0, "codec_name": "dnxhd", "label": "DNxHR LB"},
}
AUDIO_BYTES_PER_SAMPLE = 3          # pcm_s24le
PROGRESS_EVERY_S = 30.0


def hw_decode_available() -> bool:
    """True when this ffmpeg lists the VideoToolbox hwaccel (macOS). Decode only; the encode is software (ProRes/DNxHR)."""
    try:
        out = subprocess.run([ffutil._bin("ffmpeg"), "-hide_banner", "-hwaccels"], capture_output=True, text=True).stdout
    except Exception:
        return False
    return "videotoolbox" in out.split()


def proxy_path(ep: Episode, original: str | Path) -> Path:
    """Where the proxy of `original` lives. Pure path math: nothing is created."""
    f = Path(original).resolve()
    for d in ep.raw_dirs:
        try:
            rel = f.relative_to(d.resolve())
        except ValueError:
            continue
        return d.resolve() / PROXY_DIRNAME / rel.with_suffix(".mov")
    return f.parent / PROXY_DIRNAME / (f.stem + ".mov")


def footage_used(ep: Episode, doc: dict) -> list[Path]:
    """Absolute paths of the video sources the edit actually places on V1 (what Resolve will play), sorted."""
    out = set()
    for k in {s["src"] for s in doc["tracks"]["video"]}:
        f = Path(doc["sources"][k]["file"])
        out.add(f if f.is_absolute() else (ep.root / f).resolve())
    return sorted(out)


def _source_facts(original: Path, height: int) -> dict:
    p = ffutil.probe(original)
    src_h = p["h"] or height
    h = min(height, src_h)
    h -= h % 2
    w = int(round((p["w"] or 16 * h / 9) * h / src_h / 2)) * 2
    return {"duration": p["duration"], "fps": p["fps"] or 30.0, "h": h, "w": w, "channels": p["channels"] or 0}


def estimate_bytes(facts: dict, codec: str) -> int:
    mbps = PROFILES[codec]["mbps_1080p30"] * (facts["w"] * facts["h"]) / (1920 * 1080) * (facts["fps"] / 30.0)
    video = mbps * 1e6 / 8 * facts["duration"]
    audio = facts["channels"] * ffutil.SAMPLE_RATE * AUDIO_BYTES_PER_SAMPLE * facts["duration"]
    return int(video + audio)


def verify(original: Path, proxy: Path, codec: str) -> tuple[bool, str]:
    """(ok, why-not). A proxy is good when it decodes, has the profile's codec and its duration is within one source frame."""
    if not proxy.is_file():
        return False, "missing"
    try:
        pp, sp = ffutil.probe(proxy), ffutil.probe(original)
    except ffutil.FFmpegError as e:
        return False, f"unreadable ({str(e)[:80]})"
    if not pp["has_video"] or pp["codec"] != PROFILES[codec]["codec_name"]:
        return False, f"codec {pp['codec']!r}, wanted {PROFILES[codec]['codec_name']!r}"
    tol = 1.0 / (sp["fps"] or 24.0)
    if abs(pp["duration"] - sp["duration"]) > tol + 1e-3:
        return False, f"duration {pp['duration']:.3f}s vs source {sp['duration']:.3f}s (tolerance {tol:.3f}s)"
    return True, ""


def _encode_args(codec: str) -> list[str]:
    if codec == "prores-proxy":
        return ["-c:v", "prores_ks", "-profile:v", "0", "-pix_fmt", "yuv422p10le", "-vendor", "apl0"]
    return ["-c:v", "dnxhd", "-profile:v", "dnxhr_lb", "-pix_fmt", "yuv422p"]


def _encode(original: Path, partial: Path, codec: str, facts: dict, *, hw: bool, on_progress: Callable[[float], None]) -> None:
    cmd = [ffutil._bin("ffmpeg"), "-nostdin", "-hide_banner", "-v", "error", "-nostats", "-progress", "pipe:1"]
    if hw:
        cmd += ["-hwaccel", "videotoolbox"]
    cmd += ["-i", str(original), "-map", "0:v:0", "-map", "0:a?", "-vf", f"scale=-2:{facts['h']}:flags=bicubic",
            *_encode_args(codec), "-c:a", "pcm_s24le", "-map_metadata", "0", "-f", "mov", "-y", str(partial)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    for line in proc.stdout:
        if line.startswith("out_time_us=") and facts["duration"] > 0:
            try:
                on_progress(min(1.0, int(line.split("=", 1)[1]) / 1e6 / facts["duration"]))
            except ValueError:
                pass
    err = proc.stderr.read() if proc.stderr else ""
    if proc.wait() != 0:
        raise ffutil.FFmpegError(f"ffmpeg failed ({proc.returncode}) making {partial.name}:\n{err[-1500:]}")


def plan_proxies(ep: Episode, files: list[Path], codec: str, height: int, *, force: bool = False) -> dict:
    """Decide what to do for each file WITHOUT writing anything: keep (verified), build, rebuild (bad proxy). Includes the estimate."""
    items, total = [], 0
    for f in files:
        facts = _source_facts(f, height)
        out = proxy_path(ep, f)
        ok, why = (False, "forced") if (force and out.exists()) else verify(f, out, codec)
        action = "keep" if ok else ("build" if why == "missing" else "rebuild")
        need = 0 if ok else estimate_bytes(facts, codec)
        total += need
        items.append({"original": str(f), "proxy": str(out), "action": action, "why": why, "facts": facts, "est_bytes": need})
    probe_dir = Path(items[0]["proxy"]).parent if items else ep.root
    while not probe_dir.exists() and probe_dir != probe_dir.parent:
        probe_dir = probe_dir.parent
    free = shutil.disk_usage(probe_dir).free
    return {"codec": codec, "height": height, "items": items, "estimate_bytes": total, "free_bytes": free,
            "space_ok": free >= SPACE_FACTOR * total}


def gb(n: float) -> str:
    return f"{n / 1e9:.2f} GB"


def run(ep: Episode, files: list[Path], codec: str = "prores-proxy", height: int = DEFAULT_HEIGHT, *, hw: bool | None = None,
        force: bool = False, dry_run: bool = False, log: Callable[[str], None] | None = None) -> dict:
    """Make the proxies. Refuses (SystemExit) when the disk is too full. Returns the plan plus `made`, `kept`, `failed`."""
    from . import progress
    say = log or (lambda m: print(m, flush=True))
    plan = plan_proxies(ep, files, codec, height, force=force)
    todo = [i for i in plan["items"] if i["action"] != "keep"]
    say(f"proxies ({PROFILES[codec]['label']}, {height}p): {len(plan['items'])} file(s), {len(todo)} to make, "
        f"estimate {gb(plan['estimate_bytes'])}, free {gb(plan['free_bytes'])}")
    if not plan["space_ok"]:
        raise SystemExit(f"Refusing: free space {gb(plan['free_bytes'])} is below {SPACE_FACTOR}x the estimate "
                         f"({gb(plan['estimate_bytes'])}, so {gb(SPACE_FACTOR * plan['estimate_bytes'])} needed). "
                         "Free up space or proxy fewer files.")
    result = {**plan, "made": [], "kept": [i["proxy"] for i in plan["items"] if i["action"] == "keep"], "failed": [], "dry_run": dry_run}
    if dry_run:
        return result
    use_hw = hw_decode_available() if hw is None else hw
    progress.auto_log(ep, f"proxies: making {len(todo)} {PROFILES[codec]['label']} proxy file(s), about {gb(plan['estimate_bytes'])}",
                      "note", "proxies", refresh_checklist=False)
    for n, it in enumerate(todo, 1):
        orig, out = Path(it["original"]), Path(it["proxy"])
        if out.resolve() == orig.resolve():                      # impossible by construction; the cheapest guard against clobbering footage
            raise SystemExit(f"internal error: proxy path equals the original {orig}")
        out.parent.mkdir(parents=True, exist_ok=True)
        partial = out.with_name(out.name + ".partial")
        partial.unlink(missing_ok=True)
        t0, last = time.monotonic(), [time.monotonic()]
        say(f"[{n}/{len(todo)}] {orig.name} -> {PROXY_DIRNAME}/{out.name}  ({it['action']})")

        def tick(frac: float, name=orig.name, n=n) -> None:
            if time.monotonic() - last[0] >= PROGRESS_EVERY_S:
                last[0] = time.monotonic()
                msg = f"proxies [{n}/{len(todo)}] {name}: {frac:.0%}"
                say(msg)
                progress.auto_log(ep, msg, "note", "proxies", refresh_checklist=False)
        try:
            try:
                _encode(orig, partial, codec, it["facts"], hw=use_hw, on_progress=tick)
            except ffutil.FFmpegError:
                if not use_hw:
                    raise
                partial.unlink(missing_ok=True)
                say("  hardware decode failed for this file; retrying in software")
                _encode(orig, partial, codec, it["facts"], hw=False, on_progress=tick)
            partial.replace(out)
            ok, why = verify(orig, out, codec)
            if not ok:
                out.unlink(missing_ok=True)
                raise ffutil.FFmpegError(f"{out.name} failed verification: {why}")
            result["made"].append(str(out))
            say(f"  done in {time.monotonic() - t0:.0f}s, {gb(out.stat().st_size) if out.exists() else '?'}")
        except Exception as e:                                   # one bad file must not lose the rest; the report says which
            partial.unlink(missing_ok=True)
            result["failed"].append({"original": str(orig), "error": str(e)[:600]})
            say(f"  FAILED {orig.name}: {str(e)[:200]}")
    bad = {f["original"] for f in result["failed"]}
    write_json(ep.artifact("proxies.json"), {"codec": codec, "height": height,
                                              "files": {i["original"]: i["proxy"] for i in plan["items"] if i["original"] not in bad}})
    progress.auto_log(ep, f"proxies: {len(result['made'])} made, {len(result['kept'])} kept, {len(result['failed'])} failed",
                      "done" if not result["failed"] else "blocker", "proxies")
    return result


def proxy_map(ep: Episode, doc: dict) -> tuple[dict[str, str], list[str]]:
    """({original abs path: proxy abs path} for every footage file of the edit that has a VERIFIED proxy, originals without one)."""
    found: dict[str, str] = {}
    missing: list[str] = []
    for f in footage_used(ep, doc):
        p = proxy_path(ep, f)
        if any(verify(f, p, c)[0] for c in PROFILES):
            found[str(f)] = str(p)
        else:
            missing.append(str(f))
    return found, missing
