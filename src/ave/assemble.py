"""Assembly edit: the synced, shot/reverse-shot switched program with NOTHING removed.

cutlist.json (plan-cuts) -> edits/assembly.vN.json (EditDoc). The dialogue track is one uncut master from the
transcribed (reference) source so the human hears the whole conversation; cleanup comes in the next stage.

Sessions (field note N1): `build(ep, session="s1")` turns work/cutlist.s1.json into the NAMED edit `assembly.s1`. It covers only the
spans where that session's cameras exist (the cut list's `regions`); lead-in, dead space and tail are excluded, and the doc says so
(`session`, `excluded`). `build_combined` joins every session with those gaps removed, as the unnamed `assembly`.

Trim (field note N8): `episode.json` `trim: {"s1": {"in": e, "out": e}}` (episode time, recorded by the human with `ave trim set`) shortens a
session's span to the real start/end. It is applied here, to the cut list, so the assembly is shorter and still validated; the trimmed-away
parts join `excluded`. The cut list itself is not edited.
"""

from __future__ import annotations

from . import cutplan, editdoc, sessions, trim
from .episode import Episode, read_json


def _channel_map(media: dict, f: str) -> list[dict] | None:
    """media.json's channel_map for file f when some range has a dead channel (all-mix needs no folding)."""
    cm = (media.get(f) or {}).get("channel_map")
    return cm if cm and any(m["use"] != "mix" for m in cm) else None


def _build_from_cut(ep: Episode, cut: dict, canvas: tuple[int, int]) -> dict:
    meta = read_json(ep.meta_path)
    cams = {k: v for k, v in (meta.get("cameras") or {}).items() if "cameras" not in cut or k in cut["cameras"]}
    sync = read_json(ep.artifact("sync.json"))["files"] if ep.artifact("sync.json").exists() else {}
    media = {m["file"]: m for m in read_json(ep.artifact("media.json"))["files"]}

    sources: dict[str, dict] = {}
    for key, info in cams.items():
        f = info["file"]
        sources[key] = {"file": f, "kind": media[f]["kind"], "start_offset": float(sync.get(f, {}).get("start_offset", 0.0)),
                        "drift_ppm": float(sync.get(f, {}).get("drift_ppm", 0.0) or 0.0), "role": info.get("role")}
        if sync.get(f, {}).get("sync_map"):
            sources[key]["sync_map"] = sync[f]["sync_map"]
    audio_file = cut["audio"]["file"]
    a_key = next((k for k, s in sources.items() if s["file"] == audio_file), None)
    if a_key is None:  # dedicated recorder not listed as a camera
        a_key = "M"
        sources[a_key] = {"file": audio_file, "kind": media[audio_file]["kind"],
                          "start_offset": float(cut["audio"]["start_offset"]),
                          "drift_ppm": float(cut["audio"].get("drift_ppm", 0.0) or 0.0), "role": "audio"}
        if cut["audio"].get("sync_map"):
            sources[a_key]["sync_map"] = cut["audio"]["sync_map"]
    for s_ in sources.values():
        cm = _channel_map(media, s_["file"])
        if cm:
            s_["channel_map"] = cm

    regions = cut.get("regions") or [[0.0, float(cut["timeline_seconds"])]]
    tm = editdoc.TimeMap(regions)
    dur = tm.duration
    doc = editdoc.new_doc("assembly", cut["fps"], canvas, sources, dur)
    cam_of_file = {s["file"]: k for k, s in sources.items()}
    for s in cut["shots"]:
        o0, o1 = s.get("o0", s["start_s"]), s.get("o1", s["end_s"])
        doc["tracks"]["video"].append({
            "id": f"v{s['n']}", "src": cam_of_file[s["file"]], "o0": o0, "o1": o1, "e0": s["start_s"],
            "crop": None, "zoom": 1.0, "reason": s["reason"], "confidence": 1.0, "speaker": s["speaker"]})
    vt = doc["tracks"]["video"]
    for a, b in zip(vt, vt[1:]):     # the cut list rounds to ms: make abutting shots exactly contiguous
        b["o0"] = a["o1"]
    vt[0]["o0"] = 0.0
    vt[-1]["o1"] = dur             # snap the last shot to the exact duration
    for n, (a, b) in enumerate(regions, 1):
        o = tm.o_starts[n - 1]
        doc["tracks"]["dialogue"].append({"id": f"d{n}", "src": a_key, "o0": o, "o1": o + (b - a), "e0": a,
                                          "xfade_ms": 0, "gain_db": 0.0, "reason": "uncut master" if len(regions) == 1 else "uncut master, one span"})
    doc["tracks"]["dialogue"][-1]["o1"] = dur
    if cut.get("session"):
        doc["session"] = cut["session"]
    if cut.get("trim"):
        doc["trim"] = cut["trim"]
    if cut.get("excluded"):
        doc["excluded"] = cut["excluded"]
        doc["notes"] = (f"session {cut.get('session')}: covers {', '.join(f'{a:.1f}-{b:.1f}s' for a, b in regions)} of episode time; "
                        "regions no camera covers are excluded." + (f" Trimmed to the human-approved in/out ({cut['trim']})." if cut.get("trim") else ""))
    elif cut.get("trim"):
        doc["notes"] = f"trimmed to the human-approved in/out {cut['trim']}; the rest is excluded."
    return doc


