"""The Room's server side: stage, event stream, flags inbox and the MCP tools that drive it."""
import json
import os
import socket
import stat
import threading
import time
import urllib.error
from types import SimpleNamespace

import pytest

from ave.agent import AgentApp
from ave.room.bus import Bus
from ave.room.client import QUEUE_MAX, queue_file, room_file
from ave.studio.project import StudioProject
from ave.studio.service import open_studio
from ave.studio.ui.server import StudioServer


@pytest.fixture
def tmp_project(tmp_path):
    sp = StudioProject(tmp_path / "studio")
    sp.create(title="Room", tz="America/Denver")
    return sp.root


@pytest.fixture
def mcp(tmp_project):
    app = AgentApp(open_studio(tmp_project), by="codex")
    return SimpleNamespace(call=lambda name, **args: app.call(name, args), app=app)


@pytest.fixture
def room_server(tmp_project):
    srv = StudioServer(open_studio(tmp_project), pin="424242")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def http_get(srv, path, token="default", last_id=None, timeout=1.0, read=True):
    """(status, headers, body) of a raw GET; a stream is read for at most `timeout` s."""
    tok = srv.token if token == "default" else token
    head = f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{srv.port}\r\n" + (f"X-AVE-Token: {tok}\r\n" if tok else "")
    head += (f"Last-Event-ID: {last_id}\r\n" if last_id is not None else "") + "Connection: close\r\n\r\n"
    with socket.create_connection(("127.0.0.1", srv.port), timeout=timeout) as s:
        s.sendall(head.encode())
        buf, end = b"", time.monotonic() + timeout
        try:
            while time.monotonic() < end and (chunk := s.recv(65536)):
                buf += chunk
        except socket.timeout:
            pass
    raw_head, _, body = buf.partition(b"\r\n\r\n")
    lines = raw_head.decode().split("\r\n")
    hdrs = dict(l.split(": ", 1) for l in lines[1:])
    return SimpleNamespace(status=int(lines[0].split()[1]), headers=hdrs, body=body.decode(errors="replace"))


def sse_get(srv, last_id=None):
    r = http_get(srv, "/api/room/stream", last_id=last_id)
    assert r.status == 200 and r.headers["Content-Type"].startswith("text/event-stream")
    return [json.loads(l[len("data: "):]) for l in r.body.splitlines() if l.startswith("data: ")]


def test_stream_resumes_from_last_event_id(room_server):
    room_server.bus.publish("stage", {"view": "cut"})
    room_server.bus.publish("stage", {"view": "script"})
    assert [e["data"]["view"] for e in sse_get(room_server, last_id=1)] == ["script"]
    assert [e["seq"] for e in sse_get(room_server)] == [1, 2]


def test_room_show_updates_the_stage(room_server, mcp):
    out = mcp.call("room_show", artifact="art_ep", view="script", position_s=42.0)
    assert out["room"] == "open"
    assert room_server.stage.view == "script" and room_server.stage.position_s == 42.0
    assert sse_get(room_server)[-1]["data"]["artifact"] == "art_ep"


def test_room_show_rejects_a_bad_view(room_server, mcp):
    with pytest.raises(ValueError):
        mcp.call("room_show", artifact="art_ep", view="timeline")
    assert room_server.stage is None


def test_highlight_and_offer_version(room_server, mcp):
    assert mcp.call("room_highlight", highlights=[{"start": 1.0, "end": 2.0}])["status"] == 409     # nothing on stage yet
    mcp.call("room_show", artifact="art_ep", view="cut", version="v1")
    mcp.call("room_highlight", highlights=[{"start": 1.0, "end": 2.5, "note": "tighten"}])
    assert room_server.stage.highlights == [{"start": 1.0, "end": 2.5, "note": "tighten"}]
    mcp.call("room_offer_version", artifact="art_ep", version="v2", compare_to="v1")
    assert (room_server.stage.version, room_server.stage.compare_to, room_server.stage.view) == ("v2", "v1", "cut")
    types = [e["type"] for e in room_server.bus.since(0) if e["type"] != "preview"]      # the preview follows on its own thread
    assert types[-2:] == ["stage", "offer"]


def test_tools_report_a_closed_room_and_do_not_fail(tmp_project, mcp):
    closed = {"room": "closed", "queued": True}
    assert mcp.call("room_show", artifact="art_ep", view="cut") == closed
    assert mcp.call("room_highlight", highlights=[]) == closed
    assert mcp.call("room_offer_version", artifact="art_ep", version="v2") == closed
    assert mcp.call("room_read") == {"room": "closed"}


def test_a_stale_room_file_reads_as_closed(tmp_project, mcp):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]                                    # nothing listens here any more
    room_file(tmp_project).parent.mkdir(parents=True)
    room_file(tmp_project).write_text(json.dumps({"port": port, "token": "x"}))
    assert mcp.call("room_read")["room"] == "closed"
    assert mcp.call("room_show", artifact="art_ep", view="cut")["queued"] is True


