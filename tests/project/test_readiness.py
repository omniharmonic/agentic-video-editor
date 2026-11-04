"""Readiness: a version whose files have not all arrived (or arrived wrong) is never opened.

Tomas's computer writes the log; her log syncs here ahead of the files. Each test builds that by appending to a second log file
in the same project (her device), then making her files appear (or not) on this computer.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
from pathlib import Path

import sqlite3

import pytest

from ave import editdoc
from ave.project import Project, readiness as R
from ave.project.log import Log

SRC = {"A": {"file": "raw/camA.mov", "kind": "video", "start_offset": 0.0}}


def doc(stage: str = "rough") -> dict:
    d = editdoc.new_doc(stage, 30, (1920, 1080), copy.deepcopy(SRC), 10.0)
    d["tracks"]["video"] = [{"id": "v1", "src": "A", "o0": 0, "o1": 10, "e0": 0}]
    d["tracks"]["dialogue"] = [{"id": "d1", "src": "A", "o0": 0, "o1": 10, "e0": 0}]
    return d


def tomas(project: Project) -> Log:
    return Log(project.log_dir, "dev_TOMAS0000000000000000000000", "tomas")


def need(rel: str, data: bytes) -> dict:
    return {"path": rel, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def tomas_made(project: Project, version: str, files: dict[str, bytes], footage: dict[str, int] | None = None) -> dict:
    """Tomas logs `version` with what it needs (her edit and caption files hashed, footage by size only). Nothing lands here."""
    needs = [need(rel, data) for rel, data in files.items()]
    needs += [{"path": rel, "bytes": n, "mtime": 1.0, "sha256": None, "hash": "pending"} for rel, n in (footage or {}).items()]
    edit = next(iter(files))
    return tomas(project).append("version.added", artifact="art_ep", data={"version": version, "file": edit}, needs=needs)


def put(root: Path, rel: str, data: bytes) -> Path:
    """A file lands as Syncthing lands it: written to a temp name, then renamed into place."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".put.{p.name}.part")
    tmp.write_bytes(data)
    os.replace(tmp, p)
    return p


@pytest.fixture(autouse=True)
def fresh_caches():
    R.reset_caches()
    yield
    R.drain(10)
    R.reset_caches()


@pytest.fixture
def ep(project):
    """The project as an episode folder (flat layout), with art_ep on the board."""
    (project.root / "episode.json").write_text(json.dumps({"title": "The Long Table"}))
    project.log.append("artifact.created", "art_ep", {"kind": "episode", "title": "The Long Table"})
    return project


def settled(project: Project) -> dict:
    """readiness() as the app reads it (never hashing inline), after the background hasher has finished what it was given."""
    R.readiness(project.state(), project.root)
    assert R.drain(10)
    return R.readiness(project.state(), project.root)


def test_log_ahead_of_files_is_arriving(ep):
    edit = json.dumps(doc()).encode()
    srt = b"1\n00:00:00,000 --> 00:00:01,000\nhello\n"
    tomas_made(ep, "rough.v1~jos0", {"edits/rough.v1~jos0.json": edit, "captions/rough.srt": srt}, footage={"raw/camA.mov": 4096})
    st = ep.state()

    got = R.readiness(st, ep.root)                        # the log is here; not one of its files is
    assert got["rough.v1~jos0"] == "arriving" and got["art_ep"] == "arriving"
    d = R.details(st, ep.root)["rough.v1~jos0"]
    assert d["reason"] == "missing" and d["from"] == "tomas"
    assert R.say(d) == ("This version is still arriving from Tomas: its files have not all reached this computer yet. "
                        "Wait until it has arrived.")

    # Syncthing is writing the edit file: a temp file beside where it will land, and the half-written file not yet renamed
    put(ep.root, "edits/.syncthing.rough.v1~jos0.json.tmp", edit[:100])
    put(ep.root, "captions/rough.srt", srt)
    put(ep.root, "raw/camA.mov", b"\0" * 4096)
    assert settled(ep)["rough.v1~jos0"] == "arriving"
    assert R.details(ep.state(), ep.root)["rough.v1~jos0"]["reason"] == "copying"

    # renamed into place, but a size that is not the recorded one yet: still arriving, never damaged
    (ep.root / "edits/.syncthing.rough.v1~jos0.json.tmp").unlink()
    put(ep.root, "edits/rough.v1~jos0.json", edit[:-5])
    assert settled(ep)["rough.v1~jos0"] == "arriving"

    # footage short of its recorded size is arriving too
    put(ep.root, "edits/rough.v1~jos0.json", edit)
    put(ep.root, "raw/camA.mov", b"\0" * 1000)
    assert settled(ep)["rough.v1~jos0"] == "arriving"

    put(ep.root, "raw/camA.mov", b"\0" * 4096)
    done = settled(ep)
    assert done["rough.v1~jos0"] == "ready" and done["art_ep"] == "ready"


