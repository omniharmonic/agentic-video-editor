"""Zoom-state rules shared by every producer of punch-ins (cleanup masks, the pacing pass, shorts) and the validator that audits them.

Why: a punch-in that goes IN, OUT, IN within a few seconds reads as jumpy. A slow in/out is natural; a quick one is not. So:

  * a zoom change (a framing change on the SAME camera of at least `min_zoom_delta`) must come at least `zoom_min_dwell_s` after the
    previous zoom change on that camera (cutaways to another camera in between do not reset the clock);
  * at most `max_ins_per_shot` punch-ins per shot (a run of consecutive segments on one camera);
  * so the only legal rhythm is: one punch-in, held, and a punch back out no sooner than the dwell.

The producers ask `allows()` before they reframe; `validate()` audits a finished video track and reports what still breaks the rules.
A zoom change is a change of the `zoom` value on the same `src` (and `crop`): a change of camera is not a zoom change."""
from __future__ import annotations

EPS = 1e-6


def _chain(video: list[dict], delta: float) -> list[dict]:
    """Zoom events of a video track in time order: {o, dir: in|out, src, z0, z1, run}. `run` numbers the shot (consecutive segments
    on one camera, whatever their zoom). Only a framing change between two ADJACENT segments of the same camera is a zoom change
    (a visible push/pull on the same shot); a cutaway to another camera and back does not reset the dwell clock, because the
    clock is read per camera in `allows`/`validate`."""
    segs = sorted(video, key=lambda s: s["o0"])
    events, run, prev = [], -1, None
    for s in segs:
        if prev is None or s["src"] != prev["src"]:
            run += 1                    # a camera change is not a zoom change, whatever the framings on either side
        elif prev.get("crop") == s.get("crop"):
            z0, z1 = float(prev.get("zoom") or 1.0), float(s.get("zoom") or 1.0)
            if abs(z1 - z0) >= delta - EPS:
                events.append({"o": float(s["o0"]), "dir": "in" if z1 > z0 else "out", "src": s["src"], "z0": z0, "z1": z1, "run": run})
        prev = s
    return events


def events(video: list[dict], delta: float) -> list[dict]:
    return _chain(video, delta)


def allows(video: list[dict], t: float, src: str, direction: str, dwell_s: float, delta: float, max_ins: int = 1) -> tuple[bool, str]:
    """May `src` change zoom (`direction` in|out) at output time `t`, given the track as it stands before `t`?
    Returns (ok, why-not). dwell_s <= 0 disables the rule (legacy behaviour)."""
    if dwell_s <= 0:
        return True, ""
    before = [s for s in video if s["o0"] < t - 1e-6]
    ev = [e for e in _chain(before, delta) if e["src"] == src]
    if ev and t - ev[-1]["o"] < dwell_s - 1e-6:
        e = ev[-1]
        return False, (f"zoom on {src} already changed ({e['dir']}, {e['z0']:.2f}x->{e['z1']:.2f}x) {t - e['o']:.1f}s ago "
                       f"(zoom_min_dwell_s {dwell_s:g})")
    if direction == "in" and max_ins > 0 and before:
        # the shot being extended is the last run before `t`; count the punch-ins already in it
        last = max(before, key=lambda s: s["o0"])
        if last["src"] == src:
            cur = _run_of_last(before)
            n = sum((1 if e["dir"] == "in" else -1) for e in _chain(before, delta) if e["run"] == cur)
            if n >= max_ins:
                return False, f"already punched in on this shot of {src} ({n} in, none back out: max_ins_per_shot {max_ins})"
    return True, ""


def _run_of_last(segs: list[dict]) -> int:
    ordered = sorted(segs, key=lambda s: s["o0"])
    run, prev = -1, None
    for s in ordered:
        if s["src"] != prev:
            run += 1
            prev = s["src"]
    return run


def upcoming_change_ok(video: list[dict], t: float, end: float, src: str, z: float, dwell_s: float, delta: float) -> tuple[bool, str]:
    """A reframe over [t, end] must not be undone by a zoom change right after it (`end` within the dwell of `t`), nor land on a
    framing indistinguishable from the next shot (the two would merge into one long hold)."""
    if dwell_s <= 0:
        return True, ""
    nxt = next((s for s in sorted(video, key=lambda s: s["o0"]) if s["o0"] >= end - 1e-6), None)
    if nxt is None or nxt["src"] != src:
        return True, ""
    dz = abs(float(nxt.get("zoom") or 1.0) - z)
    if dz < delta - EPS:
        return False, f"the next shot on {src} is already at {float(nxt.get('zoom') or 1.0):.2f}x, too close to {z:.2f}x"
    if end - t < dwell_s - 1e-6:
        return False, f"the next shot on {src} changes zoom {end - t:.1f}s later (zoom_min_dwell_s {dwell_s:g})"
    return True, ""