def test_room_read_drains_flags_once(room_server, mcp):
    room_server.flag({"artifact": "art_ep", "start": 10.0, "end": 12.0, "note": "this"})
    got = mcp.call("room_read")
    assert len(got["flags"]) == 1 and got["stage"] is None
    assert mcp.call("room_read")["flags"] == []


def test_flag_endpoint_queues_a_flag_for_room_read(room_server, mcp):
    body = json.dumps({"artifact": "art_ep", "start": 1.0, "end": 2.0, "note": "cut here"}).encode()
    with socket.create_connection(("127.0.0.1", room_server.port), timeout=2) as s:
        s.sendall((f"POST /api/room/flag HTTP/1.1\r\nHost: 127.0.0.1:{room_server.port}\r\nX-AVE-Token: {room_server.token}\r\n"
                   f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body)
        assert b" 200 " in s.recv(4096).split(b"\r\n")[0] + b" "
    assert mcp.call("room_read")["flags"][0]["note"] == "cut here"


def test_stream_requires_the_token(room_server):
    assert http_get(room_server, "/api/room/stream", token=None).status == 401
    assert http_get(room_server, "/api/room/stream", token="wrong").status == 401


def test_room_file_is_private_and_removed_on_stop(tmp_project):
    srv = StudioServer(open_studio(tmp_project))
    f = room_file(tmp_project)
    assert json.loads(f.read_text()) == {"port": srv.port, "token": srv.token}
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    srv.server_close()
    assert not f.exists()


def test_bus_keeps_the_last_500():
    b = Bus()
    for i in range(520):
        b.publish("x", {"i": i})
    held = b.since(0)
    assert len(held) == 500 and held[0]["seq"] == 21 and b.publish("x", {}) == 521


def test_stream_sends_a_heartbeat_when_quiet(room_server, monkeypatch):
    monkeypatch.setattr("ave.studio.ui.server.HEARTBEAT_S", 0.2)
    r = http_get(room_server, "/api/room/stream", timeout=0.8)
    assert r.body.count(": heartbeat") >= 2


def post(srv, route, body, token="default"):
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}/api/room/{route}", data=json.dumps(body).encode(), method="POST",
                                 headers={"X-AVE-Token": srv.token if token == "default" else token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_calls_made_while_closed_are_replayed_when_the_app_starts(tmp_project, mcp):
    mcp.call("room_show", artifact="art_ep", view="script", position_s=3.0)
    mcp.call("room_highlight", highlights=[{"start": 1.0, "end": 2.0}])
    mcp.call("room_offer_version", artifact="art_ep", version="v2", compare_to="v1")
    assert len(queue_file(tmp_project).read_text().splitlines()) == 3
    srv = StudioServer(open_studio(tmp_project))
    try:
        st = srv.stage
        assert (st.view, st.position_s, st.version, st.compare_to, st.highlights) == ("script", 3.0, "v2", "v1", [{"start": 1.0, "end": 2.0}])
        assert [e["type"] for e in srv.bus.since(0) if e["type"] != "preview"][-1] == "offer"
        assert queue_file(tmp_project).read_text() == ""
    finally:
        srv.server_close()


def test_the_queue_keeps_the_last_200(tmp_project, mcp):
    for i in range(QUEUE_MAX + 5):
        mcp.call("room_show", artifact=f"a{i}", view="cut")
    lines = queue_file(tmp_project).read_text().splitlines()
    assert len(lines) == QUEUE_MAX and json.loads(lines[-1])["args"]["artifact"] == f"a{QUEUE_MAX + 4}"
    assert stat.S_IMODE(os.stat(queue_file(tmp_project)).st_mode) == 0o600


def test_a_slow_app_is_not_a_closed_room(room_server, mcp, monkeypatch, tmp_project):
    import urllib.error
    from ave.room import client
    def slow(*a, **k):
        raise urllib.error.URLError(socket.timeout("timed out"))
    monkeypatch.setattr(client._OPENER, "open", slow)
    got = mcp.call("room_show", artifact="art_ep", view="cut")
    assert got["room"] == "open" and "error" in got and not queue_file(tmp_project).exists()


def test_the_client_does_not_use_a_proxy(room_server, mcp, monkeypatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    assert mcp.call("room_show", artifact="art_ep", view="cut")["room"] == "open"


def test_a_lost_read_reply_loses_no_flags(room_server, mcp, monkeypatch):
    room_server.flag({"artifact": "art_ep", "start": 1.0, "end": 2.0, "note": "a"})
    first = mcp.call("room_read")
    assert [f["note"] for f in first["flags"]] == ["a"]
    mcp.app._room_client.ack = None                                            # the reply never reached Claude
    assert [f["note"] for f in mcp.call("room_read")["flags"]] == ["a"]
    assert mcp.call("room_read")["flags"] == []


def test_ack_from_an_earlier_run_does_not_drop_new_flags(room_server):
    room_server.flag({"artifact": "a", "start": 0, "end": 1, "note": "x"})
    status, got = post(room_server, "read", {"ack": "deadbeef:99"})
    assert [f["note"] for f in got["flags"]] == ["x"]


def test_stream_resyncs_when_last_event_id_is_ahead(room_server):
    room_server.bus.publish("stage", {"view": "cut"})
    evs = sse_get(room_server, last_id=50)
    assert [e["type"] for e in evs] == ["resync", "stage"] and evs[0]["data"]["reason"] == "future"


def test_stream_resyncs_when_last_event_id_fell_out_of_the_window(room_server):
    for i in range(520):
        room_server.bus.publish("x", {"i": i})
    evs = sse_get(room_server, last_id=3)
    assert evs[0]["type"] == "resync" and evs[0]["data"]["reason"] == "window" and evs[1]["seq"] == 21
    assert [e["type"] for e in sse_get(room_server, last_id=500)] == ["x"] * 20


def test_flags_are_validated_and_capped(room_server):
    for bad in ({"artifact": "a", "start": 2, "end": 1}, {"artifact": "a", "start": 0}, {"artifact": "a", "start": 0, "end": 1, "x": 1},
                {"artifact": "a", "start": "0", "end": 1}, {"artifact": "a", "start": 0, "end": 1, "note": "n" * 501}):
        assert post(room_server, "flag", bad)[0] == 400
    for _ in range(200):
        assert post(room_server, "flag", {"artifact": "a", "start": 0, "end": 1})[0] == 200
    assert post(room_server, "flag", {"artifact": "a", "start": 0, "end": 1})[0] == 429


def test_room_json_dir_is_private_and_not_removed_if_replaced(tmp_project):
    srv = StudioServer(open_studio(tmp_project))
    assert stat.S_IMODE(os.stat(room_file(tmp_project).parent).st_mode) == 0o700
    other = StudioServer(open_studio(tmp_project))                # a second app replaced room.json
    srv.server_close()
    assert json.loads(room_file(tmp_project).read_text())["token"] == other.token
    other.server_close()
    assert not room_file(tmp_project).exists()


def test_room_json_mode_is_tightened_before_the_token_is_written(tmp_project):
    f = room_file(tmp_project)
    f.parent.mkdir(parents=True)
    f.write_text("old")
    os.chmod(f, 0o644)
    srv = StudioServer(open_studio(tmp_project))
    try:
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    finally:
        srv.server_close()


def test_concurrent_streams_are_capped_and_a_disconnect_frees_the_slot(room_server):
    def open_stream():
        s = socket.create_connection(("127.0.0.1", room_server.port), timeout=2)
        s.sendall(f"GET /api/room/stream HTTP/1.1\r\nHost: 127.0.0.1:{room_server.port}\r\nX-AVE-Token: {room_server.token}\r\n\r\n".encode())
        return s, s.recv(4096)
    held = [open_stream() for _ in range(8)]
    assert all(b" 200 " in h[:20] for _, h in held)
    over, head = open_stream()
    assert b" 503 " in head[:20]
    over.close()
    held.pop()[0].close()
    for _ in range(50):                                           # the server notices the close within a second
        s, head = open_stream()
        if b" 200 " in head[:20]:
            break
        s.close()
        time.sleep(0.1)
    assert b" 200 " in head[:20]
    s.close()
    for s, _ in held:
        s.close()


def test_keep_records_the_choice_and_stays(room_server):
    room_server.room.show(artifact="art_ep", view="cut", version="rough.v1")
    room_server.offer("rough.v2", summary="Opens on the question")
    assert room_server.stage.compare_to == "rough.v1"                 # the version on stage is the one being replaced
    assert room_server.room.offer["summary"] == "Opens on the question"
    r = http_post(room_server, "/api/room/keep", {"version": "rough.v2"})
    assert r["click"]["action"] == "keep" and r["stage"]["version"] == "rough.v2" and r["stage"]["compare_to"] is None
    assert room_server.room.offer is None
    assert room_server.room.read()["clicks"][-1]["version"] == "rough.v2"


def test_go_back_restores_the_previous_version(room_server):
    room_server.room.show(artifact="art_ep", view="cut", version="rough.v1")
    room_server.offer("rough.v2")
    assert http_post(room_server, "/api/room/back", {}, status=400)                 # the version is required ...
    assert http_post(room_server, "/api/room/back", {"version": "rough.v1"}, status=400)   # ... and must be the one on stage
    assert room_server.stage.version == "rough.v2"
    r = http_post(room_server, "/api/room/back", {"version": "rough.v2"})
    assert r["stage"]["version"] == "rough.v1" and r["click"] == {**r["click"], "action": "back", "from": "rough.v2"}
    assert [e["type"] for e in room_server.bus.since(0)][-2:] in (["click", "stage"], ["stage", "preview"])
    assert http_post(room_server, "/api/room/back", {"version": "rough.v1"}, status=409)   # nothing earlier to go back to


def test_go_back_without_an_offer_uses_the_last_version_shown(room_server):
    room_server.room.show(artifact="art_ep", view="cut", version="rough.v1")
    room_server.room.show(artifact="art_ep", view="cut", version="rough.v3")
    assert http_post(room_server, "/api/room/back", {"version": "rough.v3"})["stage"]["version"] == "rough.v1"


def test_a_preview_that_cannot_be_found_says_so(room_server):
    room_server.offer("rough.v2", artifact="nowhere")
    deadline = time.monotonic() + 5
    while room_server.room.preview is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert room_server.room.preview["status"] == "failed" and "nowhere" in room_server.room.preview["message"]


def http_post(srv, path, body, status=200):
    import urllib.request
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}", method="POST", data=json.dumps(body).encode(),
                                 headers={"X-AVE-Token": srv.token, "Content-Type": "application/json"})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5) as r:
            assert r.status == status
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        assert e.code == status, e.read()
        return json.loads(e.read() or b"{}") or True