def test_hash_unknown_reads_arriving_and_is_never_worked_out_on_the_callers_thread(ep, monkeypatch):
    edit = json.dumps(doc()).encode()
    tomas_made(ep, "rough.v1~jos0", {"edits/rough.v1~jos0.json": edit})
    put(ep.root, "edits/rough.v1~jos0.json", edit)
    gate, threads = threading.Event(), []
    real = R._sha256

    def slow(p):
        threads.append(threading.current_thread().name)
        gate.wait(10)
        return real(p)
    monkeypatch.setattr(R, "_sha256", slow)
    t0 = time.monotonic()
    got = R.details(ep.state(), ep.root)["rough.v1~jos0"]
    assert time.monotonic() - t0 < 1.0                     # answered while the hash is still being worked out
    assert (got["status"], got["reason"]) == ("arriving", "checking")
    assert "being checked" in R.say(got)
    gate.set()
    assert R.drain(10)
    assert threads == ["readiness-hash"]
    assert R.readiness(ep.state(), ep.root)["rough.v1~jos0"] == "ready"


def test_wrong_hash_is_damaged(ep):
    edit = json.dumps(doc()).encode()
    tomas_made(ep, "rough.v1~jos0", {"edits/rough.v1~jos0.json": edit})
    wrong = edit.replace(b'"rough"', b'"rouge"')
    assert len(wrong) == len(edit) and wrong != edit
    put(ep.root, "edits/rough.v1~jos0.json", wrong)
    got = settled(ep)
    assert got["rough.v1~jos0"] == "damaged" and got["art_ep"] == "damaged"
    d = R.details(ep.state(), ep.root)["rough.v1~jos0"]
    assert d["reason"] == "hash"
    assert R.say(d) == "This version arrived damaged: rough.v1~jos0.json does not match what Tomas saved. Ask Tomas to resend it."
    put(ep.root, "edits/rough.v1~jos0.json", edit)           # synced again
    assert settled(ep)["rough.v1~jos0"] == "ready"


def test_hash_is_cached_until_the_file_changes(ep, monkeypatch):
    edit = json.dumps(doc()).encode()
    tomas_made(ep, "rough.v1~jos0", {"edits/rough.v1~jos0.json": edit})
    p = put(ep.root, "edits/rough.v1~jos0.json", edit)
    calls = []
    real = R._sha256
    monkeypatch.setattr(R, "_sha256", lambda path: calls.append(path) or real(path))

    assert settled(ep)["rough.v1~jos0"] == "ready"
    for _ in range(3):
        assert settled(ep)["rough.v1~jos0"] == "ready"
    assert R.readiness(ep.state(), ep.root, wait=True)["rough.v1~jos0"] == "ready"
    assert len(calls) == 1                                  # hashed once, then remembered

    R.reset_caches()                                        # a new run of the app: only index.sqlite remembers
    ep.index().refresh()                                    # the index is rebuilt from the logs: the hashes survive it
    assert settled(ep)["rough.v1~jos0"] == "ready" and len(calls) == 1

    wrong = edit.replace(b'"rough"', b'"rouge"')            # same size, new content and mtime: hashed again
    put(ep.root, "edits/rough.v1~jos0.json", wrong)
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    R.invalidate(ep.root, ["edits/rough.v1~jos0.json"])     # as Syncthing's ItemFinished event does
    assert settled(ep)["rough.v1~jos0"] == "damaged"
    assert len(calls) == 2

    R.reset_caches()
    (ep.root / ".ave/local/index.sqlite").unlink()         # the cache is disposable: deleting it costs one more hash
    assert settled(ep)["rough.v1~jos0"] == "damaged" and len(calls) == 3


def test_an_old_event_without_needs_is_ready_once_its_file_exists(ep):
    tomas(ep).append("version.added", artifact="art_ep", data={"version": "rough.v1", "file": "edits/rough.v1.json"})
    assert R.readiness(ep.state(), ep.root)["rough.v1"] == "arriving"
    put(ep.root, "edits/rough.v1.json", json.dumps(doc()).encode())
    assert R.readiness(ep.state(), ep.root)["rough.v1"] == "ready"


