"""The studio server and API (spec 7.3, 7.6, 10.6), over a real server on an ephemeral localhost port with the stdlib http client.

Security: token, Host, Origin (POST and PUT), traversal, dot-files, what /files/ serves, body and upload caps, the approval PIN
(wrong, locked, replayed, never on disk or in a response) and the view-event rule. Behavior: every endpoint against a real studio, the
501 for hooks nobody registered yet, the 409 shape, the change feed, and every committed fixture served through `FixtureStudio`.
"""

import http.client
import json
import os
import threading
from pathlib import Path

import pytest

from ave.publish import queue as pq
from ave.studio import state
from ave.studio.events import Actor
from ave.studio.service import FixtureStudio
from ave.studio.ui.server import StudioServer
from studio_helpers import DATA

PIN = "424242"
FIXTURES = sorted((DATA / "api").glob("*.json"))


class Client:
    def __init__(self, srv):
        self.srv = srv
        self.seen: list[bytes] = []          # every response body, to prove the PIN never comes back

    def req(self, method, path, body=None, headers=None, token="header", host=None, raw=None):
        h = dict(headers or {})
        h["Host"] = host or f"127.0.0.1:{self.srv.port}"
        if token == "header":
            h["X-AVE-Token"] = self.srv.token
        elif token == "cookie":
            h["Cookie"] = f"ave_ui={self.srv.token}"
        elif token == "query":
            path += ("&" if "?" in path else "?") + "t=" + self.srv.token
        elif token:
            h["X-AVE-Token"] = token
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=10)
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in h.items():
            c.putheader(k, v)
        if "Content-Length" not in h:
            c.putheader("Content-Length", str(len(data) if data else 0))
        c.endheaders(data)
        r = c.getresponse()
        out = r.read()
        c.close()
        self.seen.append(out)
        return r.status, dict(r.getheaders()), out

    def api(self, method, route, body=None, **kw):
        s, h, raw = self.req(method, "/api/studio/" + route, body, **kw)
        return s, (json.loads(raw) if raw else None)

    def get(self, route, **kw):
        return self.api("GET", route, **kw)

    def post(self, route, body=None, **kw):
        return self.api("POST", route, {} if body is None else body, **kw)


def serve(studio, **kw):
    s = StudioServer(studio, port=0, by="tester", **kw)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


@pytest.fixture
def srv(built):
    s = serve(built.studio, pin=PIN)
    yield s
    s.shutdown()
    s.server_close()


@pytest.fixture
def cl(srv):
    return Client(srv)


@pytest.fixture
def fx_cl():
    s = serve(FixtureStudio())
    yield Client(s)
    s.shutdown()
    s.server_close()


def review_target(built):
    """(post id, variant id, queue item, sha) of the first variant in review."""
    pid = built.posts["in_review"]
    post = built.studio.post(pid)
    v = post["variants"][0]
    item = state.load_queue_item(built.sp, v)
    return pid, v["id"], item, pq.current_sha(built.sp, item)


# ---- binding, token, Host, Origin ------------------------------------------------------------------------------------------

def test_binds_loopback_only(srv):
    assert srv.server_address[0] == "127.0.0.1"
    assert srv.app_url().startswith(f"http://127.0.0.1:{srv.port}/app/?t=")
    assert srv.url().startswith(f"http://127.0.0.1:{srv.port}/tools/?t=")              # More tools


@pytest.mark.parametrize("path", ["/", "/static/style.css", "/api/studio/summary", "/files/library/assets/x.png"])
def test_every_request_needs_the_token(cl, path):
    assert cl.req("GET", path, token=None)[0] == 401
    assert cl.req("GET", path, token="wrong")[0] == 401
    assert cl.req("GET", path, token="query")[0] in (200, 303, 404)       # a good token passes the gate


def test_bad_host_refused(cl, srv):
    assert cl.req("GET", "/api/studio/summary", host="evil.example")[0] == 403
    assert cl.req("GET", "/api/studio/summary", host=f"127.0.0.1:{srv.port + 1}")[0] == 403
    assert cl.req("GET", "/api/studio/summary", host=f"localhost:{srv.port}")[0] == 200


