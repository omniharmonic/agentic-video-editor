#!/usr/bin/env python3
"""Execute a handoff plan (work/handoff_plan.json, see src/ave/handoff.py) in the OPEN Resolve project.

Run by `ave handoff` with the Resolve MCP's Python. Creates a NEW timeline; never edits an existing one.
Verified-on-readback: video item count/start/duration/name, audio items, gaps/overlaps. Exit 0 only if everything matches.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


APPEND_CHUNK = 60
IMPORT_WAIT_S = 240.0   # Resolve registers big files (a 2.4 GB WAV) ASYNCHRONOUSLY after import_to_pool returns
IMPORT_POLL_S = 3.0


def fail(msg: str, **extra) -> None:
    print(json.dumps({"ok": False, "error": msg, **extra}, indent=2, default=str))
    sys.exit(1)


def ok(result, what: str):
    if not isinstance(result, dict) or result.get("error") or result.get("success") is False:
        fail(f"{what} failed", detail=result)
    return result


def track_plans(plan: dict) -> dict[tuple[str, int], list[dict]]:
    """Layered handoff: every (kind, track_index) -> its planned items in timeline order. One entry per track, so the append, resume
    and read-back logic is the same for V1, the alternate angles, the dialogue clips, the baked master, music and overlays."""
    tp: dict[tuple[str, int], list[dict]] = {}
    L = plan["layers"]
    groups = [("video", plan["video"]), ("video", L["alt_video"]), ("audio", L["audio_clips"]),
              ("audio", [L["baked_master"]] if L.get("baked_master") else []), ("audio", plan["music"]), ("video", plan["overlays"])]
    for kind, items in groups:
        for it in items:
            tp.setdefault((kind, it["track_index"]), []).append(it)
    for v in tp.values():
        v.sort(key=lambda i: i["record_frame"])
    return tp


def matches(p_: dict, it: dict, tl0: int, kind: str) -> bool:
    """A placed item is the planned one: same start and length (and, for video, the same camera file)."""
    ok_ = int(it["start"]) == tl0 + p_["record_frame"] and int(it["duration"]) == p_["frames"]
    if kind == "video":
        ok_ = ok_ and it.get("name") in p_.get("names", [Path(p_["file"]).name])
    return ok_


def gaps_and_overlaps(items: list[dict]) -> tuple[int, int]:
    """(gaps, overlaps) between consecutive items of one track, from the read-back start/duration."""
    g = o = 0
    for a, b in zip(items, items[1:]):
        d = int(b["start"]) - (int(a["start"]) + int(a["duration"]))
        g += d > 0
        o += d < 0
    return g, o


PROXY_UNVERIFIED = ("UNVERIFIED in real Resolve: the link call and its read-back were checked against the bridge's answers only. Check once: "
                    "Media Pool > right-click a clip > Clip Attributes / the clip's Proxy column shows the proxy, then switch Resolve to "
                    "proxy playback (Playback > Proxy Mode). Renders must come from the originals: set Proxy Mode to off before Deliver.")


def link_proxies(server, plan: dict, ids: dict[str, str]) -> dict:
    """Link each review proxy to its imported ORIGINAL (MCP `link_proxy_checked`). The plan keeps naming the originals, so the timeline
    and every render use them; a proxy only changes what Resolve plays. A failed link is reported, never fatal."""
    failed, linked, verified = [], 0, 0
    for orig, proxy in sorted(plan["proxies"].items()):
        cid = ids.get(orig)
        if not cid:
            failed.append({"file": orig, "reason": "original not in the media pool"})
            continue
        try:
            res = server.media_pool(action="link_proxy_checked", params={"clip_id": cid, "proxy_path": proxy, "check_compatibility": True})
        except Exception as e:
            failed.append({"file": orig, "reason": f"{type(e).__name__}: {str(e)[:160]}"})
            continue
        if not isinstance(res, dict) or res.get("error") or res.get("success") is False:
            failed.append({"file": orig, "reason": str((res or {}).get("error") or "LinkProxyMedia returned false")[:200]})
            continue
        linked += 1
        status = ((res.get("verified_operation") or {}).get("verification_status")) or ""
        fields = (res.get("readback") or {}).get("proxy_fields") or {}
        if status == "readback_verified" or any(os.path.realpath(str(v)) == os.path.realpath(proxy) for v in fields.values()):
            verified += 1
    return {"requested": len(plan["proxies"]), "linked": linked, "readback_verified": verified, "failed": failed,
            "originals_in_timeline": True, "status": PROXY_UNVERIFIED}


def subtitle_instructions(sub: dict) -> list[str]:
    srt = sub.get("copy") or sub["srt"]
    return [f"The SRT is at {srt} ({sub.get('cues', '?')} cues). In Resolve, with the new timeline open:",
            "1. File > Import > Subtitle... and pick that file. It lands on a new subtitle track at the start of the current timeline.",
            f"2. Or drag {Path(srt).name} from the Media Pool onto the timeline (it creates a subtitle track).",
            "Styled captions stay burned in by FFmpeg; this track is for tweaking text and timing."]


def place_subtitles(server, plan: dict, pool) -> str:
    """W7, UNVERIFIED in real Resolve. Candidate route: MediaPool.ImportMedia(srt) then AppendToTimeline (reported working on Studio
    21.0.4.5, issue #169 of the MCP; never measured on free 21.0.4). Only attempted with --try-subtitles. Success is declared ONLY when
    the subtitle track now exists with as many items as the SRT has cues; anything else is reported as manual with exact steps."""
    sub = plan["subtitles"]
    srt = sub["srt"]
    if not sub.get("try"):
        return f"manual (not attempted; pass --try-subtitles to try): see subtitle_instructions for {Path(srt).name}"
    try:
        before = {k: int(server.timeline(action="get_track_count", params={"track_type": k}).get("count") or 0)
                  for k in ("video", "audio", "subtitle")}
        ok(server.media_storage(action="import_to_pool", params={"items": [srt]}), "importing SRT")
        sid = pool().get(os.path.realpath(srt))
        if not sid:
            return f"manual: the SRT did not appear in the media pool (see subtitle_instructions for {Path(srt).name})"
        res = server.media_pool(action="append_to_timeline", params={"clip_ids": [sid]})
        after = {k: int(server.timeline(action="get_track_count", params={"track_type": k}).get("count") or 0)
                 for k in ("video", "audio", "subtitle")}
        if after["video"] > before["video"] or after["audio"] > before["audio"]:
            return ("manual: the SRT landed on a VIDEO/AUDIO track instead of a subtitle track; delete that clip "
                    f"(see subtitle_instructions for {Path(srt).name})")
        if not after["subtitle"]:
            return f"manual: Resolve accepted the call but made no subtitle track ({_brief(res)}); see subtitle_instructions"
        got = server.timeline(action="get_items", params={"track_type": "subtitle", "track_index": 1}).get("items")
        want = int(sub.get("cues") or 0)
        if isinstance(got, list) and want and len(got) != want:
            return f"manual: subtitle track has {len(got)} items but the SRT has {want} cues (check it in Resolve; see subtitle_instructions)"
        n = f"{len(got)} cues" if isinstance(got, list) else "cue count not readable"
        return f"on the timeline ({after['subtitle']} subtitle track, {n}); UNVERIFIED route, check text and timing in Resolve"
    except SystemExit:
        raise
    except Exception as e:                      # never fail the handoff over captions
        return f"manual: {type(e).__name__}: {str(e)[:160]} (see subtitle_instructions for {Path(srt).name})"


def _brief(x) -> str:
    return json.dumps(x, default=str)[:160]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("--mcp-src", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    plan = json.loads(Path(args.plan).read_text())
    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "project": plan.get("project"), **{k: plan[k] for k in ("name", "fps", "timeline_frames")},
                          "video": len(plan["video"]), "audio": len(plan["audio"]), "music": len(plan["music"]),
                          "overlays": len(plan["overlays"]), "subtitles": bool(plan["subtitles"]), "try_subtitles": bool((plan["subtitles"] or {}).get("try")),
                          "proxies_to_link": len(plan.get("proxies") or {}), "warnings": plan["warnings"],
                          **({"layers": {"items_per_track": {f"{k}{i}": len(v) for (k, i), v in sorted(track_plans(plan).items())},
                                         "tracks": plan["layers"]["tracks"], "note": plan["layers"]["note"]}} if plan.get("layers") else {})},
                         indent=2))
        return

    os.environ.setdefault("DAVINCI_RESOLVE_BRIDGE", "1")
    sys.path.insert(0, args.mcp_src)
    import server  # noqa: E402

    name, fps = plan["name"], float(plan["fps"])
    ok(server.project_settings(action="get_name"), "reading the open project (is the bridge running and a project open?)")
    want = plan.get("project")
    if want:   # one Resolve project per episode: open it, or create it. Save the current one first so nothing is lost.
        cur = server.project_manager(action="get_current").get("name")
        if cur != want:
            server.project_manager(action="save")
            have_projects = server.project_manager(action="list").get("projects") or []
            names = [x if isinstance(x, str) else x.get("name") for x in have_projects]
            if want not in names:
                ok(server.project_manager(action="create", params={"name": want}), f"creating project {want!r}")
            ok(server.project_manager(action="load", params={"name": want}), f"opening project {want!r}")
            if server.project_manager(action="get_current").get("name") != want:
                fail(f"could not switch to project {want!r}", current=server.project_manager(action="get_current"))
    resume = 0                      # how many leading video items already exist and EXACTLY match the plan
    layered = bool(plan.get("layers"))
    tplans = track_plans(plan) if layered else {}
    resume_tracks: dict[tuple[str, int], int] = {}   # layered: items already placed per track (each must be a prefix of its own plan)

    def read_track(kind: str, idx: int) -> list[dict]:
        return sorted(server.timeline(action="get_items", params={"track_type": kind, "track_index": idx}).get("items", []),
                      key=lambda i: i["start"])

    existing = [t for t in (server.timeline(action="list").get("timelines") or []) if t.get("name") == name]
    if existing:
        # A failed earlier attempt can leave a partial timeline of this name. Resume it ONLY if what is there is exactly a prefix of
        # the plan (same start, length and camera) and nothing else was placed; anything else is the human's work: never touch it.
        server.timeline(action="set_current", params={"name": name})
        tl0 = int(server.timeline(action="get_start_frame").get("frame", 0))
        if layered:
            # every track must be a prefix of ITS OWN plan: V1 first, then each extra track; a track the plan does not use must be empty
            for kind in ("video", "audio"):
                n_t = int(server.timeline(action="get_track_count", params={"track_type": kind}).get("count") or 0)
                for idx in range(1, n_t + 1):
                    got, want = read_track(kind, idx), tplans.get((kind, idx), [])
                    if got and (len(got) > len(want) or not all(matches(p_, it, tl0, kind) for p_, it in zip(want, got))):
                        fail(f"a timeline named {name!r} already exists and its {kind} track {idx} is not a prefix of this plan; handoffs "
                             "never overwrite. Bump the edit version or pass a label.", items=len(got), planned=len(want))
                    if got:
                        resume_tracks[(kind, idx)] = len(got)
        else:
            v = read_track("video", 1)
            n_a = int(server.timeline(action="get_track_count", params={"track_type": "audio"}).get("count") or 0)
            other = sum(len(server.timeline(action="get_items", params={"track_type": "audio", "track_index": i}).get("items", [])) for i in range(1, n_a + 1))
            extra = int(server.timeline(action="get_track_count", params={"track_type": "video"}).get("count") or 0) > 1
            prefix_ok = len(v) <= len(plan["video"]) and all(
                int(it["start"]) == tl0 + p_["record_frame"] and int(it["duration"]) == p_["frames"]
                and it.get("name") in p_.get("names", [Path(p_["file"]).name]) for p_, it in zip(plan["video"], v))
            if other or extra or not prefix_ok:
                fail(f"a timeline named {name!r} already exists with content that is not a prefix of this plan; handoffs never overwrite. "
                     "Bump the edit version or pass a label.", video_items=len(v), other_items=other)
            resume = len(v)

    # Frame rate: only settable before the first timeline exists. Verify; refuse a mismatch instead of silently shifting cuts.
    have_tl = bool(server.timeline(action="list").get("timelines"))
    cur = float(server.project_settings(action="get_setting", params={"name": "timelineFrameRate"})["settings"])
    if abs(cur - fps) > 0.01:
        if have_tl:
            fail("project already has timelines at a different frame rate; open a fresh project (or one at the same fps)",
                 project_fps=cur, wanted=fps)
        ok(server.project_settings(action="set_setting", params={"name": "timelineFrameRate", "value": str(fps)}), "setting frame rate")
        if abs(float(server.project_settings(action="get_setting", params={"name": "timelineFrameRate"})["settings"]) - fps) > 0.01:
            fail("could not set the project frame rate", wanted=fps)
        w, h = plan["canvas"]["w"], plan["canvas"]["h"]
        server.project_settings(action="set_setting", params={"name": "timelineResolutionWidth", "value": str(w)})
        server.project_settings(action="set_setting", params={"name": "timelineResolutionHeight", "value": str(h)})

    def pool() -> dict[str, str]:
        probe = server.media_pool(action="probe_media_pool")
        clips = (probe.get("root") or {}).get("clips", []) + (probe.get("current_folder") or {}).get("clips", [])
        return {os.path.realpath(c["file_path"]): c["id"] for c in clips if c.get("file_path")}

    have = pool()
    missing = [f for f in plan["import"] if os.path.realpath(f) not in have]
    gone = [f for f in missing if not Path(f).exists()]
    if gone:
        fail("files to import are missing on disk", files=gone)
    if missing:
        ok(server.media_storage(action="import_to_pool", params={"items": missing}), "importing media")
        # The call returns before Resolve finishes registering large files (real episode: a 2.4 GB WAV was absent from the pool for
        # the first look, then present). Poll for every file instead of failing on the first read.
        deadline = time.monotonic() + IMPORT_WAIT_S
        have = pool()
        while any(os.path.realpath(f) not in have for f in missing) and time.monotonic() < deadline:
            time.sleep(IMPORT_POLL_S)
            have = pool()
    ids = {}
    for f in plan["import"]:
        cid = have.get(os.path.realpath(f))
        if not cid:
            fail("imported file not found in the media pool", file=f)
        ids[f] = cid

    proxy_report = link_proxies(server, plan, ids) if plan.get("proxies") else None

    if not existing:
        ok(server.media_pool(action="create_timeline", params={"name": name}), "creating the timeline")
    created_id = (server.timeline(action="get_current") or {}).get("id")

    def cleanup_incomplete(why: str) -> None:
        """A failed handoff leaves a PARTIAL timeline. We never delete it (Resolve gates deletes behind a human confirmation, and the
        partial work is safely resumable): say plainly what is there and how to continue."""
        try:
            n = len(server.timeline(action="get_items", params={"track_type": "video", "track_index": 1}).get("items", []))
        except BaseException:
            n = -1
        placed = {}
        if layered:
            for (k, i) in sorted(tplans):
                try:
                    placed[f"{k}{i}"] = f"{len(read_track(k, i))}/{len(tplans[(k, i)])}"
                except BaseException:
                    placed[f"{k}{i}"] = "?"
        print(json.dumps({"partial_timeline": name, "video_items_placed": n, "reason": why, **({"items_per_track": placed} if layered else {}),
                          "next": "re-run the same `ave handoff`: it verifies the placed items match the plan and resumes after them"}), file=sys.stderr)

    try:
        def ensure_tracks(kind: str, upto: int) -> None:
            while int(server.timeline(action="get_track_count", params={"track_type": kind}).get("count", 1)) < upto:
                ok(server.timeline(action="add_track", params={"track_type": kind}), f"adding {kind} track")

        def landed(item: dict) -> int:
            k = "video" if item["media_type"] == 1 else "audio"
            for attempt in range(4):                 # a read can hit a stale handle too; re-fetching is the documented remedy
                try:
                    return len(server.timeline(action="get_items", params={"track_type": k, "track_index": item["track_index"]}).get("items", []))
                except Exception:
                    if attempt == 3:
                        raise
                    time.sleep(1.0)

        def append(items: list[dict], what: str, already: int = 0) -> None:
            """Append in adaptive chunks. After ANY failure, re-read how many items actually landed on that track and resume from there,
            halving the chunk: a transient bridge error (`stale_handle`, null item id) never double-places or skips a clip."""
            if not items:
                return
            infos = [{"clip_id": ids[i["file"]], "start_frame": i["start_frame"], "end_frame": i["end_frame"],
                      "record_frame": i["record_frame"], "track_index": i["track_index"], "media_type": i["media_type"]} for i in items]
            base = landed(items[0]) - already            # items already on the track that are not ours (0 on a fresh timeline)
            i, size, stuck = 0, APPEND_CHUNK, 0
            while i < len(infos):
                try:
                    res = server.media_pool(action="append_to_timeline", params={"clip_infos": infos[i:i + size]})
                except Exception as e:      # the bridge can RAISE (stale_handle) instead of returning an error: same recovery path
                    res = {"error": f"{type(e).__name__}: {e}"}
                if isinstance(res, dict) and not res.get("error") and res.get("success") is not False:
                    i += size
                    stuck = 0
                    continue
                now = landed(items[0]) - base - already   # how many of OUR items are really there
                if now > i:                                # part of the failed chunk did land: resume after it
                    i, stuck = now, 0
                else:
                    size, stuck = max(1, size // 2), stuck + 1
                if stuck > 4:
                    fail(f"{what} failed repeatedly at item {i + 1} of {len(infos)}", detail=res)
            got = landed(items[0]) - base
            if got != len(infos) + already:
                fail(f"{what}: Resolve holds {got} items, expected {len(infos) + already}")

        if layered:
            ensure_tracks("video", max([1] + [i for (k, i) in tplans if k == "video"]))
            ensure_tracks("audio", max([1] + [i for (k, i) in tplans if k == "audio"]))
            for (kind, idx) in sorted(tplans, key=lambda ki: (ki[0] != "video", ki[1])):    # V1 first, then the other tracks in order
                items, done = tplans[(kind, idx)], resume_tracks.get((kind, idx), 0)
                if done < len(items):    # a track disabled by an earlier attempt is re-enabled while we fill it, disabled again below
                    server.timeline(action="set_track_enable", params={"track_type": kind, "index": idx, "enabled": True})
                append(items[done:], f"appending {kind} track {idx}", already=done)
        else:
            ensure_tracks("video", max([1] + [o["track_index"] for o in plan["overlays"]]))
            ensure_tracks("audio", max([1] + [m["track_index"] for m in plan["music"]]))
            append(plan["video"][resume:], "appending video", already=resume)
            append(plan["audio"], "appending dialogue audio")
            append(plan["music"], "appending music")
            for o in plan["overlays"]:
                append([o], f"appending overlay {o['id']}")
        if layered:     # name the layer tracks and switch the alternates / baked master OFF (after placement: placing needs them on)
            for kind in ("video", "audio"):
                for idx, meta in plan["layers"]["tracks"][kind].items():
                    server.timeline(action="set_track_name", params={"track_type": kind, "index": int(idx), "name": meta["name"]})
                    if meta["disabled"]:
                        ok(server.timeline(action="set_track_enable", params={"track_type": kind, "index": int(idx), "enabled": False}),
                           f"disabling {kind} track {idx} ({meta['name']})")

        subtitles = place_subtitles(server, plan, pool) if plan["subtitles"] else "none"

    except BaseException as e:
        cleanup_incomplete(type(e).__name__)
        raise

    # ---- read back and compare
    problems = []
    tl_start = int(server.timeline(action="get_start_frame").get("frame", 0))
    items = sorted(server.timeline(action="get_items", params={"track_type": "video", "track_index": 1}).get("items", []), key=lambda i: i["start"])
    if len(items) != len(plan["video"]):
        problems.append(f"video items: Resolve has {len(items)}, plan has {len(plan['video'])}")
    else:
        for p, it in zip(plan["video"], items):
            if int(it["start"]) != tl_start + p["record_frame"]:
                problems.append(f"{p['id']}: starts {it['start']}, expected {tl_start + p['record_frame']}")
            if int(it["duration"]) != p["frames"]:
                problems.append(f"{p['id']}: {it['duration']} frames, expected {p['frames']}")
            if it.get("name") not in p.get("names", [Path(p["file"]).name]):
                problems.append(f"{p['id']}: camera {it.get('name')!r}, expected one of {p.get('names')}")
            if len(problems) >= 10:
                break
    layer_report = None
    if layered:
        # per track: count, every start/length (+ camera for video), total frames, gaps/overlaps. V1 and the dialogue clips must tile
        # (0 gaps, 0 overlaps); an alternate track may only have the gaps its plan has (a camera that was not recording).
        layer_report, gaps = {}, {"gap_count": None, "overlap_count": None}
        for (kind, idx) in sorted(tplans, key=lambda ki: (ki[0] != "video", ki[1])):
            want = tplans[(kind, idx)]
            got = read_track(kind, idx)
            g, o = gaps_and_overlaps(got)
            want_g, want_o = gaps_and_overlaps([{"start": w["record_frame"], "duration": w["frames"]} for w in want])
            frames = sum(int(i["duration"]) for i in got)
            meta = plan["layers"]["tracks"][kind].get(str(idx), {})
            enabled = server.timeline(action="get_track_enabled", params={"track_type": kind, "index": idx}).get("enabled")
            tname = server.timeline(action="get_track_name", params={"track_type": kind, "index": idx}).get("name")
            tag_ = f"{kind[0].upper()}{idx}"
            first = (kind, idx) == ("video", 1)      # V1's count/start/length/camera were already compared above
            if first:
                pass
            elif len(got) != len(want):
                problems.append(f"{tag_}: Resolve has {len(got)} items, plan has {len(want)}")
            else:
                bad = [w["id"] for w, i in zip(want, got) if not matches(w, i, tl_start, kind)]
                if bad:
                    problems.append(f"{tag_}: {len(bad)} items differ from the plan in start/length/camera (first: {bad[:3]})")
            if not first and frames != sum(w["frames"] for w in want):
                problems.append(f"{tag_}: {frames} frames in total, plan has {sum(w['frames'] for w in want)}")
            if (g, o) != (want_g, want_o):
                problems.append(f"{tag_}: {g} gaps / {o} overlaps, plan has {want_g} / {want_o}")
            if meta and bool(enabled) == bool(meta["disabled"]):
                problems.append(f"{tag_}: track is {'enabled' if enabled else 'disabled'}, plan wants it {'disabled' if meta['disabled'] else 'enabled'}")
            layer_report[tag_] = {"name": tname, "planned_name": meta.get("name"), "items": len(got), "planned_items": len(want),
                                  "frames": frames, "gaps": g, "overlaps": o, "enabled": enabled,
                                  "state": "disabled/muted" if meta.get("disabled") else "enabled"}
            gaps = {"gap_count": (gaps["gap_count"] or 0) + g, "overlap_count": (gaps["overlap_count"] or 0) + o}
        a_items = read_track("audio", 1)
    else:
        a_items = server.timeline(action="get_items", params={"track_type": "audio", "track_index": 1}).get("items", [])
        if len(a_items) != len(plan["audio"]):
            problems.append(f"audio items: Resolve has {len(a_items)}, plan has {len(plan['audio'])}")
        gaps = server.timeline(action="detect_gaps_overlaps")
        if gaps.get("gap_count") or gaps.get("overlap_count"):
            problems.append(f"detect_gaps_overlaps: {gaps.get('gap_count')} gaps, {gaps.get('overlap_count')} overlaps")
    report = {"ok": not problems, "timeline": name, "video_items": len(items), "audio_items": len(a_items),
              "music": len(plan["music"]), "overlays": len(plan["overlays"]), "subtitles": subtitles,
              "gaps": gaps.get("gap_count"), "overlaps": gaps.get("overlap_count"), "problems": problems,
              "warnings": plan["warnings"], "timeline_start_frame": tl_start}
    if proxy_report is not None:
        report["proxies"] = proxy_report
    if plan.get("subtitles") and subtitles != "on the timeline" and not subtitles.startswith("on the timeline"):
        report["subtitle_instructions"] = subtitle_instructions(plan["subtitles"])
    if layer_report is not None:
        report["tracks"] = layer_report
        report["note"] = plan["layers"]["note"]
    print(json.dumps(report, indent=2, default=str))
    sys.exit(0 if report["ok"] else 2)


if __name__ == "__main__":
    main()