def on_proxies(project: Project) -> None:
    meta = json.loads(project.project_json.read_text())
    meta["members"][0]["media"] = "proxies"                  # this computer gets proxies only: camera originals stay away
    project.project_json.write_text(json.dumps(meta))


def test_on_proxies_the_proxy_stands_in_for_the_original(ep):
    """Camera originals never come to a member on proxies, so they never hold a version back; the proxy it plays from does."""
    on_proxies(ep)
    edit = json.dumps(doc()).encode()
    tomas_made(ep, "rough.v1~jos0", {"edits/rough.v1~jos0.json": edit}, footage={"raw/camA.mov": 4096})
    put(ep.root, "edits/rough.v1~jos0.json", edit)
    settled(ep)
    d = R.details(ep.state(), ep.root)["rough.v1~jos0"]
    assert (d["status"], d["path"]) == ("arriving", "raw/proxies/camA.mov")   # no proxy recorded: the derived path must exist
    put(ep.root, "raw/proxies/camA.mov", b"\1" * 512)
    assert settled(ep)["rough.v1~jos0"] == "ready"


def test_a_recorded_proxy_is_checked_by_size_and_only_on_proxies(ep):
    edit = json.dumps(doc()).encode()
    needs = [need("edits/rough.v1~jos0.json", edit), {"path": "raw/camA.mov", "bytes": 4096, "sha256": None, "hash": "pending"},
             {"path": "raw/proxies/camA.mov", "bytes": 512, "sha256": None, "hash": "pending", "proxy_of": "raw/camA.mov"}]
    tomas(ep).append("version.added", artifact="art_ep", data={"version": "rough.v1~jos0", "file": "edits/rough.v1~jos0.json"},
                     needs=needs)
    put(ep.root, "edits/rough.v1~jos0.json", edit)
    put(ep.root, "raw/camA.mov", b"\0" * 4096)
    assert settled(ep)["rough.v1~jos0"] == "ready"           # full media: the original is what plays, the proxy is not needed
    on_proxies(ep)
    (ep.root / "raw/camA.mov").unlink()
    assert settled(ep)["rough.v1~jos0"] == "arriving"        # the proxy it plays from is not here
    put(ep.root, "raw/proxies/camA.mov", b"\1" * 100)
    assert settled(ep)["rough.v1~jos0"] == "arriving"        # short of its recorded size
    put(ep.root, "raw/proxies/camA.mov", b"\1" * 512)
    assert settled(ep)["rough.v1~jos0"] == "ready"


def test_versions_saved_here_record_their_proxies(ep):
    from ave.episode import Episode
    put(ep.root, "raw/camA.mov", b"\0" * 4096)
    put(ep.root, "raw/proxies/camA.mov", b"\1" * 512)
    path = editdoc.save_new_version(Episode(ep.root), doc())
    v = R.find_version(ep.state(), path.name[:-len(".json")])[1]
    px = next(n for n in v["needs"] if n.get("proxy_of"))
    assert (px["path"], px["bytes"], px["proxy_of"]) == ("raw/proxies/camA.mov", 512, "raw/camA.mov")


def test_the_artifact_reads_its_kept_version(ep):
    a, b = json.dumps(doc()).encode(), json.dumps(doc("final")).encode()
    tomas_made(ep, "rough.v1~jos0", {"edits/rough.v1~jos0.json": a})
    put(ep.root, "edits/rough.v1~jos0.json", a)
    tomas_made(ep, "final.v1~jos0", {"edits/final.v1~jos0.json": b})
    assert settled(ep)["art_ep"] == "arriving"              # the newest has not arrived
    ep.log.append("version.kept", "art_ep", {"version": "rough.v1~jos0"})
    assert settled(ep)["art_ep"] == "ready"                 # the kept one has