def test_offering_the_version_on_stage_again_keeps_what_it_replaces(room_server):
    room_server.room.show(artifact="art_ep", view="cut", version="rough.v1")
    room_server.offer("rough.v2")
    room_server.offer("rough.v2", summary="Same version, new words")
    assert room_server.stage.compare_to == "rough.v1"


def test_room_read_reports_the_open_offer_and_its_preview(room_server, mcp):
    mcp.call("room_show", artifact="art_ep", view="cut", version="rough.v1")
    assert mcp.call("room_read")["offer"] is None
    room_server.room.offer_version("art_ep", "rough.v2", summary="Cut the van story")          # no render thread: status stays pending
    got = mcp.call("room_read")["offer"]
    assert got == {"artifact": "art_ep", "version": "rough.v2", "compare_to": "rough.v1", "summary": "Cut the van story",
                   "preview": {"status": "pending", "reason": None, "span": None, "level_approximate": False}}
    room_server.room.set_preview({"artifact": "art_ep", "version": "rough.v2", "status": "rendering"})
    assert mcp.call("room_read")["offer"]["preview"]["status"] == "rendering"
    room_server.room.set_preview({"artifact": "art_ep", "version": "rough.v2", "status": "too_large", "span": [10.0, 200.0]})
    assert mcp.call("room_read")["offer"]["preview"] == {"status": "too_large", "reason": None, "span": [10.0, 200.0],
                                                         "level_approximate": False}
    room_server.room.set_preview({"artifact": "art_ep", "version": "rough.v2", "status": "ready", "span": [10.0, 12.0],
                                  "level_approximate": True, "url": "/x", "start": 2.0, "end": 20.0, "seek": 8.0})
    assert mcp.call("room_read")["offer"]["preview"] == {"status": "ok", "reason": None, "span": [10.0, 12.0], "level_approximate": True}
    room_server.room.set_preview({"artifact": "art_ep", "version": "rough.v2", "status": "failed", "message": "ffmpeg broke"})
    assert mcp.call("room_read")["offer"]["preview"]["reason"] == "ffmpeg broke"