def test_foreign_origin_refused_for_post_and_put(cl, srv):
    h = {"Origin": "http://evil.example"}
    assert cl.req("POST", "/api/studio/posts", {"idea": "x"}, headers=h)[0] == 403
    assert cl.req("PUT", "/api/studio/workbench/upload?name=a.png", raw=b"abc", headers=h)[0] == 403
    ok = {"Origin": f"http://127.0.0.1:{srv.port}"}
    assert cl.req("POST", "/api/studio/posts", {"idea": "x"}, headers=ok)[0] == 201


def test_first_visit_parks_token_in_cookie_and_serves_the_shell(cl):
    s, h, _ = cl.req("GET", "/", token="query")
    assert s == 303 and "ave_ui=" in h["Set-Cookie"] and "HttpOnly" in h["Set-Cookie"]
    s, h, _ = cl.req("GET", "/", token="cookie")
    assert s == 302 and h["Location"] == "/app/"                                   # the old shell is retired: the app is the front door
    s, h, body = cl.req("GET", "/tools/", token="cookie")
    assert s == 200 and b"More tools" in body
    assert "default-src 'self'" in h["Content-Security-Policy"] and h["Cache-Control"] == "no-store"
    s, h, css = cl.req("GET", "/static/style.css", token="cookie")
    assert s == 200 and h["Content-Type"].startswith("text/css")        # the shared ave ui tokens


# ---- traversal and what /files/ serves -------------------------------------------------------------------------------------

def test_static_traversal_and_dotfiles(cl):
    for p in ("/static/../server.py", "/static/..%2fserver.py", "/static/%2e%2e/api.py", "/static/.hidden", "/static//etc/passwd",
              "/static/index.html%00.png", "/static/..%5cserver.py", "/static/api.py"):
        assert cl.req("GET", p)[0] == 404, p


def test_files_serve_only_library_exports_work(built, cl):
    sp = built.sp
    (sp.root / "inbox").mkdir(exist_ok=True)
    (sp.root / "inbox" / "raw.mov").write_bytes(b"raw footage")
    (sp.exports / "card.txt").write_text("0123456789" * 10)
    (sp.exports / ".secret").write_text("no")
    asset = built.assets[0]
    assert cl.req("GET", "/files/" + asset["file"])[0] == 200
    assert cl.req("GET", "/files/exports/card.txt")[0] == 200
    for p in ("/files/inbox/raw.mov", "/files/studio.json", "/files/accounts/ig-main.yaml", "/files/posts/p_fx003.json",
              "/files/ledger/events.jsonl", "/files/publish/queue/x.json", "/files/exports/.secret",
              "/files/exports/../studio.json", "/files/exports/..%2f..%2fstudio.json", "/files/%2e%2e/studio.json",
              "/files/exports/%00", "/files//etc/passwd", "/api/studio/files/studio.json", "/api/studio/files/inbox/raw.mov"):
        assert cl.req("GET", p)[0] in (403, 404), p


