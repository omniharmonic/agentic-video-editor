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
    return SimpleNamespace(status=int(lines[0].split()[1]), headers=hdrs, body=body.decode())


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
    assert [e["type"] for e in room_server.bus.since(0)][-2:] == ["stage", "offer"]


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
        assert [e["type"] for e in srv.bus.since(0)][-1] == "offer"
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