def test_a_preview_that_cannot_be_found_reaches_room_read(room_server, mcp):
    room_server.offer("rough.v2", artifact="nowhere")
    deadline = time.monotonic() + 5
    while (mcp.call("room_read")["offer"] or {}).get("preview", {}).get("status") != "failed" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert mcp.call("room_read")["offer"]["preview"]["status"] == "failed"


def test_room_keep_said_in_chat_records_the_click_as_the_button_does(room_server, mcp):
    mcp.call("room_show", artifact="art_ep", view="cut", version="rough.v1")
    room_server.room.offer_version("art_ep", "rough.v2")
    wrong = mcp.call("room_keep", version="rough.v1")                        # the version must be the one on stage
    assert wrong["status"] == 400 and room_server.room.offer is not None
    got = mcp.call("room_keep", version="rough.v2")
    assert got["click"]["action"] == "keep" and got["stage"]["version"] == "rough.v2" and got["stage"]["compare_to"] is None
    assert room_server.room.offer is None
    read = mcp.call("room_read")
    assert read["clicks"][-1] == {**read["clicks"][-1], "action": "keep", "version": "rough.v2"} and read["offer"] is None


def test_room_back_said_in_chat_restores_the_version_it_replaced(room_server, mcp):
    mcp.call("room_show", artifact="art_ep", view="cut", version="rough.v1")
    room_server.room.offer_version("art_ep", "rough.v2")
    assert mcp.call("room_back", version="rough.v1")["status"] == 400 and room_server.stage.version == "rough.v2"
    got = mcp.call("room_back", version="rough.v2")
    assert got["stage"]["version"] == "rough.v1" and got["click"]["action"] == "back" and got["click"]["from"] == "rough.v2"
    assert room_server.room.offer is None
    assert mcp.call("room_back", version="rough.v1")["status"] == 409            # nothing earlier to go back to


