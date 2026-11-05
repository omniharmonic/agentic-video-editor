"""Final review I1, M7 and M3: attribution follows the channel.

Claude's MCP holds the room token, so a request that holds only the token cannot show it is the person. Every board write over
HTTP (a field, a stage, an update, a new artifact, an answer, a reopen) is Claude's: `actor: "claude"`, and a sign-off or an
answer carries `relayed_by: "claude"`, so the director confirms or undoes it on the board like one Claude relayed through the MCP
tools. The person's own writes come through the app's native bridge (desktop_approvals `boardWrite`, `via="app"`); an answer that
changes a shared settings file also takes the device-owner check, as do handing over publishing and removing a member.
"""

from __future__ import annotations

import http.client
import json
import threading
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from ave import humanpresence
from ave.desktop_approvals import Approvals, approval_args
from ave.desktop_auth import NativeAuthority
from ave.episode import Episode
from ave.project import Project
from ave.studio.project import StudioProject
from ave.studio.service import open_studio
from ave.studio.ui.api import StudioError
from ave.studio.ui.server import StudioServer


@pytest.fixture(autouse=True)
def no_markers(monkeypatch):
    for k in humanpresence.AGENT_MARKERS:                  # the suite may run inside an agent harness
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    sp = StudioProject(tmp_path / "studio")
    sp.create(title="Grounded conversations", tz="UTC")
    Project.create(sp.root, "Grounded conversations", person="Maren")
    ep = Episode(sp.root / "ep12")
    ep.create("Episode 12")
    p12 = Project.create(ep.root, "Episode 12", person="Maren")
    p12.log.append("artifact.created", artifact="art_ep", data={"kind": "episode", "title": "Episode 12", "stage": "Refine"}, actor="human")
    p12.log.append("decision.recorded", artifact="art_ep", actor="claude",
                   data={"decision": "dec_wide", "summary": "Opened on the wide shot", "options": ["Wide shot", "Close-up"],
                         "chosen": "Wide shot"})
    p12.log.append("decision.recorded", artifact="art_ep", actor="scheduler",
                   data={"decision": "dec_set", "kind": "setting", "summary": "Maren and Imani both changed episode.json",
                         "options": ["Use all of Maren's", "Use all of Imani's"], "chosen": None, "keys": [], "whole": False})
    p13 = Project.create(sp.root / "ep13", "Episode 13", person="Maren")
    p13.log.append("artifact.created", artifact="art_ep", data={"kind": "episode", "title": "Episode 13", "stage": "Plan"}, actor="human")
    srv = StudioServer(open_studio(sp.root), pin="424242")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield SimpleNamespace(srv=srv, sp=sp, ep=ep, p12=p12, p13=p13, ep12=f"{p12.project_id}:art_ep", ep13=f"{p13.project_id}:art_ep")
    srv.shutdown()
    t.join(5)
    srv.server_close()


def _http(srv, verb, path, body):
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=20)
    c.request(verb, path, body=json.dumps(body).encode(), headers={"X-AVE-Token": srv.token, "Host": f"127.0.0.1:{srv.port}",
                                                                 "Content-Type": "application/json"})
    r = c.getresponse()
    data = json.loads(r.read() or b"{}")
    c.close()
    return r.status, data


def _patch(ws, key, body):
    return _http(ws.srv, "PATCH", "/api/workspace/artifact/" + quote(key, safe=""), body)


def _last(p: Project) -> dict:
    return sorted(p.read_events(), key=lambda e: (e["hlc"], e["id"]))[-1]


def _bridge(ws):
    return Approvals(ws.srv.app, ws.srv.workspace, ws.srv.episodes, server=ws.srv)


def _app_write(ws, method, path, body):
    """What the app's web view does for the person: aveNative.boardWrite(method, path, body)."""
    sheets = _bridge(ws)
    req = sheets.board_request(approval_args("boardWrite", {"method": method, "path": path, "body": body}))
    return sheets.board_write(req)