def validate(video: list[dict], dwell_s: float, delta: float, max_ins: int = 1) -> dict:
    """Audit a finished video track. Counts and lists every place that breaks the zoom rules.

    kinds: `short_dwell` (two zoom changes on one camera closer than the dwell), `in_out_in` (in, out, in within the dwell
    window: the jumpy pattern; also counted as short_dwell), `multiple_ins` (a shot punched in deeper than max_ins without coming back out)."""
    ev = _chain(video, delta)
    viol: list[dict] = []
    by_src: dict[str, list[dict]] = {}
    for e in ev:
        by_src.setdefault(e["src"], []).append(e)
    gaps: list[float] = []
    n_short = n_ioi = 0
    for src, es in by_src.items():
        for a, b in zip(es, es[1:]):
            gaps.append(b["o"] - a["o"])
            if dwell_s > 0 and b["o"] - a["o"] < dwell_s - 1e-6:
                n_short += 1
                viol.append({"kind": "short_dwell", "o": round(b["o"], 3), "src": src, "interval_s": round(b["o"] - a["o"], 3),
                             "pattern": f"{a['dir']}->{b['dir']}", "detail": f"{a['dir']} at {a['o']:.1f}s then {b['dir']} at {b['o']:.1f}s on {src}"})
        for a, b, c in zip(es, es[1:], es[2:]):
            if (a["dir"], b["dir"], c["dir"]) == ("in", "out", "in") and dwell_s > 0 and c["o"] - a["o"] < dwell_s - 1e-6:
                n_ioi += 1
                viol.append({"kind": "in_out_in", "o": round(a["o"], 3), "src": src, "interval_s": round(c["o"] - a["o"], 3),
                             "detail": f"in {a['o']:.1f}s, out {b['o']:.1f}s, in {c['o']:.1f}s on {src}"})
    n_multi = 0
    if max_ins > 0:
        net: dict[int, int] = {}          # punch-ins still "in" (not yet back out) per shot
        for e in ev:
            net[e["run"]] = net.get(e["run"], 0) + (1 if e["dir"] == "in" else -1)
            if net[e["run"]] > max_ins:
                n_multi += 1
                viol.append({"kind": "multiple_ins", "o": round(e["o"], 3), "src": e["src"], "count": net[e["run"]],
                             "detail": f"{net[e['run']]} punch-ins deep in one shot of {e['src']} without coming back out (max {max_ins})"})
    viol.sort(key=lambda v: (v["o"], v["kind"]))
    return {"dwell_s": dwell_s, "max_ins_per_shot": max_ins, "zoom_changes": len(ev),
            "ins": sum(1 for e in ev if e["dir"] == "in"), "outs": sum(1 for e in ev if e["dir"] == "out"),
            "min_interval_s": round(min(gaps), 3) if gaps else None,
            "median_interval_s": round(sorted(gaps)[len(gaps) // 2], 3) if gaps else None,
            "short_dwell": n_short, "in_out_in": n_ioi, "multiple_ins": n_multi, "violations": viol, "ok": not viol}


def policy_limits(pol: dict) -> tuple[float, float, int]:
    """(dwell_s, delta, max_ins) the validator should use for a cleanup policy: the stricter of the masks and pacing dwell."""
    m, p = pol["masks"], pol.get("pacing") or {}
    d = [x for x in (float(m.get("zoom_min_dwell_s") or 0.0), float(p.get("zoom_min_dwell_s") or 0.0)) if x > 0]
    return (max(d) if d else 0.0), float(m["min_zoom_delta"]), int(m.get("max_ins_per_shot", 1))


def format_violations(rep: dict, limit: int = 12) -> list[str]:
    out = [f"zoom rule broken ({v['kind']}) at {v['o']:.1f}s: {v['detail']}" for v in rep["violations"][:limit]]
    if len(rep["violations"]) > limit:
        out.append(f"... and {len(rep['violations']) - limit} more zoom-rule violations")
    return out