def test_room_keep_and_back_are_not_queued_when_the_app_is_closed(tmp_project, mcp):
    closed = {"room": "closed", "queued": False}
    assert mcp.call("room_keep", version="rough.v2") == closed
    assert mcp.call("room_back", version="rough.v2") == closed
    assert not queue_file(tmp_project).exists()


def test_keep_and_back_say_how_they_arrived(room_server, mcp):
    mcp.call("room_show", artifact="art_ep", view="cut", version="rough.v1")
    room_server.room.offer_version("art_ep", "rough.v2")
    assert http_post(room_server, "/api/room/keep", {"version": "rough.v2"})["click"]["via"] == "button"     # the button's default
    room_server.room.offer_version("art_ep", "rough.v3")
    assert mcp.call("room_keep", version="rough.v3")["click"]["via"] == "chat"
    room_server.room.offer_version("art_ep", "rough.v4")
    assert mcp.call("room_back", version="rough.v4")["click"]["via"] == "chat"
    clicks = [e["data"] for e in room_server.bus.since(0) if e["type"] == "click"]
    assert [(c["action"], c["via"]) for c in clicks] == [("keep", "button"), ("keep", "chat"), ("back", "chat")]
    assert [c["via"] for c in mcp.call("room_read")["clicks"]] == ["button", "chat", "chat"]
    room_server.room.offer_version("art_ep", "rough.v5")
    assert http_post(room_server, "/api/room/keep", {"version": "rough.v5", "via": "agent"}, status=400)
    assert room_server.room.offer is not None


def test_keeping_the_kept_version_again_records_nothing_new(room_server, mcp):
    mcp.call("room_show", artifact="art_ep", view="cut", version="rough.v1")
    room_server.room.offer_version("art_ep", "rough.v2")
    first = http_post(room_server, "/api/room/keep", {"version": "rough.v2"})["click"]          # the director clicks
    seq = room_server.bus.latest
    again = mcp.call("room_keep", version="rough.v2")                                         # then Claude relays their words
    assert again["click"] == {**first, "already_kept": True}
    assert room_server.bus.latest == seq and len(room_server.room.read()["clicks"]) == 1
    room_server.room.offer_version("art_ep", "rough.v3")                                     # a new offer can be kept again
    room_server.room.back("rough.v3")
    assert mcp.call("room_keep", version="rough.v2")["click"].get("already_kept") is None    # back on v2 after Go back: a fresh keep
    assert len(room_server.room.read()["clicks"]) == 3


# ---- Task 12: views by hand, flags from the Script and Selects, and the review data

def test_the_director_switches_the_view_and_claude_sees_it(room_server, mcp):
    with pytest.raises(LookupError):
        room_server.room.set_view("script")
    room_server.room.show(artifact="ep", view="cut", version="rough.v1", position_s=3)
    assert post(room_server, "view", {"view": "script"})[0] == 200
    assert mcp.call("room_read")["stage"] == {**mcp.call("room_read")["stage"], "view": "script", "version": "rough.v1", "position_s": 3.0}
    assert post(room_server, "view", {"view": "nope"})[0] == 400


def test_flags_say_where_they_were_made_and_reads_say_which(room_server):
    assert post(room_server, "flag", {"artifact": "ep", "start": 8, "end": 12, "view": "script", "text": "thanks for having me"})[0] == 200
    assert post(room_server, "flag", {"artifact": "ep", "start": 1, "end": 2, "view": "selects", "clip": "c02"})[0] == 200
    for bad in ({"view": "timeline"}, {"text": "x" * 2001}, {"clip": ""}, {"clip": 3}):
        assert post(room_server, "flag", {"artifact": "ep", "start": 0, "end": 1, **bad})[0] == 400
    snap = room_server.room.snapshot()
    assert [f["view"] for f in snap["flags"]] == ["script", "selects"] and snap["flags"][1]["clip"] == "c02"
    got = room_server.room.read()
    seq = room_server.bus.latest
    room_server.room.read(got["ack"])
    ev = room_server.bus.since(seq)[-1]
    assert ev["type"] == "read" and ev["data"]["ids"] == [1, 2] and ev["data"]["flags_waiting"] == 0
    assert [f["id"] for f in room_server.room.snapshot()["flags_read"]] == [1, 2]