# ---------------------------------------------------------------------------------------------------- over the room token
def test_a_room_token_stage_move_is_claudes_relayed_sign_off(ws):
    status, got = _patch(ws, ws.ep13, {"stage": "Draft", "from": "Plan"})
    assert status == 200
    e = _last(ws.p13)
    assert e["op"] == "stage.signed_off" and e["actor"] == "claude" and e["data"]["relayed_by"] == "claude"
    relayed = got["row"]["relayed"]
    assert [r["type"] for r in relayed] == ["sign_off"] and relayed[0]["event"] == e["id"]
    assert "without your words" in relayed[0]["text"] and got["row"]["waiting"] == "you"   # the director confirms or undoes it
    status, _ = _patch(ws, ws.ep13, {"stage": "Refine", "from": "Draft", "quote": "lock the draft"})
    assert status == 200 and _last(ws.p13)["data"]["quote"] == "lock the draft"
    status, _ = _patch(ws, ws.ep13, {"stage": "Plan", "from": "Refine"})                    # going back: Claude's, not a sign-off
    assert status == 200 and _last(ws.p13)["actor"] == "claude" and _last(ws.p13)["op"] == "stage.reopened"


def test_room_token_fields_updates_new_artifacts_answers_and_reopens_are_claudes(ws):
    assert _patch(ws, ws.ep13, {"fields": {"title": "Episode 13: the river"}})[0] == 200
    assert _last(ws.p13)["actor"] == "claude"
    assert _patch(ws, ws.ep13, {"note": "The opening drags."})[0] == 200
    assert _last(ws.p13)["actor"] == "claude"
    status, got = _http(ws.srv, "POST", "/api/workspace/artifact", {"project": ws.p13.project_id, "title": "Thumbnail"})
    assert status == 201 and _last(ws.p13)["op"] == "artifact.created" and _last(ws.p13)["actor"] == "claude"
    status, got = _patch(ws, ws.ep12, {"resolve": "dec_wide", "choice": "Close-up"})
    e = _last(ws.p12)
    assert status == 200 and e["actor"] == "claude" and e["data"]["relayed_by"] == "claude"
    assert [r["type"] for r in got["row"]["relayed"]] == ["resolution"]                    # to confirm or undo
    assert _patch(ws, ws.ep12, {"reopen_decision": "dec_wide"})[0] == 200
    assert _last(ws.p12)["actor"] == "claude"
    status, _ = _patch(ws, ws.ep12, {"resolve": "dec_set", "choice": "Use all of Imani's"})  # a settings answer: relayed too
    assert status == 200 and _last(ws.p12)["actor"] == "claude" and _last(ws.p12)["data"]["relayed_by"] == "claude"
    # an undo over the token is Claude's too, in a browser as in the app
    assert _patch(ws, ws.ep12, {"undo": e["id"]})[0] in (200, 409)
    assert not [x for x in ws.p12.read_events() + ws.p13.read_events() if x.get("actor") == "human" and x["op"] != "artifact.created"]


def test_a_room_token_sign_off_cannot_pass_claudes_draft_trim(ws):
    import json as _j
    meta = _j.loads(ws.ep.meta_path.read_text())
    meta["trim"] = {"main": {"in": 3.2, "out": 60.0, "by": "claude", "draft_only": True, "at": "2026-11-02T10:00:00+0000"}}
    ws.ep.meta_path.write_text(_j.dumps(meta))
    status, got = _patch(ws, ws.ep12, {"stage": "Finish", "from": "Refine"})
    assert status == 409 and got["code"] == "gate.trim" and "trim" in got["error"]
    assert _last(ws.p12)["op"] != "stage.signed_off"


# ---------------------------------------------------------------------------------------------------- through the app's bridge
def test_the_apps_bridge_writes_as_the_person(ws):
    key = quote(ws.ep13, safe="")
    _app_write(ws, "PATCH", f"/api/workspace/artifact/{key}", {"stage": "Draft", "from": "Plan"})
    e = _last(ws.p13)
    assert e["actor"] == "human" and e["person"] == "Maren" and "relayed_by" not in e["data"]
    _app_write(ws, "PATCH", f"/api/workspace/artifact/{key}", {"fields": {"due": "2026-12-01"}})
    assert _last(ws.p13)["actor"] == "human"
    _app_write(ws, "PATCH", f"/api/workspace/artifact/{key}", {"note": "Looks good."})
    assert _last(ws.p13)["actor"] == "human"
    got = _app_write(ws, "POST", "/api/workspace/artifact", {"project": ws.p13.project_id, "title": "Thumbnail"})
    assert got["row"]["name"] == "Thumbnail" and _last(ws.p13)["actor"] == "human"
    _app_write(ws, "PATCH", "/api/workspace/artifact/" + quote(ws.ep12, safe=""), {"resolve": "dec_wide", "choice": "Close-up"})
    assert _last(ws.p12)["actor"] == "human" and "relayed_by" not in _last(ws.p12)["data"]


