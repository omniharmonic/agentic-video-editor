"""Task 24 (ruling 3), review I5 and re-review: no HTTP route can approve anything without the app's native per-act grant.

The routes are not a hand-written list. Every route string and every path shape is read from the server's own dispatchers
(StudioApp.post and _post_action, the server's _workspace, _episodes and _room_post): each literal, each literal under its
parent ids, and the nested shapes the handlers split on (`<lit>/<id>`, `<lit>/<project>/<id>`, `<lit>/<project>/<id>/<action>`,
`<lit>/<id>/<action>`). Each is called once per id kind (a studio post, an episode publish-queue item, a content item, a clip,
a trim key, a relayed sign-off), with the room token and a body whose every id field names that kind, plus everything an
approval could want (the PIN, the sha, a name, "approve", a 5-second view, `via: native-sheet`, trim ends, send again). It runs
in a browser (no native authority) and in the app (an authority with no grant spent). Afterwards no protected state may move:
studio post approval, a content item's approval, the episode's trim, the episode publish queue, the opening and closing
(sequence) approval, studio resolve, and a confirmed relayed sign-off.

Final review I1: attribution follows the channel, so the same walk also asserts that no room-token request writes an
`actor: "human"` event to any project log (only the app's native bridge writes as the person). `presence.set` is the one
exception: it is the Room's heartbeat ("someone is working here"), not an act or an answer. A third canary, a route that writes
as the person with only the room token, proves the harness catches that too.

Two canaries prove the harness can catch an approving route: a test-only route that approves a content item and one that
approves an episode publish item, each with only the room token. With either one registered, the harness must report it.
A reach check proves the scan really got through: the known legitimate routes for each state answer for real (not 404, not 400).
"""

from __future__ import annotations

import http.client
import inspect
import json
import re
import shutil
import threading

import pytest

from ave import cli, sequence
from ave.content import manifest as M
from ave.desktop_auth import NativeAuthority
from ave.episode import Episode
from ave.project import Project
from ave.publish import queue as pq
from ave.studio.ui import api as studio_api, server as studio_server

from content_helpers import apply_env, make_episode, pick_quote
from publish_helpers import isolated_secrets, queue_item, video_pool  # noqa: F401  (fixtures)
from sched_helpers import make_world, no_agent_markers  # noqa: F401

SEG = re.compile(r"""["']([a-z][a-z0-9_-]*(?:/[a-z][a-z0-9_-]*)*)["']""")
ACTION = re.compile(r"""(?:bits|parts)\[(?:[2-9]|-1)\]\s*==\s*["']([a-z][a-z0-9_-]*)["']|action\s*==\s*["']([a-z][a-z0-9_-]*)["']""")
HANDLERS = ("_room_post", "_workspace", "_episodes")


def _src(fn) -> str:
    fns = [fn]
    while getattr(fns[-1], "__wrapped__", None) is not None:          # a wrapped dispatcher: read the original's routes too
        fns.append(fns[-1].__wrapped__)
    return "\n".join(inspect.getsource(f.__code__) for f in fns)       # the code object: getsource would follow __wrapped__


def _literals(src: str) -> set[str]:
    return {m.group(1) for m in SEG.finditer(src)}


def _actions(src: str) -> set[str]:
    return {a or b for a, b in ACTION.findall(src)}


def route_table(ids: dict) -> list[tuple[str, str, str]]:
    """(verb, path, id kind) for every mutating route shape the dispatchers accept. `ids` maps a kind to its id; a path with an id
    in it is called with that kind's body, a path without one with every kind's body."""
    H = studio_server.StudioHandler
    studio_src = _src(studio_api.StudioApp.post) + _src(studio_api.StudioApp._post_action)
    studio_lits, studio_acts = _literals(studio_src), _actions(studio_src) | _literals(_src(studio_api.StudioApp._post_action))
    out = set()
    kinds = list(ids)
    for lit in studio_lits | studio_acts:
        for k in kinds:
            out.add(("POST", f"/api/studio/{lit}", k))
        out.add(("POST", f"/api/studio/posts/{ids['post']}/{lit}", "post"))
        for parent in ("campaigns/c_none", "alerts/al_none", "schedule/proposals/sp_none", "collaboration", "episodes", "workbench"):
            for k in kinds:
                out.add(("POST", f"/api/studio/{parent}/{lit}", k))
    for name in HANDLERS:
        src = _src(getattr(H, name))
        lits, acts = _literals(src), _actions(src)
        base = "/api/room/" if name == "_room_post" else "/api/workspace/"
        for lit in lits:
            for k in kinds:
                out.add(("POST", f"{base}{lit}", k))
                out.add(("POST", f"{base}{lit}/{ids[k]}", k))
                out.add(("POST", f"{base}{lit}/{ids['project']}/{ids[k]}", k))
                for a in acts:
                    out.add(("POST", f"{base}{lit}/{ids[k]}/{a}", k))
                    out.add(("POST", f"{base}{lit}/{ids['project']}/{ids[k]}/{a}", k))
    for k in kinds:
        out |= {("PATCH", f"/api/workspace/artifact/{ids['signoff']}", k), ("PUT", "/api/studio/workbench/upload?name=a.png", k),
                ("PUT", "/api/studio/library/upload?name=a.png", k), ("POST", "/api/studio/approve", k)}
    return sorted(out)


