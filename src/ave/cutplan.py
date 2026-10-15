"""Shot / reverse-shot planner: transcript + sync + camera roles -> work/cutlist.json.

Pure planning. It decides WHICH camera is on screen WHEN; it never touches media or Resolve.
The cut list is expressed in the exact terms the Resolve MCP's `append_to_timeline`
`clip_infos` expect (source frames, record frame, track), so building the timeline is one call.

Taste lives in `CutPolicy` (numbers) and `should_cut_to_listener` (the one judgment call).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from . import config, editdoc
from .episode import Episode, read_json, write_json

DEFAULT_POLICY = config.REPO_ROOT / "config" / "cutplan.default.yaml"


@dataclass
class CutPolicy:
    """Every number the planner uses. Defaults equal config/cutplan.default.yaml (a test keeps them in step); `load_policy` reads the YAML."""
    lead_s: float = 0.25            # cut to the new speaker this long BEFORE they start (reads as natural)
    min_shot_s: float = 1.5         # never leave a shot sooner than this; delays the cut instead
    interjection_max_s: float = 1.2  # an utterance this short from the off-camera person is an "interjection"
    interjection_max_words: int = 3
    hold_wide_over_s: float = 0.0   # unused (kept for old cut lists): long holds are broken by the cleanup pacing pass
    legacy_never_cut_to_listener: bool = False
    reaction_min_s: float = 0.8     # interjection.reaction_min_s
    interjection_min_margin_db: float = 6.0
    interjection_require_margin: bool = False
    min_hold_before_s: float = 6.0
    laugh_min_s: float = 1.2
    ignore_words: tuple = ("mm-hm", "mm-hmm", "mhm", "mm", "hmm", "uh-huh", "uh huh", "yeah", "yep", "yes", "right", "okay", "ok", "sure", "exactly")
    wide_enabled: bool = True
    wide_opening_s: float = 3.0
    wide_crosstalk_min_s: float = 2.0


# YAML key -> CutPolicy field. A typo in a policy file is an error naming the key, never a silently ignored setting.
_YAML_FIELDS = {
    ("lead_s",): "lead_s", ("min_shot_s",): "min_shot_s", ("hold_wide_over_s",): "hold_wide_over_s",
    ("legacy_never_cut_to_listener",): "legacy_never_cut_to_listener",
    ("interjection", "max_s"): "interjection_max_s", ("interjection", "max_words"): "interjection_max_words",
    ("interjection", "reaction_min_s"): "reaction_min_s", ("interjection", "min_margin_db"): "interjection_min_margin_db",
    ("interjection", "require_margin"): "interjection_require_margin", ("interjection", "min_hold_before_s"): "min_hold_before_s",
    ("interjection", "laugh_min_s"): "laugh_min_s", ("interjection", "ignore_words"): "ignore_words",
    ("wide", "enabled"): "wide_enabled", ("wide", "opening_s"): "wide_opening_s", ("wide", "crosstalk_min_s"): "wide_crosstalk_min_s",
}


def _flatten(d: dict, prefix: tuple = ()) -> dict[tuple, object]:
    out = {}
    for k, v in (d or {}).items():
        if k == "version" and not prefix:
            continue
        if isinstance(v, dict):
            out.update(_flatten(v, prefix + (k,)))
        else:
            out[prefix + (k,)] = v
    return out


def _policy_from(data: dict) -> CutPolicy:
    kw = {}
    for key, val in _flatten(data).items():
        if key not in _YAML_FIELDS:
            raise ValueError(f"cutplan policy: unknown key {'.'.join(key)!r}")
        if _YAML_FIELDS[key] == "ignore_words":
            if any(not isinstance(x, str) for x in val):
                raise ValueError("cutplan policy interjection.ignore_words: every word must be a string (quote \"yes\" and \"no\": YAML reads them as booleans)")
            val = tuple(val)
        kw[_YAML_FIELDS[key]] = val
    pol = CutPolicy(**kw)
    for name in ("lead_s", "min_shot_s", "interjection_max_s", "reaction_min_s", "min_hold_before_s", "laugh_min_s", "wide_opening_s", "wide_crosstalk_min_s"):
        if getattr(pol, name) < 0:
            key = next(".".join(k) for k, f in _YAML_FIELDS.items() if f == name)
            raise ValueError(f"cutplan policy {key} must be >= 0 (got {getattr(pol, name)})")
    if pol.min_shot_s <= 0:
        raise ValueError("cutplan policy min_shot_s must be > 0")
    return pol


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_policy(path: Path | str | None = None) -> CutPolicy:
    """config/cutplan.default.yaml, with the YAML at `path` (if any) merged over it. Validated."""
    data = yaml.safe_load(DEFAULT_POLICY.read_text())
    if path:
        data = _merge(data, yaml.safe_load(Path(path).read_text()) or {})
    return _policy_from(data)


def resolve_policy(ep: Episode, explicit: Path | str | None = None, home: Path | None = None) -> tuple[CutPolicy, str]:
    """--policy > <ep>/policy/cutplan.yaml > ~/.config/ave/cutplan.yaml > default. Returns (policy, source label)."""
    home = Path(home) if home else Path.home()
    if explicit:
        if not Path(explicit).is_file():
            raise FileNotFoundError(f"policy file not found: {explicit}")
        return load_policy(explicit), str(Path(explicit).resolve())
    for cand in (ep.root / "policy" / "cutplan.yaml", home / ".config" / "ave" / "cutplan.yaml"):
        if cand.is_file():
            return load_policy(cand), str(cand)
    return load_policy(), str(DEFAULT_POLICY)


_TOKEN = re.compile(r"[^a-z0-9'\- ]+")


def _tokens(text: str) -> list[str]:
    return [t.strip("-") for t in _TOKEN.sub(" ", text.lower()).split() if t.strip("-")]


def is_backchannel(text: str, policy: CutPolicy) -> bool:
    """Every word of the utterance is a listener noise from `ignore_words` ("Mm-hmm.", "yeah yeah", "uh huh")."""
    ignore = {" ".join(_tokens(w)) for w in policy.ignore_words}
    toks, i = _tokens(text), 0
    if not toks:
        return False
    while i < len(toks):
        if " ".join(toks[i:i + 2]) in ignore and i + 1 < len(toks):
            i += 2
        elif toks[i] in ignore:
            i += 1
        else:
            return False
    return True


def is_interjection(utt: dict, policy: CutPolicy) -> bool:
    return (utt["end"] - utt["start"]) <= policy.interjection_max_s or len(utt["text"].split()) <= policy.interjection_max_words


def should_cut_to_listener(utt: dict, current_shot: dict, policy: CutPolicy) -> bool:
    """A short interjection ("yeah", "mm-hm", a laugh) from the person who is NOT on screen.

    Return True to cut to them (a reaction shot), False to stay on the current speaker.

    utt:          {"speaker", "start", "end", "text", "margin_db"?}    the interjection; margin_db = how far it is above the other mic
    current_shot: {"camera", "speaker", "start", "end", "held_s"}      what is on screen now; held_s = how long this shot has already been up
    policy:       the CutPolicy numbers (config/cutplan.default.yaml `interjection:`)

    Cut when it lasts at least `reaction_min_s`, is clearly on the listener's own channel (`margin_db` >= `min_margin_db`; with no channel
    data it is accepted unless `interjection_require_margin`), and the shot has been up at least `min_hold_before_s`. Backchannel words
    (`ignore_words`) stay on the speaker unless they last `laugh_min_s` (a laugh). `legacy_never_cut_to_listener` restores "never".
    """
    if policy.legacy_never_cut_to_listener:
        return False
    dur = utt["end"] - utt["start"]
    if dur < policy.reaction_min_s - 1e-9:
        return False
    if is_backchannel(utt["text"], policy) and dur < policy.laugh_min_s - 1e-9:
        return False
    margin = utt.get("margin_db")
    if margin is None:
        if policy.interjection_require_margin:
            return False
    elif margin < policy.interjection_min_margin_db:
        return False
    return current_shot["held_s"] >= policy.min_hold_before_s - 1e-9


def plan(utterances: list[dict], speaker_cam: dict[str, str], coverage: dict[str, tuple[float, float]],
         timeline_end: float, policy: CutPolicy | None = None, timeline_start: float = 0.0, wide: str | None = None) -> list[dict]:
    """Return contiguous shots [{camera, speaker, start, end, reason}] covering [timeline_start, timeline_end].

    speaker_cam: speaker id -> camera key.   coverage: camera key -> (first_s, last_s) on the timeline.
    wide: key of a `role: wide` camera (it frames nobody): used for an establishing opening and for crosstalk, and as a last-resort
    fallback when no speaker camera is recording. Without it the plan is exactly the two-camera shot/reverse-shot.
    """
    policy = policy or CutPolicy()
    if not utterances:
        raise ValueError("no utterances to plan from")
    cams = set(speaker_cam.values())
    wide = wide if wide in coverage and wide not in cams else None
    shots: list[dict] = []

    def covers(cam: str, t: float) -> bool:
        lo, hi = coverage[cam]
        return lo <= t <= hi

    for utt in utterances:
        spk = utt["speaker"]
        cam = speaker_cam.get(spk)
        if cam is None:
            continue  # unmapped speaker (e.g. off-mic noise): stay where we are
        if not shots:
            shots.append({"camera": cam, "speaker": spk, "start": timeline_start, "end": utt["end"], "reason": "opening"})
            continue
        cur = shots[-1]
        if cam == cur["camera"]:
            cur["end"] = max(cur["end"], utt["end"])
            continue
        if is_interjection(utt, policy):
            view = {"camera": cur["camera"], "speaker": cur["speaker"], "start": cur["start"], "end": cur["end"],
                    "held_s": utt["start"] - cur["start"]}
            if not should_cut_to_listener(utt, view, policy):
                cur["end"] = max(cur["end"], utt["end"])
                continue
            reason = "reaction"
        else:
            reason = "speaker change"
        cut_at = max(utt["start"] - policy.lead_s, cur["start"] + policy.min_shot_s)
        cut_at = min(cut_at, utt["end"] - 0.05)  # never push a cut past the end of the line that caused it
        if cut_at <= cur["start"]:
            cur["end"] = max(cur["end"], utt["end"])
            continue
        cur["end"] = cut_at
        shots.append({"camera": cam, "speaker": spk, "start": cut_at, "end": utt["end"], "reason": reason})

    # Make contiguous and run to the end of the timeline.
    for a, b in zip(shots, shots[1:]):
        a["end"] = b["start"]
    shots[-1]["end"] = timeline_end

    # A camera may not exist yet / any more at a given moment: fall back to one that does.
    fixed: list[dict] = []
    for s in shots:
        if covers(s["camera"], s["start"]) and covers(s["camera"], s["end"]):
            fixed.append(s)
            continue
        alt = next((c for c in [*sorted(cams), *([wide] if wide else [])] if c != s["camera"] and covers(c, s["start"]) and covers(c, s["end"])), None)
        if alt is None:
            raise ValueError(f"no camera covers {s['start']:.2f}-{s['end']:.2f}s; check sync.json / coverage")
        fixed.append({**s, "camera": alt, "reason": s["reason"] + f" (fallback: {s['camera']} not recording)"})
    # Fallbacks can leave two neighbours on the same camera; that is one shot, not a cut.
    merged = _merge_shots(fixed)
    if wide and policy.wide_enabled:
        merged = _merge_shots(_wide_shots(merged, utterances, speaker_cam, coverage, wide, policy, timeline_start, timeline_end))
    return merged


def _merge_shots(shots: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for s in shots:
        if merged and merged[-1]["camera"] == s["camera"]:
            merged[-1]["end"] = s["end"]
        else:
            merged.append(s)
    return merged


def _overlay_wide(shots: list[dict], a: float, b: float, wide: str, reason: str, min_shot: float) -> list[dict]:
    """Show `wide` over [a, b]. A leftover of the neighbouring shot shorter than `min_shot` is absorbed into the wide, never left as a flash."""
    for s in shots:
        if s["start"] < a < s["end"] and a - s["start"] < min_shot:
            a = s["start"]
        if s["start"] < b < s["end"] and s["end"] - b < min_shot:
            b = s["end"]
    out: list[dict] = []
    for s in shots:
        if s["end"] <= a + 1e-9 or s["start"] >= b - 1e-9:
            out.append(s)
            continue
        if s["start"] < a - 1e-9:
            out.append({**s, "end": a})
        if not any(o["camera"] == wide and o["start"] == a for o in out):
            out.append({"camera": wide, "speaker": None, "start": a, "end": b, "reason": reason})
        if s["end"] > b + 1e-9:
            out.append({**s, "start": b})
    return out


def _wide_shots(shots: list[dict], utts: list[dict], speaker_cam: dict[str, str], coverage: dict, wide: str, pol: CutPolicy,
                lo: float, hi: float) -> list[dict]:
    """The wide camera's two jobs in the planner: an establishing shot at the start of the region, and crosstalk (two mapped speakers
    talking over each other for at least `wide_crosstalk_min_s`). Pacing inside long single-speaker holds is the cleanup pacing pass."""
    wlo, whi = coverage[wide]

    def ok(a, b):
        return wlo - 1e-6 <= a and b <= whi + 1e-6

    out = [dict(s) for s in shots]
    first = out[0]
    if pol.wide_opening_s > 0 and first["camera"] != wide and ok(first["start"], first["start"] + pol.wide_opening_s):
        b = first["start"] + pol.wide_opening_s
        if first["end"] - b < pol.min_shot_s:
            b = first["end"]
        if b - first["start"] >= min(pol.wide_opening_s, pol.min_shot_s) and ok(first["start"], b) and (len(out) > 1 or b < first["end"]):
            out = _overlay_wide(out, first["start"], b, wide, "establishing wide", pol.min_shot_s)
    mapped = sorted((u for u in utts if speaker_cam.get(u["speaker"])), key=lambda u: u["start"])
    for j, v in enumerate(mapped):
        for u in mapped[max(0, j - 8):j]:
            if u["speaker"] == v["speaker"]:
                continue
            a, b = max(v["start"], u["start"], lo), min(u["end"], v["end"], hi)
            if b - a >= pol.wide_crosstalk_min_s - 1e-9 and ok(a, b):
                out = _overlay_wide(out, a, b, wide, "crosstalk wide", pol.min_shot_s)
    return out


def to_frames(shots: list[dict], fps: float, offsets: dict[str, float], files: dict[str, str],
              drifts: dict[str, float] | None = None, sources: dict[str, dict] | None = None) -> list[dict]:
    """Convert seconds to frames with consecutive rounding so record frames are exactly contiguous.
    `drifts` (camera -> ppm) makes the source frame follow editdoc.src_time (default: no drift).
    A shot may carry `o0`/`o1` (output seconds, when the cut list skips uncovered regions); otherwise output time == episode time."""
    out = []
    for i, s in enumerate(shots):
        o0, o1 = s.get("o0", s["start"]), s.get("o1", s["end"])
        rec = round(o0 * fps)
        nxt = round(o1 * fps)
        n = max(1, nxt - rec)
        src = (sources or {}).get(s["camera"]) or {"start_offset": offsets[s["camera"]], "drift_ppm": (drifts or {}).get(s["camera"], 0.0)}
        src_start = round(editdoc.src_time(src, s["start"]) * fps)
        out.append({
            "n": i + 1, **{k: s[k] for k in ("camera", "speaker", "reason")},
            "file": files[s["camera"]],
            "start_s": round(s["start"], 3), "end_s": round(s["end"], 3),
            "o0": round(o0, 3), "o1": round(o1, 3),
            "record_frame": rec,
            "source_start_frame": src_start,
            "source_end_frame": src_start + n - 1,   # inclusive, as Resolve's startFrame/endFrame
            "frames": n,
        })
    return out


# ---- regions: where an episode actually has picture ---------------------------------------------------------------

MIN_REGION_S = 0.5


def regions_from_coverage(coverage: dict[str, tuple[float, float]], lo: float, hi: float) -> list[tuple[float, float]]:
    """Episode-time spans (sorted, disjoint) where at least one camera exists, clipped to [lo, hi] (the extent of the master audio
    and transcript). Everything else (lead-in, dead space, tail) is EXCLUDED from the assembly, never an error."""
    spans = sorted((max(a, lo), min(b, hi)) for a, b in coverage.values() if min(b, hi) - max(a, lo) > 0)
    out: list[list[float]] = []
    for a, b in spans:
        if out and a <= out[-1][1] + 1e-6:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out if b - a >= MIN_REGION_S]


def complement(regions: list[tuple[float, float]], lo: float, hi: float) -> list[tuple[float, float]]:
    out, pos = [], lo
    for a, b in regions:
        if a - pos > 1e-6:
            out.append((pos, a))
        pos = max(pos, b)
    if hi - pos > 1e-6:
        out.append((pos, hi))
    return out


def _elementary(coverage: dict[str, tuple[float, float]], a: float, b: float) -> list[tuple[float, float]]:
    """Split [a, b] wherever a camera starts or stops, so each piece has a constant set of cameras."""
    cuts = sorted({a, b, *[t for lo, hi in coverage.values() for t in (lo, hi) if a < t < b]})
    return list(zip(cuts, cuts[1:]))


def _plan_region(utts: list[dict], speaker_cam: dict[str, str], coverage: dict[str, tuple[float, float]], a: float, b: float,
                 policy: CutPolicy | None, wide: str | None = None) -> list[dict]:
    """Shots for one region [a, b]. Utterances are clipped to it; a region with no speech gets one shot on any camera that covers it.
    If no single alternative camera covers a shot (cameras start/stop mid-region), the region is planned piece by piece."""
    clipped = [{**u, "start": max(u["start"], a), "end": min(u["end"], b)} for u in utts if u["end"] > a and u["start"] < b]
    clipped = [u for u in clipped if u["end"] - u["start"] > 0.05]
    cams = sorted(c for c in set(speaker_cam.values()) if c in coverage) + ([wide] if wide in coverage else [])

    def silent(x: float, y: float) -> list[dict]:
        cam = next((c for c in cams if coverage[c][0] <= x + 1e-6 and coverage[c][1] >= y - 1e-6), None)
        if cam is None:
            raise ValueError(f"no camera covers {x:.2f}-{y:.2f}s")
        return [{"camera": cam, "speaker": None, "start": x, "end": y, "reason": "no speech in this region"}]

    if not any(speaker_cam.get(u["speaker"]) for u in clipped):
        try:
            return silent(a, b)
        except ValueError:
            return [sh for x, y in _elementary(coverage, a, b) for sh in silent(x, y)]
    try:
        return plan(clipped, speaker_cam, coverage, b, policy, timeline_start=a, wide=wide)
    except ValueError:
        shots: list[dict] = []
        for x, y in _elementary(coverage, a, b):
            part = [{**u, "start": max(u["start"], x), "end": min(u["end"], y)} for u in clipped if u["end"] > x and u["start"] < y]
            part = [u for u in part if u["end"] - u["start"] > 0.05 and speaker_cam.get(u["speaker"])]
            shots += plan(part, speaker_cam, coverage, y, policy, timeline_start=x, wide=wide) if part else silent(x, y)
        merged: list[dict] = []
        for sh in shots:
            if merged and merged[-1]["camera"] == sh["camera"]:
                merged[-1]["end"] = sh["end"]
            else:
                merged.append(sh)
        return merged


def _duration(media: dict, f: str) -> float:
    return float(media[f]["duration"])


def _with_margins(utts: list[dict], words: list[dict]) -> list[dict]:
    """Give each utterance `margin_db`: the weakest `speaker_margin_db` of its words (how clearly it is on its own mic; absent without channel data)."""
    out = []
    for u in utts:
        ms = [w["speaker_margin_db"] for w in words[u.get("first_word", 0):u.get("last_word", -1) + 1] if w.get("speaker_margin_db") is not None]
        out.append({**u, "margin_db": min(ms)} if ms else u)
    return out


def build_cutlist(ep: Episode, policy: CutPolicy | None = None, session: str | None = None, policy_path: Path | str | None = None,
                  home: Path | None = None) -> dict:
    """Plan shots and write the cut list.

    `policy`: a CutPolicy; else resolved from `policy_path` > <ep>/policy/cutplan.yaml > ~/.config/ave/cutplan.yaml > config/cutplan.default.yaml.
    A camera with `role: wide` (not mapped to any speaker) is the planner's wide: establishing shot, crosstalk, last-resort fallback.

    No `session` on an ordinary (single-session) episode: the original behaviour, work/cutlist.json over [0, earliest camera end].
    `session="s1"`: work/cutlist.s1.json for that session's cameras only. Episode time stays the recorder's timeline; the cut list covers
    `regions` (union of the session's camera coverage, drift included, clipped to the audio and transcript) and EXCLUDES the rest.
    """
    from . import sessions
    if policy is None:
        try:
            policy, policy_source = resolve_policy(ep, policy_path, home)
        except (FileNotFoundError, ValueError) as e:
            raise SystemExit(str(e))
    else:
        policy_source = "given in code"
    meta = read_json(ep.meta_path)
    cameras = meta.get("cameras") or {}
    speakers = meta.get("speakers") or {}
    if not cameras or not speakers:
        raise SystemExit("episode.json needs `cameras` and `speakers` filled in. See skill: map-cameras-speakers.")
    cam_session = sessions.camera_sessions(ep, meta)
    multi = len(set(cam_session.values())) > 1
    if session is None and multi:
        raise SystemExit(f"This episode has sessions {sessions.session_ids(ep, meta)}: plan one with --session <id> (or run `ave plan-cuts` with no flag to plan all).")
    if session is not None:
        if session not in set(cam_session.values()):
            raise SystemExit(f"No cameras are assigned to session {session!r} in episode.json (known: {sorted(set(cam_session.values())) or 'none'}). "
                             "Set cameras.<key>.session.")
        cameras = {c: i for c, i in cameras.items() if cam_session.get(c) == session}
    transcript = read_json(ep.artifact("transcript.json"))
    sync = read_json(ep.artifact("sync.json"))["files"] if ep.artifact("sync.json").exists() else {}
    media = {m["file"]: m for m in read_json(ep.artifact("media.json"))["files"]}

    speaker_cam = {spk: c for spk in speakers if (c := sessions.speaker_camera(meta, spk, session, cam_session)) and c in cameras}
    if not speaker_cam:
        raise SystemExit(f"No speaker is mapped to a camera of session {session!r}: set speakers.<id>.cameras.{session} in episode.json.")
    files = {c: info["file"] for c, info in cameras.items()}
    offsets = {c: sync.get(f, {"start_offset": 0.0})["start_offset"] for c, f in files.items()}
    drifts = {c: float(sync.get(f, {}).get("drift_ppm") or 0.0) for c, f in files.items()}
    srcs = {c: {"start_offset": offsets[c], "drift_ppm": drifts[c], "sync_map": sync.get(f, {}).get("sync_map")} for c, f in files.items()}
    coverage = {c: (editdoc.episode_time(srcs[c], 0.0), editdoc.episode_time(srcs[c], _duration(media, files[c]))) for c in files}

    audio_file = meta.get("audio_source") or transcript["source"]
    audio_off = sync.get(audio_file, {"start_offset": 0.0})["start_offset"]
    audio_drift = float(sync.get(audio_file, {}).get("drift_ppm") or 0.0)
    audio_src = {"start_offset": audio_off, "drift_ppm": audio_drift, "sync_map": sync.get(audio_file, {}).get("sync_map")}
    ref_cam = next((c for c, f in files.items() if f == transcript["source"]), next(iter(files)))
    vid = media[files[ref_cam]].get("video")
    if not vid or not vid.get("fps"):
        raise SystemExit(f"{files[ref_cam]} has no video fps; pick a video camera as the reference.")
    fps = vid["fps"]
    wide = next((c for c in sorted(cameras) if (cameras[c].get("role") or "").lower() == "wide" and c not in set(speaker_cam.values())), None)
    utts = _with_margins([u for u in transcript["utterances"] if u["speaker"] in speaker_cam], transcript["words"])

    if session is None:
        # legacy single-session behaviour, kept exactly: one region from 0 to the earliest end of the cameras that frame a speaker
        timeline_end = min(hi for c, (lo, hi) in coverage.items() if c in set(speaker_cam.values()))
        timeline_end = min(timeline_end, transcript.get("duration") or timeline_end)
        shots = plan(utts, speaker_cam, coverage, timeline_end, policy, wide=wide)
        regions = [(0.0, timeline_end)]
    else:
        a_lo = editdoc.episode_time(audio_src, 0.0)
        a_hi = editdoc.episode_time(audio_src, _duration(media, audio_file))
        hi = min(a_hi, transcript.get("duration") or a_hi)
        regions = regions_from_coverage(coverage, max(a_lo, 0.0), hi)
        if not regions:
            raise SystemExit(f"Session {session}: its cameras cover no part of the audio/transcript timeline. Check `ave sync` output "
                             f"(coverage {coverage}, audio {a_lo:.1f}-{hi:.1f}s).")
        shots = [sh for a, b in regions for sh in _plan_region(utts, speaker_cam, coverage, a, b, policy, wide)]

    # output time: regions are concatenated in order (identity for the single legacy region)
    tm = editdoc.TimeMap(regions)
    for sh in shots:
        k = max(i for i, (a, _) in enumerate(tm.keeps) if a <= sh["start"] + 1e-6)
        sh["o0"] = tm.o_starts[k] + sh["start"] - tm.keeps[k][0]
        sh["o1"] = tm.o_starts[k] + sh["end"] - tm.keeps[k][0]
    frames = to_frames(shots, fps, offsets, files, drifts, srcs)
    total = tm.duration
    extent = max(b for _, b in regions) if session is None else hi
    result = {
        "session": session,
        "fps": fps,
        "policy": asdict(policy),
        "policy_source": policy_source,
        "timeline_seconds": round(total, 3),
        "regions": [[round(a, 3), round(b, 3)] for a, b in regions],
        "excluded": [[round(a, 3), round(b, 3)] for a, b in complement(regions, 0.0, extent)] if session else [],
        "cameras": sorted(files),
        "audio": {"file": audio_file, "start_offset": audio_off, "drift_ppm": audio_drift,
                  **({"sync_map": audio_src["sync_map"]} if audio_src["sync_map"] else {})},
        "shots": frames,
        "stats": {"shots": len(frames), "avg_shot_s": round(total / len(frames), 2),
                  "shortest_s": min(round(s["end_s"] - s["start_s"], 2) for s in frames),
                  "reactions": sum(1 for s in frames if s["reason"] == "reaction"), "wide_shots": sum(1 for s in frames if wide and s["camera"] == wide)},
    }
    write_json(ep.artifact(sessions.cutlist_name(session)), result)
    ep.mark_stage("cutplan", shots=len(frames), **({"session": session} if session else {}))
    return result
