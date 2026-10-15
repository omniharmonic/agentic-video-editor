#!/usr/bin/env python3
"""OPT-IN live smoke test of the layered handoff. NOT part of pytest: it drives the real Resolve bridge.

Builds the synthetic two-camera episode (tests/fixtures.py) in a temp dir, plans a layered handoff and runs scripts/resolve_handoff.py
against the OPEN Resolve into a SCRATCH project (default "AVE layers smoke"; created if missing, never an episode's project).
Needs Resolve open with `Workspace > Scripts > resolve_bridge` running. Run it twice to exercise the idempotent/resume refusal path.

    .venv/bin/python scripts/resolve_layers_smoke.py --yes-touch-resolve [--project NAME]

Success = the script prints "ok": true with tracks V1 4, V2 2, V3 1, A1 3, A2 1 (V2/V3/A2 disabled). Exit code is the script's.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(REPO / "src"), str(REPO / "tests")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes-touch-resolve", action="store_true", help="required: this creates a project and a timeline in the open Resolve")
    ap.add_argument("--project", default="AVE layers smoke")
    a = ap.parse_args()
    if not a.yes_touch_resolve:
        raise SystemExit("Refusing to touch Resolve without --yes-touch-resolve (this is the opt-in live smoke test).")
    import fixtures
    from ave import compile as comp, config, editdoc, handoff
    work = Path(tempfile.mkdtemp(prefix="ave-layers-smoke-"))
    ep = fixtures.build_episode(work / "episode", upto="assembly")
    doc = editdoc.clone(editdoc.load_latest(ep, "assembly"))
    V = lambda i, s, o0, o1, e0: {"id": i, "src": s, "o0": o0, "o1": o1, "e0": e0, "crop": None, "zoom": 1.0}  # noqa: E731
    D = lambda i, o0, o1, e0: {"id": i, "src": "A", "o0": o0, "o1": o1, "e0": e0, "xfade_ms": 0, "gain_db": 0.0}  # noqa: E731
    doc["tracks"]["video"] = [V("v1", "A", 0.0, 0.9, 0.0), V("v2", "B", 0.9, 3.9, 10.0), V("v3", "A", 3.9, 9.9, 13.0), V("v4", "B", 9.9, 12.0, 19.0)]
    doc["tracks"]["dialogue"] = [D("d1", 0.0, 0.9, 0.0), D("d2", 0.9, 5.0, 10.0), D("d3", 5.0, 12.0, 14.1)]
    doc["duration"] = 12.0
    master = work / "master.wav"
    comp.compile_audio(ep, doc, master)
    wav = handoff.sum_wav_path(ep, doc, "A")
    comp.compile_source_sum(ep, doc, "A", wav)
    plan = handoff.plan(ep, doc, name="AVE layers smoke", audio_master=master, project=a.project, layers=True)
    plan_path = work / "plan.json"
    plan_path.write_text(json.dumps(plan))
    py, mcp = config.resolve_mcp_python(), config.resolve_mcp_dir()
    if not (py and mcp):
        raise SystemExit("Resolve MCP server not found. Run scripts/setup.sh")
    return subprocess.run([str(py), str(REPO / "scripts/resolve_handoff.py"), str(plan_path), "--mcp-src", str(mcp / "src")]).returncode


if __name__ == "__main__":
    sys.exit(main())