def test_versions_saved_here_record_what_they_need(ep):
    from ave.episode import Episode
    e = Episode(ep.root)
    path = editdoc.save_new_version(e, doc())
    v = R.find_version(ep.state(), path.name[:-len(".json")])[1]
    rel = path.relative_to(ep.root).as_posix()
    n = next(n for n in v["needs"] if n["path"] == rel)
    assert n["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest() and n["bytes"] == path.stat().st_size
    assert R.readiness(ep.state(), ep.root, wait=True)[v["version"]] == "ready"


# ---------------------------------------------------------------------------------------------------- one choke point
@pytest.fixture
def v1_here_v2_arriving(ep):
    """Maren saved rough v1 here; Tomas's rough v2 is in the log, its edit file still on the way."""
    from ave.episode import Episode
    e = Episode(ep.root)
    v1 = editdoc.save_new_version(e, doc())
    edit2 = json.dumps(doc()).encode()
    tomas_made(ep, "rough.v2~jos0", {"edits/rough.v2~jos0.json": edit2})
    return e, v1, edit2


def _args(**kw):
    import argparse
    base = dict(stage="rough", version=None, name=None, preset="proxy", proxy=True, out=None, overwrite=False, no_captions=True,
                label=None, no_master=True, project=None, layers=False, use_proxies=False, try_subtitles=False, dry_run=True)
    return argparse.Namespace(**{**base, **kw})


def test_render_refuses_when_the_newest_version_is_arriving(v1_here_v2_arriving, monkeypatch):
    from ave.commands import render
    e, v1, _ = v1_here_v2_arriving
    with pytest.raises(R.NotReady) as got:
        render.cmd_render(_args(episode=str(e.root)))
    assert str(got.value) == ("Rough cut, version 2 is still arriving from Tomas: its files have not all reached this computer "
                              "yet. Wait until it has arrived. To use the version before on purpose, name it: --version 1.")
    seen = []                                                # named on purpose, a ready version goes ahead
    monkeypatch.setattr("ave.compile.render", lambda ep, doc, out, **kw: seen.append(doc) or {"out": str(out)})
    assert render.cmd_render(_args(episode=str(e.root), version=1)) == 0 and len(seen) == 1


def test_handoff_refuses_when_the_newest_version_is_arriving(v1_here_v2_arriving, monkeypatch):
    from ave.commands import handoff
    e, v1, _ = v1_here_v2_arriving
    with pytest.raises(R.NotReady, match="Rough cut, version 2 is still arriving from Tomas.*--version 1"):
        handoff.cmd(_args(episode=str(e.root), stage=None))
    picked = handoff._pick(e, "rough", None, "1")            # `ave handoff --version 1`
    assert picked[3] == v1


def test_a_damaged_edit_is_refused_before_it_is_parsed(v1_here_v2_arriving, monkeypatch):
    from ave.commands import render
    e, v1, edit2 = v1_here_v2_arriving
    put(e.root, "edits/rough.v2~jos0.json", b"{" + b" " * (len(edit2) - 1))     # right size, not even JSON
    monkeypatch.setattr(editdoc, "read_json", lambda p: pytest.fail("parsed a damaged edit"))
    with pytest.raises(R.NotReady, match="arrived damaged: rough.v2~jos0.json does not match what Tomas saved"):
        render.cmd_render(_args(episode=str(e.root)))
    with pytest.raises(R.NotReady, match="arrived damaged"):
        editdoc.load(e.root / "edits/rough.v2~jos0.json")     # named on purpose: still never parsed


def test_final_refuses_through_the_same_choke_point(v1_here_v2_arriving):
    """`ave final` clones the latest rough: it must not build a final from v1 while v2 is on its way."""
    from ave import final
    e, v1, _ = v1_here_v2_arriving
    with pytest.raises(R.NotReady, match="Rough cut, version 2 is still arriving from Tomas"):
        editdoc.load_latest(e, "rough")
    src = Path(final.__file__).read_text()
    assert "load_latest(" in src or "latest_path(" in src     # final reads its base through the choke point


def test_once_the_newest_arrives_the_latest_is_it(v1_here_v2_arriving):
    e, v1, edit2 = v1_here_v2_arriving
    put(e.root, "edits/rough.v2~jos0.json", edit2)
    assert editdoc.latest_path(e, "rough").name == "rough.v2~jos0.json"


# ---------------------------------------------------------------------------------------------------- cost and dead ends
def test_a_hash_that_cannot_be_saved_settles_and_backs_off(ep, monkeypatch):
    edit = json.dumps(doc()).encode()
    tomas_made(ep, "rough.v1~jos0", {"edits/rough.v1~jos0.json": edit})
    put(ep.root, "edits/rough.v1~jos0.json", edit)

    def fail(*a, **k):
        raise sqlite3.OperationalError("attempt to write a readonly database")
    monkeypatch.setattr(R.HashCache, "put", fail)
    heard = []
    off = R.on_hashed(lambda root: heard.append(root))
    hashed0, saves0 = R.HASHER.hashed, R.HASHER.save_attempts
    try:
        for _ in range(20):
            got = settled(ep)
        assert got["rough.v1~jos0"] == "ready"               # remembered in memory, so it settles
        assert R.HASHER.hashed - hashed0 == 1                # hashed once, never again
        assert len(heard) == 1                               # listeners hear of a new hash once: no hash, kick, look loop
        assert R.HASHER.save_attempts - saves0 <= 2          # and saving backs off instead of retrying every look
        assert R.HASHER._retry_at > time.monotonic()
    finally:
        off()


def test_repeated_looks_cost_a_stat_per_folder(ep, monkeypatch):
    for n in range(1, 31):                                   # 30 versions, each an edit and a caption file
        e = json.dumps({**doc(), "n": n}).encode()
        srt = f"1\n00:00:00,000 --> 00:00:01,000\nline {n}\n".encode()
        tomas_made(ep, f"rough.v{n}~jos0", {f"edits/rough.v{n}~jos0.json": e, f"captions/rough.v{n}.srt": srt})
        put(ep.root, f"edits/rough.v{n}~jos0.json", e)
        put(ep.root, f"captions/rough.v{n}.srt", srt)
    st = ep.state()
    assert all(v == "ready" for v in settled(ep).values())
    calls, real = [], R._stat
    monkeypatch.setattr(R, "_stat", lambda p: calls.append(str(p)) or real(p))
    hashed0 = R.HASHER.hashed
    for _ in range(10):
        assert all(v == "ready" for v in R.readiness(st, ep.root).values())
    files = [c for c in calls if not c.endswith(("edits", "captions"))]
    assert len(calls) - len(files) <= 2 * 10                 # each folder once per look
    assert len(files) <= 10 * 3                              # project.json, episode.json, studio.json: no file is stat'ed again
    assert R.HASHER.hashed == hashed0


def test_arriving_with_no_change_for_ten_minutes_is_stuck(ep, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(R, "_now", lambda: clock[0])
    edit = json.dumps(doc()).encode()
    tomas_made(ep, "rough.v1~jos0", {"edits/rough.v1~jos0.json": edit})
    assert R.readiness(ep.state(), ep.root)["rough.v1~jos0"] == "arriving"
    clock[0] += 599
    assert R.readiness(ep.state(), ep.root)["rough.v1~jos0"] == "arriving"
    clock[0] += 2
    d = R.details(ep.state(), ep.root)["rough.v1~jos0"]
    assert (d["status"], d["reason"]) == ("stuck", "sync")
    assert R.say(d) == ("This version is stuck: nothing more has arrived from Tomas for ten minutes. Check sync status: Tomas's "
                        "computer may be off or not syncing.")

    # a file of the wrong size with nothing copying it: ask Tomas to resend
    put(ep.root, "edits/rough.v1~jos0.json", edit[:-10])
    R.invalidate(ep.root)
    assert R.readiness(ep.state(), ep.root)["rough.v1~jos0"] == "arriving"   # it changed: the ten minutes start again
    clock[0] += 601
    d = R.details(ep.state(), ep.root)["rough.v1~jos0"]
    assert (d["status"], d["reason"]) == ("stuck", "resend")
    assert "Ask Tomas to resend it." in R.say(d)

    # a Syncthing temp file that has not grown for ten minutes is stuck; one still growing is arriving
    (ep.root / "edits/rough.v1~jos0.json").unlink()
    tmp = put(ep.root, "edits/.syncthing.rough.v1~jos0.json.tmp", edit[:50])
    R.invalidate(ep.root)
    os.utime(tmp, (clock[0] - 30, clock[0] - 30))
    R.invalidate(ep.root)
    assert R.details(ep.state(), ep.root)["rough.v1~jos0"]["reason"] == "copying"
    os.utime(tmp, (clock[0] - 700, clock[0] - 700))
    R.invalidate(ep.root)
    assert R.details(ep.state(), ep.root)["rough.v1~jos0"]["status"] == "stuck"

    # a stale temp file beside a whole, verified file does not hold it back
    put(ep.root, "edits/rough.v1~jos0.json", edit)
    R.invalidate(ep.root)
    assert settled(ep)["rough.v1~jos0"] == "ready"


# ---------------------------------------------------------------------------------------------------- the app and Claude
@pytest.fixture
def studio(tmp_path):
    """A studio holding one episode folder, "ep", that is its own project (this computer is Maren's)."""
    from ave.studio.project import StudioProject
    sp = StudioProject(tmp_path / "studio")
    sp.create(title="Room", tz="UTC")
    root = sp.root / "ep"
    root.mkdir()
    (root / "episode.json").write_text(json.dumps({"title": "The Long Table"}))
    project = Project.create(root, "The Long Table", person="maren")
    project.log.append("artifact.created", "art_ep", {"kind": "episode", "title": "The Long Table"})
    return sp, project


def test_claude_cannot_put_a_version_on_stage_before_it_arrives(studio):
    from ave.agent import AgentApp, NotReady, Session
    from ave.studio.service import open_studio
    sp, project = studio
    edit = json.dumps(doc()).encode()
    tomas_made(project, "rough.v1~jos0", {"edits/rough.v1~jos0.json": edit})
    app = AgentApp(open_studio(sp.root), by="claude-code")
    for tool, args in (("room_show", {"artifact": "ep", "view": "cut", "version": "rough.v1~jos0"}),
                       ("room_offer_version", {"artifact": "ep", "version": "rough.v1~jos0", "summary": "x"})):
        with pytest.raises(NotReady, match="still arriving from Tomas"):
            app.call(tool, args)
    s = Session(app)
    s.initialized = s.ready = True
    got = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "room_show", "arguments": {"artifact": "ep", "view": "cut", "version": "rough.v1~jos0"}}})
    assert got["result"]["isError"] and got["result"]["structuredContent"]["code"] == "not_ready"
    assert not (sp.root / ".ave/local/room-queue.jsonl").exists() or "rough.v1~jos0" not in (sp.root / ".ave/local/room-queue.jsonl").read_text()

    put(project.root, "edits/rough.v1~jos0.json", edit)
    assert app.call("room_show", {"artifact": "ep", "view": "cut", "version": "rough.v1~jos0"})["room"] == "closed"