def test_files_symlink_escape_blocked(built, cl, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    os.symlink(outside, built.sp.exports / "link.txt")
    assert cl.req("GET", "/files/exports/link.txt")[0] == 404


def test_ranged_library_file(built, cl):
    asset = built.assets[0]
    s, h, body = cl.req("GET", "/files/" + asset["file"], headers={"Range": "bytes=0-9"})
    assert s == 206 and len(body) == 10 and h["Content-Range"].startswith("bytes 0-9/")
    assert cl.req("GET", "/files/" + asset["file"], headers={"Range": "bytes=999999999-"})[0] == 416


def test_episode_api_is_not_exposed(cl):
    for p in ("/api/dashboard", "/api/jobs", "/api/publish", "/api/studio/jobs", "/api/studio/nope"):
        assert cl.req("GET", p)[0] == 404, p


# ---- body caps -------------------------------------------------------------------------------------------------------------

def test_oversize_json_body_refused_unread(cl):
    s, h, raw = cl.req("POST", "/api/studio/posts", headers={"Content-Length": "5000000", "Content-Type": "application/json"}, raw=b"")
    assert s == 413 and json.loads(raw)["code"] == "body.too_large"
    s, _, raw = cl.req("POST", "/api/studio/posts", raw=b"not json")
    assert s == 400 and json.loads(raw)["code"] == "bad_request"
    s, _, raw = cl.req("POST", "/api/studio/posts", raw=b"[1]")
    assert s == 400


def test_upload_cap_and_framing(built):
    s = serve(built.studio, pin=PIN, max_upload=1024)
    try:
        c = Client(s)
        st, _, raw = c.req("PUT", "/api/studio/workbench/upload?name=a.png", headers={"Content-Length": "1025"}, raw=b"")
        assert st == 413 and json.loads(raw)["code"] == "body.too_large"
        st, _, raw = c.req("PUT", "/api/studio/workbench/upload?name=a.png", headers={"Content-Length": str(10 ** 12)}, raw=b"")
        assert st == 413
        for bad in ("", "../x.png", "a/b.png", ".hidden", "a\\b.png"):
            st, _, _ = c.req("PUT", "/api/studio/workbench/upload?name=" + bad.replace("/", "%2F").replace("\\", "%5C"), raw=b"abc")
            assert st == 400, bad
        assert c.req("PUT", "/api/studio/workbench/upload?name=a.png", raw=b"")[0] == 400                      # empty
        assert c.req("PUT", "/api/studio/library/other", raw=b"abc")[0] == 404
    finally:
        s.shutdown()
        s.server_close()
    assert not list((built.sp.work / "uploads").glob("*"))


def test_truncated_upload_is_refused(built, srv):
    built.studio.register("upload", lambda *a, **k: pytest.fail("must not be called"))
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    c.putrequest("PUT", "/api/studio/workbench/upload?name=a.png", skip_host=True)
    for k, v in (("Host", f"127.0.0.1:{srv.port}"), ("X-AVE-Token", srv.token), ("Content-Length", "1000")):
        c.putheader(k, v)
    c.endheaders(b"only a few bytes")
    c.sock.shutdown(1)                                     # half-close: the body ends early
    r = c.getresponse()
    assert r.status == 400
    c.close()
    assert not list((built.sp.work / "uploads").glob("*"))


# ---- the approval PIN and the view rule -----------------------------------------------------------------------------------

# No HTTP route approves or records a view any more (Task 24): the app's native approval sheet calls these two in process, after
# its device-owner check (desktop_approvals.Approvals). These helpers make the same in-process calls, so every rule the old
# browser approval was held to (the PIN, the lock, the view of the exact sha, the replay, the typed mode) is still checked here.
def _inproc(fn):
    from ave.studio.ui.api import map_error
    try:
        return 200, fn()
    except Exception as e:                                    # what the HTTP layer used to answer
        se = map_error(e, None)
        return se.status, se.body()


def view(cl, pid, vid, sha, ms=2500):
    return _inproc(lambda: cl.srv.app.record_view(pid, {"variant": vid, "sha": sha, "displayed_ms": ms}, via="native-sheet"))


def approve(cl, pid, vid, sha, pin=PIN, by="maren", **extra):
    return _inproc(lambda: cl.srv.app.approve({"items": [{"post": pid, "variant": vid, "sha": sha}], "by": by, "pin": pin, **extra}))


def approve_items(cl, body):
    return _inproc(lambda: cl.srv.app.approve(body))


def test_approve_needs_the_pin(built, cl):
    pid, vid, item, sha = review_target(built)
    assert view(cl, pid, vid, sha)[0] == 200
    for bad in (None, "", "000000", 424242, "42424"):
        st, body = approve(cl, pid, vid, sha, pin=bad)
        assert st == 403 and body["code"] in ("pin.wrong", "pin.locked"), bad
    assert pq.load(built.sp, item["id"])["status"] == "awaiting_approval"


def test_pin_locks_after_five_wrong_even_for_the_right_pin(built, cl):
    pid, vid, item, sha = review_target(built)
    view(cl, pid, vid, sha)
    codes = [approve(cl, pid, vid, sha, pin="111111")[1]["code"] for _ in range(5)]
    assert codes == ["pin.wrong"] * 5
    st, body = approve(cl, pid, vid, sha, pin=PIN)
    assert st == 403 and body["code"] == "pin.locked"
    assert pq.load(built.sp, item["id"])["status"] == "awaiting_approval"
    assert cl.get("summary")[1]["ui"]["pin_locked"] is True


def test_approval_needs_a_view_of_the_exact_sha(built, cl):
    pid, vid, item, sha = review_target(built)
    st, body = approve(cl, pid, vid, sha)
    assert st == 200 and body["approved"] == [] and "not viewed" in body["refused"][0]["reason"]
    view(cl, pid, vid, sha, ms=1500)                                           # on screen for less than 2 s
    assert "not viewed" in approve(cl, pid, vid, sha)[1]["refused"][0]["reason"]
    view(cl, pid, vid, sha, ms=700)                                            # reports accumulate by max, never reset the time
    assert approve(cl, pid, vid, sha)[1]["approved"] == []
    other = "0" * 64
    assert "not viewed" in approve(cl, pid, vid, other)[1]["refused"][0]["reason"]
    assert pq.load(built.sp, item["id"])["status"] == "awaiting_approval"


def test_view_of_a_sha_the_server_did_not_serve_is_refused(built, cl):
    pid, vid, item, sha = review_target(built)
    st, body = view(cl, pid, vid, "a" * 64)
    assert st == 409 and body["code"] == "view.stale_sha"
    assert view(cl, pid, vid, "short")[0] == 400
    assert approve(cl, pid, vid, "a" * 64)[1]["refused"]


def test_approve_records_studio_ui_source_and_never_sends(built, cl):
    pid, vid, item, sha = review_target(built)
    assert view(cl, pid, vid, sha)[0] == 200
    st, body = approve(cl, pid, vid, sha, by="Maren")
    assert st == 200 and body["refused"] == [] and len(body["approved"]) == 1
    rec = body["approved"][0]
    assert rec["source"] == "studio-ui" and rec["by"] == "Maren" and rec["sha"] == sha and rec["queue_id"] == item["id"]
    q = pq.load(built.sp, item["id"])
    assert q["status"] == "approved" and q["approvals"][-1]["source"] == "studio-ui" and q["approvals"][-1]["by"] == "Maren"
    assert not any(p.sent for p in built.publishers.values())
    assert built.studio.post(pid)["variants"][0]["status"] == "approved"
    events = cl.get("changes?cursor=ev:0")[1]["events"]
    assert any(e["op"] == "approved" and e["detail"]["source"] == "studio-ui" and e["actor"] == {"kind": "human", "name": "Maren"} for e in events)
    assert any(e["op"] == "viewed" and e["after_sha"] == sha for e in events)


def test_replaying_an_approval_is_refused(built, cl):
    pid, vid, item, sha = review_target(built)
    view(cl, pid, vid, sha)
    assert approve(cl, pid, vid, sha)[1]["approved"]
    st, body = approve(cl, pid, vid, sha)                                       # same request again: the view was consumed
    assert st == 200 and body["approved"] == [] and "not viewed" in body["refused"][0]["reason"]
    view(cl, pid, vid, sha)                                                     # even viewing again: the queue item is already approved
    again = approve(cl, pid, vid, sha)[1]
    assert again["approved"] == [] and "cannot approve" in again["refused"][0]["reason"]
    assert len(pq.load(built.sp, item["id"])["approvals"]) == 1


def test_a_pin_from_another_run_does_not_work(built, srv, cl):
    other = serve(built.studio)                                                 # a second server: its own random PIN
    try:
        pid, vid, item, sha = review_target(built)
        view(cl, pid, vid, sha)
        assert approve(cl, pid, vid, sha, pin=other.app.pin.reveal())[0] == 403
        assert other.app.pin.reveal() != PIN
    finally:
        other.shutdown()
        other.server_close()


def test_media_changed_after_viewing_is_refused(built, cl):
    pid, vid, item, sha = review_target(built)
    view(cl, pid, vid, sha)
    f = pq.resolve_files(built.sp, item)[0]
    f.write_bytes(f.read_bytes() + b"tamper")
    refused = approve(cl, pid, vid, sha)[1]["refused"]
    assert refused and "changed" in refused[0]["reason"]
    assert pq.load(built.sp, item["id"])["status"] == "awaiting_approval"


def test_typed_mode_wants_typed_hex_not_a_view(built, cl):
    meta = built.sp.read_meta()
    meta["approval"]["mode"] = "typed"
    (built.sp.root / "studio.json").write_text(json.dumps(meta))
    pid, vid, item, sha = review_target(built)
    assert "typed" in approve(cl, pid, vid, sha)[1]["refused"][0]["reason"]
    body = approve_items(cl, {"items": [{"post": pid, "variant": vid, "sha": sha, "confirm": sha[:5]}], "by": "maren", "pin": PIN})[1]
    assert "typed" in body["refused"][0]["reason"]
    body = approve_items(cl, {"items": [{"post": pid, "variant": vid, "sha": sha, "confirm": sha[:10]}], "by": "maren", "pin": PIN})[1]
    assert len(body["approved"]) == 1
    assert approve(cl, pid, vid, sha, pin="000000")[0] == 403                    # typed mode still needs the PIN


def test_the_pin_is_never_in_a_response_a_file_or_the_log(built, srv, cl, capsys):
    pid, vid, item, sha = review_target(built)
    view(cl, pid, vid, sha)
    approve(cl, pid, vid, sha)
    approve(cl, pid, vid, sha, pin="999999")
    cl.post("pause", {})
    cl.get("summary")
    cl.get("posts/" + pid)
    for body in cl.seen:
        assert PIN.encode() not in body and b"999999" not in body
    for root, _, files in os.walk(built.sp.root.parent):
        for f in files:
            data = (Path(root) / f).read_bytes()
            assert PIN.encode() not in data and b"999999" not in data, (root, f)
    out = capsys.readouterr()
    assert PIN not in out.out + out.err
    assert PIN not in repr(srv.app.pin) and PIN not in repr(srv.app)


# ---- starting the server is a human act -------------------------------------------------------------------------------------

class Args:
    def __init__(self, dir=None, fixtures=False, port=0):
        self.dir, self.fixtures, self.port, self.open, self.by = dir, fixtures, port, False, "tester"


def test_start_refuses_without_a_human_terminal(built, monkeypatch, capsys):
    from ave import humanpresence
    from ave.studio.ui import server as S
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: False)
    monkeypatch.setattr(S.StudioServer, "serve_forever", lambda self: pytest.fail("must not serve"))
    with pytest.raises(SystemExit) as e:
        S.run_cli(Args(dir=str(built.sp.root)))
    assert "human act" in str(e.value) and "ave studio ui" in str(e.value)
    assert capsys.readouterr().out == ""                                       # no URL, no PIN