def apply_trim(cut: dict, rec: dict | None, label: str = "this edit") -> dict:
    """The cut list limited to episode time [rec.in, rec.out]: regions intersected, shots clipped (dropped when nothing is left), output times
    recomputed, trimmed-away spans added to `excluded`. A trim that leaves no picture is an error that names the fix. No trim: the cut unchanged."""
    if not rec or (rec.get("in") is None and rec.get("out") is None):
        return cut
    lo = float(rec["in"]) if rec.get("in") is not None else float("-inf")
    hi = float(rec["out"]) if rec.get("out") is not None else float("inf")
    regions = [[max(a, lo), min(b, hi)] for a, b in cut["regions"]]
    regions = [[a, b] for a, b in regions if b - a >= cutplan.MIN_REGION_S]
    if not regions:
        raise SystemExit(f"The trim for {label} (in {rec.get('in')}, out {rec.get('out')}) leaves no picture: the cameras cover "
                         f"{', '.join(f'{a:.1f}-{b:.1f}s' for a, b in cut['regions'])}. Fix it with `ave trim set <project> <session> --in S --out S`, "
                         "or `ave trim clear <project> <session>`.")
    tm = editdoc.TimeMap(regions)
    shots = []
    for s in cut["shots"]:
        a, b = max(s["start_s"], lo), min(s["end_s"], hi)
        k = next((i for i, (ra, rb) in enumerate(regions) if ra - 1e-6 <= a and b <= rb + 1e-6), None)
        if k is None or b - a < 1e-3:
            continue
        o0 = tm.o_starts[k] + a - regions[k][0]
        stale = ("record_frame", "source_start_frame", "source_end_frame", "frames")     # frame numbers belong to the untrimmed cut list
        shots.append({**{x: y for x, y in s.items() if x not in stale}, "start_s": a, "end_s": b, "o0": o0, "o1": o0 + (b - a)})
    for n, s in enumerate(shots, 1):
        s["n"] = n
    extent = max([b for _, b in cut["regions"]] + [b for _, b in cut.get("excluded") or []])
    return {**cut, "regions": regions, "shots": shots, "timeline_seconds": tm.duration, "trim": {k: rec[k] for k in ("in", "out", "by", "at") if k in rec},
            "excluded": [[round(a, 3), round(b, 3)] for a, b in cutplan.complement([tuple(r) for r in regions], 0.0, extent)]}


def build(ep: Episode, canvas: tuple[int, int] = (1920, 1080), session: str | None = None) -> dict:
    p = ep.artifact(sessions.cutlist_name(session))
    if not p.exists():
        raise SystemExit(f"No cut list for {'session ' + session if session else 'this episode'}. Run: ave plan-cuts {ep.root}"
                         + (f" --session {session}" if session else ""))
    cut = apply_trim(read_json(p), trim.get(ep, read_json(ep.meta_path), session), f"session {session}" if session else "this episode")
    return _build_from_cut(ep, cut, canvas)


def build_combined(ep: Episode, canvas: tuple[int, int] = (1920, 1080)) -> dict:
    """Every session's regions joined in episode order with the uncovered gaps removed (TimeMap), one unnamed assembly."""
    ids = sessions.session_ids(ep)
    meta = read_json(ep.meta_path)
    cuts = []
    for sid in ids:
        p = ep.artifact(sessions.cutlist_name(sid))
        if not p.exists():
            raise SystemExit(f"Session {sid} has no cut list yet. Run: ave plan-cuts {ep.root} --session {sid}")
        cuts.append(apply_trim(read_json(p), trim.get(ep, meta, sid), f"session {sid}"))
    regions = sorted(([a, b] for c in cuts for a, b in c["regions"]))
    tm = editdoc.TimeMap([tuple(r) for r in regions])
    shots = []
    for c in cuts:
        for s in c["shots"]:
            k = max(i for i, (a, _) in enumerate(tm.keeps) if a <= s["start_s"] + 1e-6)
            o0 = tm.o_starts[k] + s["start_s"] - tm.keeps[k][0]
            shots.append({**s, "o0": o0, "o1": o0 + (s["end_s"] - s["start_s"])})
    shots.sort(key=lambda s: s["o0"])
    for n, s in enumerate(shots, 1):
        s["n"] = n
    merged = {**cuts[0], "session": None, "regions": regions, "shots": shots, "timeline_seconds": tm.duration,
              "cameras": sorted({c for x in cuts for c in x["cameras"]}),
              "excluded": [[round(a, 3), round(b, 3)] for a, b in cutplan.complement(
                  [tuple(r) for r in regions], 0.0, max(b for _, b in regions))]}
    doc = _build_from_cut(ep, merged, canvas)
    doc["notes"] = f"combined: sessions {', '.join(ids)} joined with the uncovered gaps removed; episode time is preserved in each segment's e0."
    doc["sessions"] = ids
    return doc