@pytest.fixture
def world(tmp_path, monkeypatch, capsys, video_pool):
    apply_env(monkeypatch, tmp_path)
    w = make_world(tmp_path)
    pid, vid = w.add_post()
    root = w.sp.root / "ep12"
    ep = make_episode(root)
    proj = Project.create(root, "Episode 12", person="Maren")
    proj.log.append("artifact.created", artifact="art_ep", data={"kind": "episode", "title": "Episode 12", "stage": "Plan"}, actor="human")
    signoff = proj.log.append("stage.signed_off", artifact="art_ep", actor="claude",
                              data={"from": "Plan", "to": "Draft", "quote": "lock the brief", "relayed_by": "claude"})
    proj.log.append("decision.recorded", artifact="art_ep", actor="claude",
                    data={"decision": "dec_w", "summary": "Opened wide", "options": ["Wide", "Close"], "chosen": "Wide"})
    picks = tmp_path / "picks.json"
    picks.write_text(json.dumps([pick_quote()]))
    assert cli.main(["content", "propose", str(root), str(picks)]) == 0
    assert cli.main(["content", "render", str(root)]) == 0
    capsys.readouterr()
    video = ep.exports / "shorts" / "clip1.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(video_pool / "vertical.mp4", video)
    item = queue_item(ep, video, title="Soil is a habit")
    meta = json.loads(ep.meta_path.read_text())
    meta["trim"] = {"main": {"in": 3.2, "out": 60.0, "by": "claude", "draft_only": True, "at": "2026-11-02T10:00:00+0000"}}
    ep.meta_path.write_text(json.dumps(meta))
    eid = signoff["id"] if isinstance(signoff, dict) else signoff
    return dict(w=w, pid=pid, vid=vid, ep=Episode(root), proj=proj, item=item, signoff=eid)


def _state(W):
    w, ep, proj = W["w"], W["ep"], W["proj"]
    return {
        "studio": [(q.get("status"), len(q.get("approvals") or []), json.dumps(q.get("results") or {}, sort_keys=True))
                   for q in (pq.load(w.sp, f.stem) for f in sorted((w.sp.root / "publish" / "queue").glob("*.json")))],
        "studio_events": sorted({json.loads(line).get("op") for line in (w.sp.ledger_dir / "events.jsonl").read_text().splitlines()}
                                & {"approved", "viewed", "resolved"}),
        # from the durable records, not only the end state: a later reject must not hide an approval made earlier in the scan
        "content": ["approved" if it["review"]["status"] == "approved" else "not approved" for it in M.load_latest(ep)["items"]]
                   + ["approved" for line in _lines(M.content_dir(ep) / "decisions.jsonl") if line.get("decision") == "approve"],
        "trim": json.loads(ep.meta_path.read_text()).get("trim"),
        "episode_queue": [("approved" if i["status"] == "approved" or i.get("approvals") or any(e.get("event") == "approved" for e in i.get("events") or [])
                           else "not approved") for i in pq.list_items(ep)],
        "sequence": sequence.approval_path(ep).exists(),
        "signoff_confirmed": any(e.get("op") == "note.added" and (e.get("data") or {}).get("confirms") == W["signoff"] for e in proj.read_events()),
    }


HEARTBEAT = ("presence.set",)          # the Room's "someone is here", not an act: see the module docstring


def _human_events(W) -> set[str]:
    """Every actor "human" event in every project log under the world (episode and studio), but the heartbeat."""
    out = set()
    for pj in W["w"].sp.root.rglob(".ave/project.json"):
        for e in Project.open(pj.parent.parent).read_events():
            if e.get("actor") == "human" and e.get("op") not in HEARTBEAT:
                out.add(e["id"])
    return out


def _lines(p):
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.is_file() else []


GREEDY = {"kinds": ["note"], "pin": "424242", "by": "Maren", "confirm": True, "displayed_ms": 5000, "via": "native-sheet", "decision": "approve",
          "verdict": "approve", "approve": True, "override": True, "human": True, "authorized": True, "in": 1.0, "out": 50.0,
          "doc": "main", "send_again": True, "outcome": "published", "stage": "Out", "artifact": "ep12", "note": "fine"}