@pytest.fixture(scope="module")
def browser():
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"Chromium unavailable: {e}")
        yield b
        b.close()


@pytest.fixture
def room_server(studio):
    from ave.studio.service import open_studio
    from ave.studio.ui.server import StudioServer
    sp, project = studio
    srv = StudioServer(open_studio(sp.root), pin="424242")
    srv.ep = project
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    t.join(5)
    srv.server_close()


@pytest.fixture
def page(browser, room_server):
    pg = browser.new_page(viewport={"width": 1440, "height": 900})
    pg.add_init_script("window.__AVE_TEST__ = true;")
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(room_server.app_url() + "#/room/ep")
    pg.wait_for_selector(".room")
    pg.wait_for_function("() => window.__room && window.__room.state.live === 'live'")
    yield pg
    pg.close()
    assert not errors


def test_arriving_version_cannot_be_opened(page, room_server):
    import urllib.error
    import urllib.request
    project = room_server.ep
    edit = json.dumps(doc()).encode()
    tomas_made(project, "rough.v1~jos0", {"edits/rough.v1~jos0.json": edit})
    room_server.room.show(artifact="ep", view="cut", version="rough.v1~jos0")

    page.get_by_role("heading", name="Arriving from Tomas…").wait_for()
    assert page.get_by_text("Its files are still copying to this computer.", exact=False).is_visible()
    assert page.locator(".room-arrive").inner_text() == "Arriving"
    assert page.locator(".room-main .cut-play").first.is_disabled()      # nothing to play
    assert page.get_by_role("button", name="Render the full cut").count() == 0
    assert page.evaluate("!document.querySelector('video').getAttribute('src')")
    with pytest.raises(LookupError, match="still arriving from Tomas"):
        room_server.render_full("ep", "rough.v1~jos0")
    req = urllib.request.Request(f"http://127.0.0.1:{room_server.port}/api/room/cut?artifact=ep&version=rough.v1~jos0",
                                 headers={"X-AVE-Token": room_server.token})
    with urllib.request.urlopen(req) as r:
        got = json.loads(r.read())
    assert got["readiness"]["status"] == "arriving" and "segments" not in got     # the server never hands out its contents

    # the files arrive: no reload, the Room opens it by itself
    put(project.root, "edits/rough.v1~jos0.json", edit)
    page.get_by_text("Rough cut, version 1 has arrived").wait_for(timeout=15_000)
    page.get_by_role("heading", name="No render of this version yet").wait_for()
    assert page.locator(".room-arrive").is_hidden()
    assert page.locator(".tl-seg").count() >= 1 or page.locator(".room-strip").count() >= 1

    # a version that arrives damaged says so, and what to do
    wrong = edit.replace(b'"rough"', b'"rouge"')
    tomas_made(project, "rough.v2~jos0", {"edits/rough.v2~jos0.json": edit})
    put(project.root, "edits/rough.v2~jos0.json", wrong)
    room_server.offer("rough.v2~jos0", artifact="ep", summary="Opens on the question")
    page.get_by_role("heading", name="This version arrived damaged").wait_for(timeout=15_000)
    assert page.get_by_text("Ask Tomas to resend it.", exact=False).first.is_visible()
    assert page.locator(".room-arrive").inner_text() == "Damaged"
    assert page.get_by_role("button", name="Keep this").is_disabled()
    assert page.locator(".room-main .cut-transport button", has_text="Flag this moment").is_disabled()
    req = urllib.request.Request(f"http://127.0.0.1:{room_server.port}/api/room/keep", method="POST",
                                 data=json.dumps({"version": "rough.v2~jos0"}).encode(),
                                 headers={"X-AVE-Token": room_server.token, "Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(req)
    assert refused.value.code == 409
    body = json.loads(refused.value.read())
    assert body["code"] == "not_ready" and "arrived damaged" in body["error"]
    assert not [c for c in room_server.room.read()["clicks"] if c["action"] == "keep"]

    # the board marks the row whose current version cannot be opened, live
    page.goto(room_server.app_url() + "#/workspace")
    mark = page.locator(".c-version .ready-mark")
    mark.wait_for()
    assert mark.get_attribute("aria-label") == "Damaged on the way from Tomas: ask Tomas to resend it"
    put(project.root, "edits/rough.v2~jos0.json", edit)
    mark.wait_for(state="detached", timeout=15_000)

    # the mark follows the version the cell shows: v2 kept, Tomas's v3 still copying, the cell says version 3 and is marked
    project.log.append("version.kept", "art_ep", {"version": "rough.v2~jos0"})
    tomas_made(project, "rough.v3~jos0", {"edits/rough.v3~jos0.json": edit})
    page.locator(".c-version", has_text="Rough cut, version 3").locator(".ready-mark").wait_for(timeout=15_000)
    assert page.locator(".c-version .ready-mark").get_attribute("aria-label") == "Arriving from Tomas…"


# ---------------------------------------------------------------------------------------------------- fix round 2
@pytest.fixture
def rough_here_final_logged(ep):
    """Maren has rough v1 here; Tomas's final v1 is in the log and not one byte of it has arrived."""
    from ave.episode import Episode
    e = Episode(ep.root)
    editdoc.save_new_version(e, doc())
    tomas_made(ep, "final.v1~jos0", {"edits/final.v1~jos0.json": json.dumps(doc("final")).encode()})
    return e


ARRIVING_FINAL = "Final cut, version 1 is still arriving from Tomas"


def test_render_never_skips_an_arriving_final_for_the_rough(rough_here_final_logged, monkeypatch):
    from ave.commands import render
    e = rough_here_final_logged
    monkeypatch.setattr("ave.compile.render", lambda *a, **k: pytest.fail("rendered something"))
    with pytest.raises(R.NotReady, match=ARRIVING_FINAL):
        render.cmd_render(_args(episode=str(e.root), stage="auto"))
    with pytest.raises(R.NotReady, match=ARRIVING_FINAL) as got:    # asked for the final by name: not "No final edit yet"
        render.cmd_render(_args(episode=str(e.root), stage="final"))
    assert "No final" not in str(got.value)
    assert str(got.value).endswith("if it does not arrive, check sync status.")    # no version of it is here to name instead


def test_handoff_never_skips_an_arriving_final_for_the_rough(rough_here_final_logged):
    from ave.commands import handoff
    e = rough_here_final_logged
    with pytest.raises(R.NotReady, match=ARRIVING_FINAL):
        handoff.cmd(_args(episode=str(e.root), stage=None))
    with pytest.raises(R.NotReady, match=ARRIVING_FINAL):
        handoff.cmd(_args(episode=str(e.root), stage="final"))
    assert handoff._pick(e, "rough", None)[0] == "rough"            # the rough, chosen on purpose, goes ahead


def test_sequences_never_skip_an_arriving_final(rough_here_final_logged):
    from ave import sequence
    with pytest.raises(R.NotReady, match=ARRIVING_FINAL):
        sequence.base_doc(rough_here_final_logged)


def test_the_next_step_is_one_the_command_has(v1_here_v2_arriving):
    e, v1, _ = v1_here_v2_arriving
    with pytest.raises(R.NotReady) as got:                           # `ave final`, `ave shorts`...: no version option
        editdoc.load_latest(e, "rough")
    assert "--version" not in str(got.value) and str(got.value).endswith("if it does not arrive, check sync status.")
    with R.choosing("--version"), pytest.raises(R.NotReady, match="name it: --version 1."):
        editdoc.load_latest(e, "rough")


def test_a_folder_adopted_after_a_load_is_guarded(tmp_path):
    root = tmp_path / "later"
    put(root, "edits/rough.v1~jos0.json", json.dumps(doc()).encode())
    editdoc.load(root / "edits/rough.v1~jos0.json")                 # not a project yet: read as before
    pr = Project.create(root, "Later", person="maren")
    tomas_made(pr, "rough.v1~jos0", {"edits/rough.v1~jos0.json": b"x" * len(json.dumps(doc()).encode())})
    with pytest.raises(R.NotReady, match="arrived damaged"):
        editdoc.load(root / "edits/rough.v1~jos0.json")


def test_a_failed_save_never_holds_the_index_locked(ep):
    db = R.index_path(ep.root)
    cache = R.HashCache(ep.root)
    cache.get("x", 1, 1)                                             # opens this thread's connection
    other = sqlite3.connect(db)
    other.execute("CREATE TRIGGER no_more BEFORE INSERT ON hashes BEGIN SELECT RAISE(ABORT, 'full'); END")
    other.commit()
    with pytest.raises(sqlite3.Error):
        cache.put("edits/a.json", 1, 1, "f" * 64)                    # the DELETE ran, then the INSERT failed
    assert not R._connection(db).in_transaction
    sqlite3.connect(db, timeout=0).execute("CREATE TABLE still_writable (a)")    # nobody holds the write lock
    assert cache.get("edits/a.json", 1, 1) == "f" * 64              # and it is remembered all the same


def test_backoff_never_overflows(monkeypatch):
    h = R.Hasher()
    h._fails = 100_000
    h._unsaved[("/nowhere", "a", 1, 1)] = "0" * 64
    monkeypatch.setattr(R.HashCache, "put", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    h._save()
    assert h._retry_at - time.monotonic() <= R.BACKOFF_MAX_S


def test_a_version_deleted_here_is_stuck_and_an_explicit_choice_goes_through(ep, monkeypatch):
    from ave.commands import render
    from ave.episode import Episode
    e = Episode(ep.root)
    editdoc.save_new_version(e, doc())
    v2 = editdoc.save_new_version(e, doc())
    v2.unlink()                                                      # Maren deleted his own v2 on purpose
    d = R.details(ep.state(), ep.root)[v2.name[:-len(".json")]]
    assert (d["status"], d["reason"]) == ("stuck", "removed")
    assert "was deleted on this computer, so it will not arrive by itself" in R.say(d)
    monkeypatch.setattr("ave.compile.render", lambda ep_, doc_, out, **k: {"out": str(out)})
    with pytest.raises(R.NotReady, match="deleted on this computer"):
        render.cmd_render(_args(episode=str(e.root), stage="auto"))  # "the latest" still never means the one before
    assert render.cmd_render(_args(episode=str(e.root), stage="rough")) == 0     # the stage chosen on purpose: v1


def test_a_file_in_the_sync_trash_reads_removed(ep):
    tomas_made(ep, "rough.v1~jos0", {"edits/rough.v1~jos0.json": json.dumps(doc()).encode()})
    put(ep.root, ".stversions/edits/rough.v1~jos0.json", b"{}")
    d = R.details(ep.state(), ep.root)["rough.v1~jos0"]
    assert (d["status"], d["reason"]) == ("stuck", "removed")


def test_caches_are_bounded():
    lru = R._LRU(3)
    for i in range(10):
        lru[i] = i
    lru.get(8)
    lru[10] = 10
    assert list(lru) == [8, 10] or list(lru) == [9, 8, 10]
    assert len(lru) == 3
    assert isinstance(R._MEM, R._LRU) and isinstance(R._SEEN, R._LRU) and isinstance(R._DIRS, R._LRU)