@pytest.fixture
def review_ep(tmp_project, synth_root, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))                 # device.json for the project log is never the real one
    import shutil
    from ave import clips
    from ave.episode import Episode
    shutil.copytree(synth_root, tmp_project / "ep")
    ep = Episode(tmp_project / "ep")
    pick = lambda ids, t: {"title": t, "hook": t, "sentence_ids": ids, "rationale": "Why: " + t,
                           "scores": {"hook": 8, "arc": 7, "insight": 7, "emotion": 6, "shareability": 7, "ending": 7, "speech_quality": 8}}
    clips.prepare(ep, stage="assembly", min_s=3, max_s=12)
    clips.propose(ep, [pick(["s000", "s001", "s002"], "A warm welcome"), pick(["s003", "s004"], "Systems that carry you")])
    return ep


def test_script_lines_name_the_speakers_and_keep_word_times(review_ep):
    from ave.room import review
    info = review.script_info(review_ep, "ep")
    assert info["speakers"] == ["Host", "Guest"] and len(info["lines"]) == 8
    l = info["lines"][1]
    assert (l["sp"], l["t0"], l["t1"]) == ("Guest", 8.0, 11.95) and len(l["w"]) == 2 * len(l["text"].split())
    assert [s["label"] for s in info["sources"]] == ["Host camera", "Guest camera"] and not any(s["ready"] for s in info["sources"])
    assert info["clips"]["c02"] == {"title": "Systems that carry you", "e0": 12.575, "e1": 25.225}
    assert "camA" not in json.dumps({k: v for k, v in info.items() if k != "lines"})


def test_review_copies_are_made_once_and_the_originals_are_never_served(room_server, review_ep):
    from ave.room import review
    assert http_get(room_server, "/api/room/source/ep/0").status == 404              # not made yet, and never the original
    assert post(room_server, "review", {"artifact": "ep", "key": "0"})[0] == 202
    deadline = time.monotonic() + 60
    while not review.source(review_ep, "0")["ready"] and time.monotonic() < deadline:
        time.sleep(0.1)
    copy = review.source(review_ep, "0")["copy"]
    assert copy.is_file() and copy.parent == review_ep.work / "room" / "review"
    r = http_get(room_server, "/api/room/source/ep/0", timeout=2)
    assert r.status == 200 and r.headers["Content-Type"] == "video/mp4"
    assert http_get(room_server, "/api/room/source/ep/9").status == 404
    assert http_get(room_server, "/api/room/source/..%2Fraw/0").status == 404
    assert post(room_server, "review", {"artifact": "ep", "key": "0"})[1]["status"] == "ready"


def test_selects_carry_their_state_and_a_cached_still(room_server, review_ep):
    from ave.room import review
    rows = {c["title"]: c for c in review.selects_info(review_ep, "ep")["clips"]}
    assert rows["A warm welcome"]["state"] == "new" and rows["A warm welcome"]["speakers"] == "Host and Guest"
    assert rows["A warm welcome"]["why"] == "Why: A warm welcome" and rows["Systems that carry you"]["play"]["kind"] == "source"
    r = http_get(room_server, "/api/room/thumb/ep/c01", timeout=10)
    assert r.status == 200 and r.headers["Content-Type"] == "image/jpeg"
    made = list((review_ep.work / "room" / "thumbs").glob("c01_*.jpg"))
    assert len(made) == 1
    before = made[0].stat().st_mtime_ns
    http_get(room_server, "/api/room/thumb/ep/c01", timeout=10)
    assert made[0].stat().st_mtime_ns == before
    assert http_get(room_server, "/api/room/thumb/ep/nope").status == 404


def test_keep_maybe_skip_use_the_clip_decision_path_and_the_project_log(room_server, review_ep, monkeypatch):
    from ave import humanpresence
    for k in humanpresence.AGENT_MARKERS:                    # the suite may run inside an agent harness
        monkeypatch.delenv(k, raising=False)
    from ave import clips
    from ave.project import Project
    from ave.room import review
    code, got = post(room_server, "selects/decide", {"artifact": "ep", "clip": "c02", "verdict": "keep"})
    assert code == 200 and got["state"] == "picked" and got["logged"] is False                # not a project yet: no log
    assert next(c for c in clips.load_clips(review_ep)["clips"] if c["id"] == "c02")["status"] == "approved"
    p = Project.create(review_ep.root, "synthetic", person="Maren")
    room_server.selects_write("selects/decide", {"artifact": "ep", "clip": "c02", "verdict": "keep"}, via="app")   # the app's bridge
    assert review.decide(review_ep, "c02", "pending")["state"] == "new"
    assert review.decide(review_ep, "c01", "maybe")["state"] == "maybe"
    assert review.decide(review_ep, "c01", "skip")["state"] == "skipped"
    evs = [e for e in p.read_events() if e["op"] == "clip.decided"]
    assert [(e["data"]["clip"], e["data"]["choice"], e["actor"], e["person"]) for e in evs] == [
        ("c02", "keep", "human", "Maren"), ("c02", "pending", "human", "Maren"), ("c01", "maybe", "human", "Maren"), ("c01", "skip", "human", "Maren")]
    ledger = [json.loads(l) for l in (review_ep.work / "learning" / "clip_decisions.jsonl").read_text().splitlines()]
    assert [x.get("verdict") or "withdrawn" for x in ledger] == ["keep", "keep", "withdrawn", "maybe", "skip"]
    assert [x["source"] for x in ledger] == ["relayed", "ui", "ui", "ui", "ui"]               # the first came over the room token
    st = p.state()
    assert st.clip_decisions["c01"]["choice"] == "skip" and st.clip_decisions["c02"]["choice"] == "pending"
    from ave.studio.ui.workspace import _sentence
    assert _sentence(st, evs[-1]).startswith("skipped “A warm welcome” for ")
    for bad in ({"clip": "c01", "verdict": "love"}, {"clip": "../x", "verdict": "keep"}):
        assert post(room_server, "selects/decide", {"artifact": "ep", **bad})[0] == 400
    assert post(room_server, "selects/decide", {"artifact": "ep", "clip": "c99", "verdict": "keep"})[0] == 404