def _bodies(W, ids: dict) -> dict:
    sha = W["w"].item(W["pid"], W["vid"])["sha256"]
    common = {**GREEDY, "project": ids["project"], "sha": sha, "variant": W["vid"], "confirm_sha": W["item"]["sha256"],
              "items": [{"post": W["pid"], "variant": W["vid"], "sha": sha}]}
    one = lambda v: {**common, "id": v, "item": v, "post": v, "clip": v, "session": v, "key": v, "event": v, "queue": v, "token": v}
    out = {k: one(v) for k, v in ids.items() if k != "project"}
    out["post"] = {**out["post"], "post": W["pid"]}
    out["clip"] = {**out["clip"], "verdict": "keep"}                         # a real call on a select, so the route is reached for real
    out["signoff"] = {**out["signoff"], "event": W["signoff"], "confirm": W["signoff"], "artifact": ids["signoff"]}
    return out


def _acts(W, ids: dict) -> list[dict]:
    """One real board act each (as the page sends them), so the board's own routes really write: each must be Claude's."""
    return [{"stage": "Refine"}, {"stage": "Draft"}, {"fields": {"title": "Renamed"}}, {"note": "An update"},
            {"resolve": "dec_w", "choice": "Close"}, {"reopen_decision": "dec_w"}, {"undo": W["signoff"]},
            {"project": ids["project"], "title": "A new artifact"}, {"artifact": "ep12", "clip": ids["clip"], "verdict": "keep"},
            {"artifact": "ep12", "clip": ids["clip"], "nudge": {"end": 1}}]


def scan(W, native: bool, extra=()) -> tuple[list[str], dict]:
    """Call every route shape once per id kind; return (what moved, status per route)."""
    w = W["w"]
    srv = studio_server.StudioServer(w.studio, pin="424242", by="Maren", native_authority=NativeAuthority() if native else None)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    ids = {"post": W["pid"], "queue": W["item"]["id"], "content": "quote-c01-4x5", "clip": "c02", "trim": "main",
           "signoff": f"{W['proj'].project_id}:art_ep", "project": W["proj"].project_id}
    bodies = _bodies(W, ids)
    kinds = [k for k in ids if k != "project"]
    table = sorted({(v, r, k2) for v, r, k in route_table(ids) for k2 in (kinds if k == "project" else [k])}) + list(extra)   # a <project>-only path: every body
    before = _state(W)
    humans = _human_events(W)
    answered, errors = {}, {}
    try:
        conn = [None]

        def send(verb, route, b):                                          # one kept-alive connection (thousands of requests)
            for attempt in (0, 1):
                if conn[0] is None:
                    conn[0] = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=20)
                try:
                    conn[0].request(verb, route, body=json.dumps(b).encode(), headers={"X-AVE-Token": srv.token, "Host": f"127.0.0.1:{srv.port}",
                                                                                     "Content-Type": "application/json"})
                    r = conn[0].getresponse()
                    data = r.read()
                    if r.status < 300 or (verb, route) not in errors:
                        errors[(verb, route)] = "" if r.status < 300 else data.decode("utf-8", "replace")[:300]
                    if r.getheader("Connection", "").lower() == "close" or r.will_close:
                        conn[0].close(); conn[0] = None
                    return r.status
                except (http.client.HTTPException, OSError):
                    conn[0].close(); conn[0] = None
                    if attempt:
                        raise

        for verb, route, kind in table:
            for b in (bodies[kind], {}):
                status = send(verb, route, b)
                answered[(verb, route)] = min(status, answered.get((verb, route), 999))
        acts = _acts(W, ids)
        for verb, route in sorted({(v, r) for v, r, _ in table if r.startswith(("/api/workspace/", "/api/room/"))
                                   and (r.startswith("/api/workspace/artifact/") or not any(i in r for i in ids.values()))}):
            for b in acts:                                                 # the board's real acts, on every id-free route
                status = send(verb, route, b)
                answered[(verb, route)] = min(status, answered.get((verb, route), 999))
        for verb, route in {(v, r) for v, r, _ in table if r.endswith("/approve")}:      # and the old approve shape
            send(verb, route, {"pin": "424242", "items": bodies["post"]["items"]})
    finally:
        srv.shutdown()
        t.join(5)
        srv.server_close()
    after = _state(W)
    moved = []
    if _human_events(W) - humans:
        moved.append("written as the person")
    if any(s == "approved" or n for s, n, _ in after["studio"]) or [r for *_, r in after["studio"]] != [r for *_, r in before["studio"]]:
        moved.append("studio post approval or resolve")
    if after["studio_events"] != before["studio_events"]:
        moved.append("studio approval events")
    if "approved" in after["content"]:
        moved.append("content approval")
    if after["trim"] != before["trim"]:
        moved.append("trim")
    if "approved" in after["episode_queue"]:
        moved.append("episode publish queue")
    if after["sequence"]:
        moved.append("sequence approval")
    if after["signoff_confirmed"]:
        moved.append("relayed sign-off confirmed")
    return moved, {k: (v, errors.get(k, "")) for k, v in answered.items()}