def test_start_prints_the_app_and_never_the_pin(built, monkeypatch, capsys):
    from ave import humanpresence
    from ave.studio.ui import server as S
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: True)
    seen = {}
    monkeypatch.setattr(S.StudioServer, "serve_forever", lambda self: seen.setdefault("pin", self.app.pin.reveal()))
    assert S.run_cli(Args(dir=str(built.sp.root))) == 0
    out = capsys.readouterr().out
    assert seen["pin"] not in out and "/app/?t=" in out and "nothing can be approved" in out     # a browser approves nothing
    assert not any(seen["pin"].encode() in p.read_bytes() for p in built.sp.root.rglob("*") if p.is_file())


def test_fixtures_mode_needs_no_terminal(monkeypatch, capsys):
    from ave import humanpresence
    from ave.studio.ui import server as S
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: False)
    monkeypatch.setattr(S.StudioServer, "serve_forever", lambda self: None)
    assert S.run_cli(Args(fixtures=True)) == 0
    assert "FIXTURES" in capsys.readouterr().out


def test_cli_registers_ui():
    from ave.commands.studio import command_names
    assert "ui" in command_names()


# ---- endpoints against a real studio -----------------------------------------------------------------------------------------

def test_reads(built, cl):
    st, s = cl.get("summary")
    assert st == 200 and s["posts"] == 12 and s["accounts"] == 3 and s["ui"]["fixtures"] is False
    st, a = cl.get("accounts")
    assert [x["id"] for x in a["accounts"]] == ["fb-page", "ig-alt", "ig-main"] and not any(x["live"] for x in a["accounts"])
    st, p = cl.get("posts?stage=active")
    assert st == 200 and p["total"] == len(p["posts"]) > 0 and all(x["stage"] == "active" for x in p["posts"])
    assert cl.get("posts?status=scheduled")[1]["total"] == 2
    assert cl.get("posts?status=bogus")[0] == 400
    assert cl.get("posts?q=Idea%20number")[1]["total"] == 2
    pid = built.posts["in_review"]
    st, d = cl.get("posts/" + pid)
    v = d["post"]["variants"][0]
    assert st == 200 and v["status"] == "in_review" and d["queue"][v["id"]]["status"] == "awaiting_approval"
    assert d["feedback"] == [] and isinstance(d["events"], list)
    assert cl.get("posts/p_nope")[0] == 404
    assert cl.get("posts/..%2f..%2fstudio")[0] in (400, 404)
    assert cl.get("feedback")[1] == {"feedback": []}
    assert cl.get("alerts")[1] == {"alerts": []}
    assert cl.get("feedback?status=bogus")[0] == 400