def test_a_room_token_keep_is_claudes_and_never_a_human_kept_clip(room_server, review_ep, monkeypatch):
    """Final review I1: the room token is Claude's too. A keep or a nudge over HTTP is recorded by Claude (source "relayed",
    actor claude), so the shorts path never takes the clip as kept by a person; only the app's bridge records the person's."""
    from ave.project import Project
    from ave.studio.ingest_shorts import approval_problems
    from ave import clips, humanpresence
    for k in humanpresence.AGENT_MARKERS:
        monkeypatch.delenv(k, raising=False)
    p = Project.create(review_ep.root, "synthetic", person="Maren")
    assert post(room_server, "selects/decide", {"artifact": "ep", "clip": "c02", "verdict": "keep"})[0] == 200
    e = [x for x in p.read_events() if x["op"] == "clip.decided"][-1]
    assert e["actor"] == "claude" and e["data"]["choice"] == "keep"
    clip = next(c for c in clips.load_clips(review_ep)["clips"] if c["id"] == "c02")
    assert any("without a human terminal" in x for x in approval_problems(review_ep, clip))
    code, _ = post(room_server, "selects/nudge", {"artifact": "ep", "clip": "c01", "nudge": {"end": 1}})
    if code == 200:
        assert [x for x in p.read_events() if x["op"] == "clip.decided"][-1]["actor"] == "claude"
    room_server.selects_write("selects/decide", {"artifact": "ep", "clip": "c02", "verdict": "keep"}, via="app")
    clip = next(c for c in clips.load_clips(review_ep)["clips"] if c["id"] == "c02")
    assert approval_problems(review_ep, clip) == [] and [x for x in p.read_events() if x["op"] == "clip.decided"][-1]["actor"] == "human"


def test_two_decisions_at_once_never_lose_one(review_ep):
    from ave import clips
    from ave.room import review
    for n in range(6):
        verdicts = (("keep", "skip"), ("skip", "maybe"))[n % 2]
        errs = []

        def go(cid, v):
            try:
                review.decide(review_ep, cid, v)
            except Exception as e:                       # noqa: BLE001
                errs.append(e)
        ts = [threading.Thread(target=go, args=a) for a in (("c01", verdicts[0]), ("c02", verdicts[1]))]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert not errs
        got = {c["id"]: review._verdict(c) for c in clips.load_clips(review_ep)["clips"]}
        assert got == {"c01": verdicts[0], "c02": verdicts[1]}, n


def test_the_stage_and_flags_carry_their_time_base(room_server):
    room_server.room.show(artifact="ep", view="cut", position_s=17.5)
    assert room_server.stage.time_base == "cut"
    room_server.room.set_view("script")
    assert room_server.stage.time_base == "cut"                          # the position still counts cut seconds
    room_server.room.show(artifact="ep", view="script", position_s=8)
    assert room_server.stage.time_base == "transcript"
    for view, base in ((None, "cut"), ("cut", "cut"), ("script", "transcript"), ("selects", "transcript")):
        room_server.room.flag({"artifact": "ep", "start": 1, "end": 2, **({"view": view} if view else {})})
        assert room_server.room.read()["flags"][-1]["time_base"] == base