# the known legitimate routes for each protected state, and that they really answered (not 404, not a body-shape 400)
REACHED = {
    "studio post (send back for changes)": ("POST", "/api/studio/posts/{post}/feedback"),
    "content item (reject)": ("POST", "/api/workspace/content/reject"),
    "episode publish queue (reject)": ("POST", "/api/workspace/publish/reject"),
    "clip (keep, maybe, skip)": ("POST", "/api/room/selects/decide"),
    "relayed sign-off (undo)": ("PATCH", "/api/workspace/artifact/{signoff}"),
}
SHAPE = re.compile(r"must be|is required|required|Choose|unknown|not found|Open the", re.I)
NO_HTTP_ROUTE = ("trim", "sequence", "resolve", "approve")      # these protected acts have no web route at all


@pytest.mark.parametrize("native", [False, True], ids=["browser", "app-without-a-grant"])
def test_no_http_route_approves_anything_without_the_native_grant(world, native):
    moved, answered = scan(world, native)
    assert moved == [], moved
    assert len(answered) > 2000
    fill = {"post": world["pid"], "signoff": f"{world['proj'].project_id}:art_ep"}
    for what, (verb, route) in REACHED.items():
        status, why = answered[(verb, route.format(**fill))]
        # reached for real: it answered, or it got past the body's shape to the work itself (the fixture has no clip candidates)
        assert status < 300 or (status == 400 and not SHAPE.search(why)), (what, route, status, why)
    for (verb, route), (status, _) in answered.items():
        tail = route.split("?")[0].rsplit("/", 1)[-1]
        if tail in NO_HTTP_ROUTE and route.startswith(("/api/workspace/", "/api/room/")):
            assert status in (400, 404, 405), (route, status)


def _approve_content_canary(self, verb, route, body):
    if verb == "POST" and route == "content/mark":                       # a test-only route that approves with the room token
        eps = self.server.episodes
        return self._json(200, eps.content_decide(str(body.get("project")), "main", str(body.get("id")), "approve", by="claude"))
    return _approve_content_canary.__wrapped__(self, verb, route, body)


def _approve_queue_canary(self, verb, route, body):
    bits = route.split("/")
    if verb == "POST" and len(bits) == 4 and bits[0] == "publish" and bits[3] == "ok":   # publish/<project>/<id>/ok
        _, ep = self.server.episodes._one(bits[1])
        it = pq.load(ep, bits[2])
        return self._json(200, pq.approve(ep, bits[2], "claude", it["sha256"][:12]))
    return _approve_queue_canary.__wrapped__(self, verb, route, body)


def _nested_content_canary(self, verb, route, body):
    bits = route.split("/")
    if verb == "POST" and len(bits) == 3 and bits[0] == "content" and bits[2] == "ok":   # content/<project>/ok, the id in the body
        return self._json(200, self.server.episodes.content_decide(bits[1], "main", str(body.get("id")), "approve", by="claude"))
    return _nested_content_canary.__wrapped__(self, verb, route, body)


def _as_person_canary(self, verb, route, body):
    if verb == "POST" and route == "person/note":                        # a test-only route that writes as the person on the token
        p = self.server.workspace._project(str(body.get("project")))
        if p is not None:
            p.log.append("note.added", artifact="art_ep", data={"text": "the director said so"}, actor="human")
            return self._json(200, {})
    return _as_person_canary.__wrapped__(self, verb, route, body)


@pytest.mark.parametrize("canary,expect,handler", [(_approve_content_canary, "content approval", "_episodes"),
                                                   (_approve_queue_canary, "episode publish queue", "_episodes"),
                                                   (_nested_content_canary, "content approval", "_episodes"),
                                                   (_as_person_canary, "written as the person", "_workspace")],
                         ids=["content", "episode-queue", "nested-content", "as-the-person"])
def test_the_harness_catches_an_approving_route(world, monkeypatch, canary, expect, handler):
    """A route that approves (or writes as the person) with only the room token must make the harness fail: it finds the route
    by reading the dispatcher, calls it with the matching id kind, and reports the state that moved."""
    canary.__wrapped__ = getattr(studio_server.StudioHandler, handler)
    monkeypatch.setattr(studio_server.StudioHandler, handler, canary)
    moved, answered = scan(world, native=False)
    assert expect in moved, moved