def test_unregistered_hooks_are_501(built, cl):
    pid = built.posts["in_review"]
    for method, route, body in (("GET", "calendar?from=2026-11-01&to=2026-11-30", None), ("GET", "grid/ig-main", None),
                                ("GET", f"posts/{pid}/preview/v_003", None), ("GET", "library", None), ("GET", "library/a_x", None),
                                ("GET", "broll/s_1", None),
                                ("POST", f"posts/{pid}/move", {"variant": "v_003", "expected_rev": 1, "to": {"local": "2026-11-03T09:00"}}),
                                ("POST", f"posts/{pid}/swap", {"variant": "v_003", "expected_rev": 1, "with": "v_x"}),
                                ("POST", f"posts/{pid}/unschedule", {"variant": "v_003", "expected_rev": 1}),
                                ("POST", f"posts/{pid}/swap_asset", {"variant": "v_003", "expected_rev": 1, "index": 0, "asset": "a_x"}),
                                ("POST", "broll/search", {"query": "bread"}), ("POST", "broll/pick", {"search": "s", "candidate": "c", "by": "maren"}),
                                ("POST", "schedule/proposals/sp_1/accept", {})):
        st, body = cl.api(method, route, body)
        assert st == 501 and body["code"] == "not_implemented" and "error" in body, route