def test_the_bridge_refuses_what_is_not_a_board_write(ws):
    key = quote(ws.ep13, safe="")
    for bad in ({"method": "PATCH", "path": "/api/studio/approve", "body": {}},
                {"method": "POST", "path": f"/api/workspace/artifact/{key}", "body": {}},          # a PATCH path, as POST
                {"method": "PATCH", "path": "/api/workspace/artifact/..%2F..%2Fetc", "body": {}},
                {"method": "PATCH", "path": f"/api/workspace/artifact/{key}", "body": {}, "pin": "424242"},
                {"method": "PATCH", "path": f"/api/workspace/artifact/{key}", "body": "x"}):
        with pytest.raises(ValueError):
            approval_args("boardWrite", bad)
    e1 = ws.p13.log.append("stage.signed_off", artifact="art_ep", actor="claude",
                           data={"from": "Plan", "to": "Draft", "quote": "go", "relayed_by": "claude"})
    with pytest.raises(ValueError, match="Confirm"):                                         # a confirm keeps its own sheet
        _app_write(ws, "PATCH", f"/api/workspace/artifact/{key}", {"confirm": e1["id"]})


def test_a_settings_answer_through_the_app_takes_the_device_owner_check(ws):
    a = NativeAuthority()
    ws.srv.app.native_authority = a
    ws.srv.workspace.authority = a
    sheets = _bridge(ws)
    args = approval_args("boardWrite", {"method": "PATCH", "path": "/api/workspace/artifact/" + quote(ws.ep12, safe=""),
                                        "body": {"resolve": "dec_set", "choice": "Use all of Imani's"}})
    req = sheets.board_request(args)
    assert req["kind"] == "protected" and req["action"] == "answerDecision" and req["keys"] == [f"{ws.ep12}/dec_set"]
    assert "Use all of Imani's" in req["detail"]
    before = len(ws.p12.read_events())
    with pytest.raises(ValueError):
        sheets.board_write(req)                                                               # never without its sheet
    with pytest.raises(StudioError):
        ws.srv.workspace.patch(ws.ep12, {"resolve": "dec_set", "choice": "Use all of Imani's"}, via="app")   # no grant
    other = a._mint("answerDecision", [f"{ws.ep12}/dec_other"])
    with pytest.raises(StudioError):
        sheets.run_protected(req, other)                                                     # a check for something else
    assert len(ws.p12.read_events()) == before
    sheets.run_protected(req, a._mint(req["action"], req["keys"]))
    e = _last(ws.p12)
    assert e["op"] == "decision.resolved" and e["actor"] == "human" and e["data"]["choice"] == "Use all of Imani's"


# ---------------------------------------------------------------------------------------------------- M3
def test_handing_over_publishing_and_removing_a_member_take_the_device_owner_check(ws, monkeypatch):
    a = NativeAuthority()
    ws.srv.app.native_authority = a
    sheets = _bridge(ws)
    pid, done = ws.p12.project_id, []
    for action in ("handOverPublishing", "removeMember"):
        keys = sheets.sharing_keys(action, pid)
        assert keys == [pid]
        with pytest.raises(StudioError):
            sheets.run_sharing(action, pid, None, lambda: done.append(action))                 # no check
        with pytest.raises(StudioError):
            sheets.run_sharing(action, pid, a._mint(action, [ws.p13.project_id]), lambda: done.append(action))   # another project
        other = "removeMember" if action == "handOverPublishing" else "handOverPublishing"
        with pytest.raises(StudioError):
            sheets.run_sharing(action, pid, a._mint(other, [pid]), lambda: done.append(action))  # another act
        grant = a._mint(action, [pid])
        monkeypatch.setenv("AVE_AGENT", "1")
        with pytest.raises(ValueError):
            sheets.run_sharing(action, pid, grant, lambda: done.append(action))               # an agent marker
        monkeypatch.delenv("AVE_AGENT")
        assert done == []
        assert sheets.run_sharing(action, pid, a._mint(action, [pid]), lambda: done.append(action) or "ok") == "ok"
        assert done == [action]
        done.clear()
    from ave import desktop_native
    import inspect
    src = inspect.getsource(desktop_native)
    assert src.count("approvals.run_sharing(action,pid,grant,") == 2                      # both native sheets spend the check
    assert "session.authority.request(f'Hand publishing" in src and "session.authority.request(f'Remove {person}" in src
    assert "boardWrite:(method,path,body)=>ask('boardWrite'" in desktop_native.BRIDGE_SCRIPT