def test_script_clocks_carry_the_sync_map(review_ep):
    from ave import editdoc
    from ave.room import review
    sync = json.loads(review_ep.artifact("sync.json").read_text())
    sync["files"]["raw/camB.mp4"].update(drift_ppm=80.0, sync_map=[[0.0, 1.0], [10.0, 1.2], [25.0, 1.1]])
    review_ep.artifact("sync.json").write_text(json.dumps(sync))
    for p in review_ep.edits.glob("*.json"):
        p.unlink()                                                        # no edit: the clock comes from sync.json alone
    clock = next(s for s in review.script_info(review_ep, "ep")["sources"] if s["label"] == "Guest camera")["clock"]
    assert clock["ft"] == [0.0, 10.0, 25.0] and clock["et"] == [1.0, 11.2, 26.1] and abs(clock["k"] - 1.00008) < 1e-9
    assert editdoc._knots(sync["files"]["raw/camB.mp4"])


def test_review_copies_are_rebuilt_when_the_original_changes_and_leave_nothing_half_made(review_ep, monkeypatch):
    import os
    from ave.room import review
    first = review.make_review_copy(review_ep, "0")
    (first.parent / "junk.partial.mp4").write_bytes(b"x")
    raw = review_ep.root / "raw" / "camA.mp4"
    os.utime(raw, ns=(raw.stat().st_atime_ns, raw.stat().st_mtime_ns + 5_000_000_000))
    assert not review.source(review_ep, "0")["ready"]
    second = review.make_review_copy(review_ep, "0")
    assert second != first and second.is_file() and not first.exists()
    assert not list(first.parent.glob("*.partial*"))
    monkeypatch.setattr(review.shutil, "disk_usage", lambda p: type("U", (), {"free": 10})())
    with pytest.raises(review.CopyError, match="not enough free space"):
        review.make_review_copy(review_ep, "1")


def test_the_state_read_carries_each_review_copy_job(room_server, review_ep):
    assert post(room_server, "review", {"artifact": "ep", "key": "1"})[0] == 202
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        jobs = json.loads(http_get(room_server, "/api/room/state").body)["reviews"]
        if jobs and jobs[0]["status"] == "ready":
            break
        time.sleep(0.2)
    assert jobs[0]["key"] == "1" and jobs[0]["status"] == "ready" and "url" in jobs[0]


def test_old_stills_of_a_moved_clip_are_pruned(review_ep):
    from ave.room import review
    old = review_ep.work / "room" / "thumbs" / "c01_1.jpg"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"old")
    new = review.thumb(review_ep, "c01")
    assert new.is_file() and not old.exists()


def test_finder_sections_say_why_in_words(review_ep):
    from ave.room import review
    data = json.loads(review_ep.artifact("clips.json").read_text())
    data["clips"][1].update(origin="heuristic", rationale="hook + claim; hook at 0.1s; 23s", tags=["claim", "quotable"])
    review_ep.artifact("clips.json").write_text(json.dumps(data))
    row = next(r for r in review.selects_info(review_ep, "ep")["clips"] if r["id"] == data["clips"][1]["id"])
    assert row["why"] == "The clip finder also found this: claim, quotable." and row["origin"] == "finder"


def test_two_processes_deciding_at_once_never_lose_an_update(review_ep):
    """Both go through clips.decide (as `ave clips decide`, `ave ui` and the Room do), whose lock spans processes."""
    import subprocess
    import sys
    from ave import clips
    code = """import sys
from ave import clips
from ave.episode import Episode
ep, cid, last = Episode(sys.argv[1]), sys.argv[2], sys.argv[3]
for n in range(15):
    v = last if n == 14 else ("skip" if n % 2 else "keep")
    r = clips.decide(ep, [{"id": cid, "verdict": v}], by="Maren")
    assert not r["errors"], r
"""
    ps = [subprocess.Popen([sys.executable, "-c", code, str(review_ep.root), cid, last]) for cid, last in (("c01", "keep"), ("c02", "maybe"))]
    assert [p.wait(120) for p in ps] == [0, 0]
    got = {c["id"]: c for c in clips.load_clips(review_ep)["clips"]}
    assert got["c01"]["status"] == "approved" and got["c02"]["status"] == "proposed" and got["c02"].get("maybe")
    log = [json.loads(l) for l in (review_ep.work / "clips_decisions.jsonl").read_text().splitlines()]
    assert len([x for x in log if x["applied"]]) == 30


def test_a_wait_that_fails_leaves_the_line_and_the_next_copy_runs(review_ep, monkeypatch):
    from ave.room import review
    monkeypatch.setattr(review, "WAITING_EVERY_S", 0.05)
    other = object()                                                    # another camera's copy at the front of the line
    with review._QUEUE_CV:
        review._QUEUE.append(other)

    def boom(n):
        raise RuntimeError("the page went away")
    with pytest.raises(RuntimeError):
        review.make_review_copy(review_ep, "0", waiting=boom)
    with review._QUEUE_CV:
        assert review._QUEUE == [other]                                 # the failed copy left the line
        review._QUEUE.remove(other)
        review._QUEUE_CV.notify_all()
    calls = []
    assert review.make_review_copy(review_ep, "1", waiting=calls.append).is_file() and calls == []
    assert review._QUEUE == []