def test_registered_hooks_are_called_with_facade_arguments(built, cl):
    calls = []
    built.studio.register("calendar", lambda studio, start, end, accounts=None: calls.append((str(start), str(end), accounts)) or {"days": {}})
    assert cl.get("calendar?from=2026-11-01&to=2026-11-30&accounts=ig-main,fb-page")[1] == {"days": {}}
    assert calls == [("2026-11-01", "2026-11-30", ["ig-main", "fb-page"])]
    assert cl.get("calendar?from=nope&to=2026-11-30")[0] == 400
    assert cl.get("calendar")[0] == 400
    pid = built.posts["in_review"]

    def move(studio, post_id, variant, to, *, expected_rev, actor, snap):
        calls.append((post_id, variant, to, expected_rev, actor, snap))
        post = studio.post(post_id)
        return {"post": post, "warnings": ["w"]}
    built.studio.register("move", move)
    st, body = cl.post(f"posts/{pid}/move", {"variant": "v_003", "expected_rev": 1, "to": {"slot": "ig-main:2026-11-03T09:00"}, "by": "Maren"})
    assert st == 200 and body["warnings"] == ["w"] and body["post"]["id"] == pid
    assert calls[-1] == (pid, "v_003", {"slot": "ig-main:2026-11-03T09:00"}, 1, Actor("human", "Maren"), "slot")
    assert cl.post(f"posts/{pid}/move", {"variant": "v_003", "to": {}})[0] == 400          # expected_rev is mandatory


def test_hook_conflict_is_409_with_latest(built, cl):
    class Conflict(ValueError):
        code = "conflict.slot"
        latest = {"id": "p_x", "rev": 9}
    built.studio.register("move", lambda studio, *a, **k: (_ for _ in ()).throw(Conflict("slot taken by p_y")))
    st, body = cl.post(f"posts/{built.posts['in_review']}/move", {"variant": "v", "expected_rev": 1, "to": {"slot": "s"}})
    assert st == 409 and body == {"error": "slot taken by p_y", "code": "conflict.slot", "latest": {"id": "p_x", "rev": 9}}


def test_create_edit_conflict_and_change_feed(built, cl):
    cursor = cl.get("changes")[1]["cursor"]
    st, body = cl.post("posts", {"idea": "A new idea", "copy": {"base": "Hello"}, "by": "Maren"})
    assert st == 201, body
    assert body["post"]["rev"] == 1 and body["post"]["idea"]["text"] == "A new idea"
    pid = body["post"]["id"]
    st, body = cl.post(f"posts/{pid}/edit", {"expected_rev": 1, "patch": {"copy.base": "Hello again"}, "by": "Maren"})
    assert st == 200 and body["post"]["rev"] == 2 and body["post"]["copy"]["base"] == "Hello again" and body["warnings"] == []
    fb = cl.get("feedback?status=all")[1]["feedback"]
    assert [f["kind"] for f in fb] == ["human_edit"] and fb[0]["by"] == "Maren" and fb[0]["source"] == "ui"
    st, body = cl.post(f"posts/{pid}/edit", {"expected_rev": 1, "patch": {"copy.base": "stale"}})
    assert st == 409 and body["code"] == "conflict.rev" and body["latest"]["rev"] == 2 and body["latest"]["copy"]["base"] == "Hello again"
    assert cl.post(f"posts/{pid}/edit", {"expected_rev": 2, "patch": {"id": "p_other"}})[0] == 400
    assert cl.post(f"posts/{pid}/edit", {"expected_rev": 2, "patch": {"nothing.here": 1}})[0] == 400
    assert cl.post(f"posts/{pid}/edit", {"patch": {"copy.base": "x"}})[0] == 400
    assert cl.post(f"posts/{pid}/edit", {"expected_rev": "2", "patch": {"copy.base": "x"}})[0] == 400
    assert cl.post("posts/p_nope/edit", {"expected_rev": 1, "patch": {"copy.base": "x"}})[0] == 404
    st, feed = cl.get("changes?cursor=" + cursor)
    assert st == 200 and [e["op"] for e in feed["events"]] == ["create", "edit"] and feed["cursor"] != cursor
    assert all(e["actor"]["name"] == "Maren" for e in feed["events"])
    assert cl.get("changes?cursor=" + feed["cursor"])[1]["events"] == []                # nothing new since
    assert cl.get("changes?cursor=garbage")[0] == 400


def test_send_back_writes_feedback_per_kind(built, cl):
    pid = built.posts["in_review"]
    post = cl.get("posts/" + pid)[1]["post"]
    st, body = cl.post(f"posts/{pid}/feedback", {"variant": post["variants"][0]["id"], "kinds": ["rework_copy", "retime"], "note": "too salesy",
                                                  "structured": {"copy": {"field": "caption", "before": "a", "after": None}}, "by": "Maren"})
    assert st == 201 and [f["kind"] for f in body["feedback"]] == ["rework_copy", "retime"]
    assert all(f["by"] == "Maren" and f["source"] == "ui" and f["note"] == "too salesy" for f in body["feedback"])
    assert body["post"]["variants"][0]["status"] == "needs_rework"
    assert len(cl.get("feedback")[1]["feedback"]) == 2
    for bad in ({"kinds": []}, {"kinds": ["human_edit"]}, {"kinds": ["bogus"]}, {}, {"kinds": "drop"}):
        assert cl.post(f"posts/{pid}/feedback", bad)[0] == 400, bad
    assert cl.post("posts/p_nope/feedback", {"kinds": ["note"]})[0] == 404


def test_drop_feedback_rejects_the_queue_item_and_drops_the_variant(built, cl):
    pid = built.posts["scheduled"]
    v = built.studio.post(pid)["variants"][0]
    item = state.load_queue_item(built.sp, v)
    st, body = cl.post(f"posts/{pid}/feedback", {"variant": v["id"], "kinds": ["drop"], "note": "duplicate", "by": "Maren"})
    assert st == 201 and body["post"]["variants"][0]["status"] == "dropped" and body["post"]["variants"][0]["dropped"] is True
    q = pq.load(built.sp, item["id"])
    assert q["status"] == "rejected" and q["approvals"] == []


def test_pause_here_and_resume_only_in_the_app(built, cl):
    assert cl.post("pause", {})[1] == {"paused": True, "scope": "studio"} and built.sp.pause_path.exists()
    assert cl.get("summary")[1]["paused"] is True
    assert cl.post("resume", {"pin": PIN})[0] == 404 and built.sp.pause_path.exists()     # resuming is the app's native act now
    built.sp.pause_path.unlink()
    assert cl.post("pause", {"account": "ig-main"})[1] == {"paused": True, "scope": "ig-main"}
    assert (built.sp.accounts_dir / "ig-main.paused").exists()
    assert cl.get("accounts")[1]["accounts"][2]["paused"] is True
    assert cl.post("pause", {"account": "../x"})[0] == 400
    assert cl.post("pause", {"account": "nobody"})[0] == 404
    assert cl.post("resume", {"pin": PIN, "account": "ig-main"})[0] == 404 and (built.sp.accounts_dir / "ig-main.paused").exists()


def test_unknown_methods_and_routes(cl):
    assert cl.post("nope")[0] == 404
    assert cl.req("POST", "/elsewhere", {})[0] == 404
    assert cl.req("DELETE", "/api/studio/posts")[0] in (404, 501)
    assert cl.req("HEAD", "/api/studio/summary")[0] == 200


# ---- fixtures: the committed shapes are what the server answers -----------------------------------------------------------

@pytest.mark.parametrize("fx", [f for f in FIXTURES if f.stem not in ("POST_approve", "POST_edit_conflict", "POST_move_conflict", "PUT_library_upload", "POST_view", "POST_reject",
                                                                       "POST_resume", "POST_tick_rehearse")],
                         ids=lambda f: f.stem)
def test_fixture_served_verbatim(fx_cl, fx):
    d = json.loads(fx.read_text())
    path = d["path"].removeprefix("/api/studio/")
    if d.get("query"):
        path += "?" + "&".join(f"{k}={v}" for k, v in d["query"].items())
    st, body = fx_cl.api(d["method"], path, d.get("request") if d["method"] == "POST" else None)
    assert st == d["status"], (st, body)
    if fx.stem in ("GET_summary", "POST_view"):                     # server-built parts: the committed keys must all be there
        assert set(d["response"]) <= set(body)
        if fx.stem == "POST_view":
            assert body["viewed"]["sha"] == d["response"]["viewed"]["sha"]
    else:
        assert body == d["response"]


def test_fixture_mode_files_are_placeholders_not_errors(fx_cl):
    st, h, body = fx_cl.req("GET", "/files/library/assets/9f/x.jpg")          # fixtures name files nobody has: a stand-in picture, never a 404
    assert st == 200 and h["Content-Type"] == "image/jpeg" and body[:3] == b"\xff\xd8\xff"
    assert fx_cl.req("GET", "/files/library/assets/9f/x.jpg")[2] == body        # deterministic
    assert fx_cl.req("GET", "/files/exports/none.png")[0] == 404 and fx_cl.req("GET", "/files/inbox/x.jpg")[0] == 404
    assert fx_cl.req("GET", "/app/")[0] == 200 and fx_cl.req("GET", "/tools/")[0] == 200


@pytest.mark.parametrize("extra", [{"unknown":"no"}, {"by":{}}, {"by":42}, {"by":" "}, {"by":"x"*81}, {"approved":True}])
def test_create_refuses_invalid_editorial_metadata_and_unknown_fields(built, cl, extra):
    before = cl.get("changes")[1]["cursor"]
    st, body = cl.post("posts", {"copy":{"base":"bounded creation"}, **extra})
    assert st == 400, body
    assert cl.get("changes?cursor="+before)[1]["events"] == []
