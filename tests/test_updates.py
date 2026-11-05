"""Automatic updates (ave.updates, desktop_updates, publish_update.py): no network, no Keychain, no signing, no Sparkle run."""
import json
import logging
import plistlib
import subprocess
import sys
import types
from pathlib import Path

import pytest

from ave import updates as U

ROOT = Path(__file__).resolve().parents[1]
SECRET = "https://updates.example.test/k7Qz9-unguessable-0f3c/appcast.xml"


def _item(build, short, *, minimum="14.0", sig="c2lnbmF0dXJl", url=None):
    url = url or SECRET.rsplit("/", 1)[0] + f"/AVE-Studio-{short}-arm64.dmg"
    return f'''<item><title>AVE Studio {short}</title><sparkle:version>{build}</sparkle:version>
      <sparkle:shortVersionString>{short}</sparkle:shortVersionString>
      <sparkle:minimumSystemVersion>{minimum}</sparkle:minimumSystemVersion>
      <enclosure url="{url}" length="1234" type="application/octet-stream" sparkle:edSignature="{sig}"/></item>'''


@pytest.fixture
def feed():
    items = "".join([_item("20261103", "0.1.0"), _item("20261110", "0.2.0"), _item("20261201", "0.3.0", minimum="99.0"),
                     '<item><sparkle:version>20270101</sparkle:version><enclosure url="x" length="1"/></item>'])
    return f'<?xml version="1.0"?><rss xmlns:sparkle="{U.SPARKLE_NS}"><channel>{items}</channel></rss>'.encode()


def test_newer_version_is_offered(feed):
    got = U.check(SECRET, current="20261103", system="15.1", fetch=lambda url: feed)
    assert got.version == "20261110" and got.short_version == "0.2.0" and got.length == 1234
    assert U.check(SECRET, current="20261110", system="15.1", fetch=lambda url: feed) is None       # up to date
    assert U.check(SECRET, current="20261103", system="99.1", fetch=lambda url: feed).version == "20261201"
    assert U.check(SECRET, current="20261103", system="13.6", fetch=lambda url: feed) is None       # needs a newer macOS
    assert "unguessable" not in repr(got)                                                           # the download url is hidden


@pytest.mark.parametrize("a,b,want", [("1.10", "1.9", 1), ("1.0.1", "1.0", 1), ("1.0", "1.0.0", -1), ("1.0b1", "1.0", -1),
                                      ("1.0", "1.0b2", 1), ("1.0b2", "1.0b10", -1), ("2", "10", -1), ("1.0", "1.0", 0),
                                      ("20261110", "20261103", 1), ("0.2.0", "0.10.0", -1)])
def test_versions_compare_the_way_sparkle_does(a, b, want):
    assert U.compare_versions(a, b) == want and U.compare_versions(b, a) == -want


@pytest.fixture(autouse=True)
def no_drain_left_behind():
    """An approved relaunch raises the process-wide drain flag; never let one test's flag refuse another test's jobs."""
    U.end_drain()
    yield
    U.end_drain()


@pytest.fixture
def native(monkeypatch):
    """The real desktop session (its built-in sample studio): its StudioServer, Workbench, Jobs panel and workspace."""
    monkeypatch.setenv("AVE_APPROVAL_KEY", "0" * 64)            # restored afterwards; the sample sets its own
    from ave.desktop_native import NativeSession
    from ave.desktop_files import NativeFileWork
    session = NativeSession(fixture=True)
    ctl = types.SimpleNamespace(busy=False, file_work=NativeFileWork(lambda fn: fn()), session=session)
    yield ctl
    ctl.file_work.close()
    session.close()


def test_update_waits_while_a_render_or_sync_runs(native):
    import threading
    from ave.jobs import JobRunner
    from ave.episode import Episode
    from ave.studio.workbench import job_path, write
    server = native.session.server
    assert U.safe_to_restart(native) and U.busy_reasons(native) == []

    # The Room's own renders: the full cut, a preview, a review copy (StudioServer._background threads).
    hold = threading.Event()
    for key in (("render", "art_ep", "rough.v1"), ("preview", "art_ep", "rough.v2"), ("review", "art_ep", "k")):
        assert server._background(key, hold.wait)
    assert U.busy_reasons(native) == ["a render is running", "a review copy is being made"]
    hold.set()
    for t in list(server._jobs.values()):
        t.join(5)
    assert U.safe_to_restart(native)

    # A Media tools / episode job in the real Workbench job store.
    sp = server.app.workbench.sp
    jid = "job_" + "a" * 24
    write(sp, job_path(sp, jid), {"id": jid, "operation": "video-render", "status": "running", "created": "2026-11-03T00:00:00Z"})
    assert U.busy_reasons(native) == ["a render or export is running"]
    write(sp, job_path(sp, jid), {"id": jid, "operation": "video-render", "status": "done", "created": "2026-11-03T00:00:00Z"})
    assert U.safe_to_restart(native)

    # A Jobs panel job on a real runner.
    runner = JobRunner(Episode(sp.root))
    runner.jobs["j1"] = {"id": "j1", "cmd": "clips prepare", "status": "running"}
    server.episodes.runners["prj_x"] = runner
    assert U.busy_reasons(native) == ["a job from the Jobs panel (ave clips prepare) is running"]
    runner.jobs["j1"]["status"] = "done"

    # Sync: only what this computer still has to receive holds the restart (a peer that lags does not).
    from ave.syncthing import SyncStatus
    states = {"st": SyncStatus("syncing", need_bytes=5, need_items=1)}
    native.session._sync = types.SimpleNamespace(status=lambda p: states["st"])
    server.workspace.projects = lambda: [types.SimpleNamespace(title="Episode 12")]
    assert U.busy_reasons(native) == ["Episode 12 is still syncing"]
    states["st"] = SyncStatus("syncing")                        # only a peer still needs something from here
    assert U.safe_to_restart(native)
    native.session._sync = None

    native.busy = True                                          # the native import
    assert U.busy_reasons(native) == ["an import is running"]
    native.busy = False
    native.file_work.busy = True                                # a package export or save
    assert not U.safe_to_restart(native)
    native.file_work.busy = False


def test_relaunch_gate_holds_sparkles_block_until_safe():
    app = types.SimpleNamespace(busy=True, file_work=None, session=None)
    gate, ran = U.RelaunchGate(app), []
    assert gate.postpone(lambda: ran.append(1)) is True and gate.waiting
    assert gate.poll() is False and ran == []
    app.busy = False
    assert gate.poll() is True and ran == [1] and not gate.waiting
    assert gate.poll() is False and ran == [1]
    assert gate.postpone(lambda: ran.append(2)) is False and ran == [1]     # safe: Sparkle goes ahead itself
    app.busy = True
    assert gate.postpone(lambda: ran.append(3))
    gate.force()                                                            # "Install now" in the sheet
    assert ran == [1, 3] and not gate.waiting


def test_desktop_updater_holds_the_install_and_says_why_when_off(native):
    from ave import desktop_updates as D
    said, waits, asked = [], [], []
    off = D.DesktopUpdater(native, lambda t, d: said.append((t, d)), state=U.status({"CFBundleVersion": "1"}))
    assert off.start() is False and off.title == D.OFF_TITLE
    off.check()
    assert said[-1][0] == "Updates are off in this build" and "development build" in said[-1][1]
    on = D.DesktopUpdater(native, lambda t, d: said.append((t, d)), waits.append,
                          state={"enabled": True, "reason": None, "version": "0.1.0", "build": "1", "feed": "configured"},
                          ask=lambda t, d: asked.append((t, d)) or False, on_main=lambda fn: fn(), background=lambda fn: fn())
    on.controller = types.SimpleNamespace(checkForUpdates_=lambda sender: said.append(("sparkle", None)))
    assert on.title == D.MENU_TITLE
    on.check()
    assert said[-1] == ("sparkle", None)
    native.busy = True
    ran = []
    assert on.gate.postpone(lambda: ran.append(1))
    on.check()
    assert asked[-1][0] == "An update is ready" and "an import is running" in asked[-1][1] and ran == []
    native.busy = False
    on.poll()
    assert ran == [1] and waits == [[]]


def test_the_menu_offers_check_for_updates():
    import ast
    from ave import desktop_native as N
    from ave.desktop_updates import MENU_TITLE, OFF_TITLE
    for title in (MENU_TITLE, OFF_TITLE):
        menus = dict(N.menu_entries(title))
        assert menus["AVE Studio"][0] == (title, "checkForUpdates:", "") and menus["AVE Studio"][-1][1] == "quit:"
    # Every selector the menus name is a method of the real Controller (AppKit itself is not run here).
    tree = ast.parse((ROOT / "src/ave/desktop_native.py").read_text())
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    cls = next(n for n in main.body if isinstance(n, ast.ClassDef) and n.name == "Controller")
    methods = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
    assert {sel.replace(":", "_") for _, rows in N.menu_entries(MENU_TITLE) for _, sel, _ in rows} <= methods
    assert "updateTick_" in methods


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr("ave.project.model.device_file", lambda: tmp_path / "device.json")
    from ave.project import Project
    p = Project.create(tmp_path / "ep", "Episode 12")
    return p


def _as_version(monkeypatch, version):
    """Run as the packaged release app of `version` (only a release build stamps its version on what it writes)."""
    import ave
    monkeypatch.setattr(ave, "__version__", version)
    monkeypatch.setattr(sys, "frozen", True, raising=False)


def test_a_source_run_never_stamps_a_version(tmp_path, monkeypatch):
    """A source checkout ahead of the shipped app writes no version, so it cannot lock teammates on the release read-only."""
    import ave
    from ave.project import Project
    monkeypatch.setattr("ave.project.model.device_file", lambda: tmp_path / "device.json")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(ave, "__version__", "9.0.0")
    p = Project.create(tmp_path / "ep", "Episode 12")
    e = p.log.append("artifact.created", artifact="art_a", data={"kind": "episode", "title": "A", "stage": "Plan"})
    assert "app" not in e and "app_version" not in json.loads(p.project_json.read_text())
    _as_version(monkeypatch, "1.0")                                   # a teammate on the 1.0 release
    assert not Project.open(p.root).read_only
    e = Project.open(p.root).log.append("note.added", data={"text": "x"})
    assert e["app"] == "1.0"


def test_project_version_newer_than_app_says_update_needed(project, monkeypatch):
    from ave import __version__
    from ave.project import Project
    from ave.project.log import LogError
    meta = json.loads(project.project_json.read_text())
    assert "app_version" not in meta and meta["v"] == 1                # a source run records no version
    assert not Project.open(project.root).read_only
    meta["app_version"] = "99.0.0"
    project.project_json.write_text(json.dumps(meta))
    newer = Project.open(project.root)
    assert newer.read_only and newer.update_needed == "This project was saved by a newer AVE (99.0.0). Update to edit it."
    assert newer.state() is not None                                   # still readable
    with pytest.raises(LogError, match="newer AVE"):
        newer.log.append("note.added", data={"text": "x"})
    meta["app_version"], meta["v"] = __version__, 2                    # a newer project format
    project.project_json.write_text(json.dumps(meta))
    assert "format 2" in Project.open(project.root).update_needed
    # The Workspace says so for that project.
    from ave.studio.ui.workspace import Workspace
    data = Workspace._with_update(Project.open(project.root), {"warnings": []})
    assert data["read_only"] and "newer AVE" in data["update_needed"] and data["warnings"][0] == data["update_needed"]


def test_a_project_written_by_a_newer_app_is_read_only(tmp_path, monkeypatch):
    """Created by 1.0, an event written by 1.2, opened by 1.1: update needed, read-only. No shared project.json rewrite."""
    monkeypatch.setattr("ave.project.model.device_file", lambda: tmp_path / "device.json")
    from ave.project import Project
    from ave.project.log import LogError
    _as_version(monkeypatch, "1.0")
    p = Project.create(tmp_path / "ep", "Episode 12")
    p.log.append("artifact.created", artifact="art_a", data={"kind": "episode", "title": "A", "stage": "Plan"})
    settings = p.project_json.read_bytes()
    _as_version(monkeypatch, "1.2")
    later = Project.open(p.root)
    later.log.append("artifact.created", artifact="art_b", data={"kind": "episode", "title": "B", "stage": "Plan"})
    assert [e.get("app") for e in later.read_events()] == ["1.0", "1.2"]
    assert p.project_json.read_bytes() == settings                         # the stamp lives in the writer's own log
    _as_version(monkeypatch, "1.1")
    older = Project.open(p.root)
    assert older.read_only and older.update_needed == "This project was saved by a newer AVE (1.2). Update to edit it."
    with pytest.raises(LogError, match="newer AVE"):
        older.log.append("note.added", data={"text": "x"})
    _as_version(monkeypatch, "1.2")
    assert not Project.open(p.root).read_only
    # Events from before stamps existed count as no version.
    log = next((p.root / ".ave/log").glob("*.jsonl"))
    lines = [json.loads(x) for x in log.read_text().splitlines()]
    for e in lines:
        e.pop("app", None)
    log.write_text("".join(json.dumps(e) + "\n" for e in lines))
    _as_version(monkeypatch, "1.0")
    assert not Project.open(p.root).read_only


def test_every_write_choke_point_refuses_a_read_only_project(tmp_path, monkeypatch):
    monkeypatch.setattr("ave.project.model.device_file", lambda: tmp_path / "device.json")
    from ave.episode import Episode, write_json
    from ave.project import Project
    from ave.project.model import ReadOnlyProject
    from ave.project.log import LogError
    from ave import editdoc
    from ave.publish import queue
    from ave.syncthing import conflicts, members
    root = tmp_path / "ep"
    ep = Episode(root)
    ep.create(title="Episode 12")
    _as_version(monkeypatch, "9.9")
    p = Project.create(root, "Episode 12")
    p.log.append("artifact.created", artifact="art_a", data={"kind": "episode", "title": "A", "stage": "Plan"})
    _as_version(monkeypatch, "1.0")
    words = "This project was saved by a newer AVE (9.9). Update to edit it."
    before = {f: f.read_bytes() for f in (root / "episode.json", root / ".ave/project.json")}
    doc = editdoc.new_doc("rough", 30, (1920, 1080), {})
    checks = {
        "log": lambda: Project.open(root).log.append("note.added", data={"text": "x"}),
        "edit version": lambda: editdoc.save_new_version(ep, doc),
        "project.json": lambda: members.write_meta(root, {**json.loads(before[root / ".ave/project.json"]), "title": "X"}),
        "episode.json": lambda: write_json(ep.meta_path, {"title": "changed"}),
        "stage mark": lambda: ep.mark_stage("ingest"),
        "settings conflict apply": lambda: conflicts._cas_write(ep.meta_path, b'{"title": "merged"}\n', before[root / "episode.json"]),
    }
    for name, write in checks.items():
        with pytest.raises((ReadOnlyProject, LogError)) as err:
            write()
        assert str(err.value) == words, name
    assert {f: f.read_bytes() for f in before} == before
    assert not list(ep.edits.glob("*.json"))
    write_json(ep.work / "scratch.json", {"local": True})                 # a local-only file never syncs: still allowed
    assert (ep.work / "scratch.json").is_file()
    queue.save(ep, {"id": "q1", "status": "draft"})                       # final ruling I3: the publish queue is local zone too
    assert (ep.publish / "queue" / "q1.json").is_file()


def test_private_feed_url_is_not_logged(feed, tmp_path, monkeypatch, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv(U.FEED_ENV, SECRET)
    secret_part = "k7Qz9-unguessable-0f3c"

    def broken(url):
        raise OSError(f"connection refused while fetching {url}")
    with pytest.raises(U.UpdateError) as err:
        U.check(SECRET, current="1", fetch=broken)
    assert secret_part not in str(err.value) and U.REDACTED in str(err.value)
    U.check(SECRET, current="20261103", system="15.0", fetch=lambda url: feed)

    from ave import doctor
    doctor.run()
    doctor.run(as_json=True)
    out = capsys.readouterr().out
    assert "Automatic updates" in out

    # The diagnostics bundle of a release build, whose Info.plist carries the address.
    from ave import desktop_runtime
    info = tmp_path / "Info.plist"
    info.write_bytes(plistlib.dumps({"CFBundleShortVersionString": "0.2.0", "CFBundleVersion": "20261110",
                                     "SUFeedURL": SECRET, "SUPublicEDKey": "A" * 43 + "="}))
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "components.json").write_text(json.dumps({"components": {}}))
    monkeypatch.setattr(desktop_runtime, "root", lambda: runtime)
    monkeypatch.setattr(U, "info_plist_path", lambda: info)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    bundle = json.dumps(desktop_runtime.diagnostics())
    assert json.loads(bundle)["updates"] == {"enabled": True, "reason": None, "version": "0.2.0", "build": "20261110", "feed": "configured"}
    rows = doctor.update_checks()          # the doctor line of that release build (the rest of doctor needs the real bundle)
    assert rows[0].status == doctor.OK and "feed configured" in rows[0].detail
    out += json.dumps([r.__dict__ for r in rows])
    for text in [out, bundle, caplog.text]:
        assert secret_part not in text and "updates.example.test" not in text


def test_bridge_files_refresh_once_after_an_update(tmp_path, monkeypatch):
    from ave import desktop_install as I
    home, data, state = tmp_path / "home", tmp_path / "bundle", tmp_path / "updates.json"
    (data / "ave/_data/skills/ave-one").mkdir(parents=True)
    script = data / "resolve-mcp/scripts/install_resolve_bridge.py"
    script.parent.mkdir(parents=True)
    target = home / I.SCRIPTS / "resolve_bridge.py"
    script.write_text(f"from pathlib import Path\nPath({str(target)!r}).write_text('new bridge')\n")
    resolve = tmp_path / "DaVinci Resolve"
    resolve.mkdir()
    real = I.install_bridge                     # with a stand-in Resolve install, so no real Scripts folder is touched
    monkeypatch.setattr(I, "install_bridge", lambda s, h: real(s, h, resolve))
    # Nothing was installed before: nothing is installed now, but the version is recorded.
    assert I.refresh_after_update("20261110", home, Path("/Apps/AVE Studio"), data, state) == {"version": "20261110"}
    assert not target.exists()
    target.parent.mkdir(parents=True)
    target.write_text("old bridge")
    I.install_shim(home, Path("/Apps/AVE Studio"))
    assert I.refresh_after_update("20261110", home, Path("/new"), data, state) is None              # once per version
    report = I.refresh_after_update("20261201", home, Path("/new"), data, state)
    assert report["resolve_bridge"]["status"] == "installed" and target.read_text() == "new bridge"
    assert report["ave"]["status"] == "installed" and "/new" in (home / ".local/bin/ave").read_text()
    assert I.refresh_after_update("20261201", home, Path("/new"), data, state) is None


# ------------------------------------------------------------------------------------------------- publish_update.py
FAKE_SIGNER = '''#!/bin/sh
# A stand-in for Sparkle's sign_update: no key, no Keychain.
file="$3"
case "$file" in
  *.xml) printf '\\n<!-- sparkle-signatures:\\nedSignature: ZmFrZQ==\\nlength: 1\\n-->\\n' >> "$file" ;;
  *) printf 'sparkle:edSignature="ZmFrZS1zaWduYXR1cmU=" length="%s"\\n' "$(wc -c < "$file" | tr -d ' ')" ;;
esac
'''


def test_publish_dry_run_signs_writes_the_appcast_and_uploads_nothing(tmp_path, monkeypatch, capsys):
    sys.path.insert(0, str(ROOT / "scripts"))
    import publish_update as P
    signer = tmp_path / "sign_update"
    signer.write_text(FAKE_SIGNER)
    signer.chmod(0o755)
    dmg = tmp_path / "AVE-Studio-0.2.0-arm64.dmg"
    dmg.write_bytes(b"not really a disk image" * 100)
    monkeypatch.setenv(U.FEED_ENV, SECRET)
    monkeypatch.setenv("AVE_R2_BUCKET", "ave-updates")
    monkeypatch.setattr(P, "put", lambda *a, **k: pytest.fail("a dry run uploaded"))
    monkeypatch.setattr(P, "credentials", lambda: pytest.fail("a dry run read credentials"))
    assert P.main(["--dmg", str(dmg), "--version", "0.2.0", "--build", "20261110", "--sign-update", str(signer),
                   "--out", str(tmp_path / "out"), "--dry-run", "--skip-notarization-check"]) == 0
    printed = capsys.readouterr()
    report = json.loads(printed.out)
    assert "unguessable" not in printed.out + printed.err
    assert [u["key"] for u in report["uploads"]] == ["[update feed]/AVE-Studio-0.2.0-arm64.dmg", "[update feed]/appcast.xml"]
    assert report["uploads"][0]["bytes"] == dmg.stat().st_size and report["ed_signature"] == "ZmFrZS1zaWduYXR1cmU="
    xml = (tmp_path / "out/appcast.xml").read_bytes()
    assert b"sparkle-signatures" in xml                                    # the feed itself is signed
    offered = U.check(SECRET, current="20261103", system="15.0", fetch=lambda url: xml)
    assert offered.version == "20261110" and offered.short_version == "0.2.0" and offered.length == dmg.stat().st_size
    assert offered.url == SECRET.rsplit("/", 1)[0] + "/AVE-Studio-0.2.0-arm64.dmg"
    assert P.plan_keys(SECRET, dmg.name)[:2] == ("k7Qz9-unguessable-0f3c/appcast.xml", "k7Qz9-unguessable-0f3c/AVE-Studio-0.2.0-arm64.dmg")


def test_publish_refuses_a_signature_for_a_different_file(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    import publish_update as P
    signer = tmp_path / "sign_update"
    signer.write_text('#!/bin/sh\necho \'sparkle:edSignature="eA==" length="3"\'\n')
    signer.chmod(0o755)
    dmg = tmp_path / "a.dmg"
    dmg.write_bytes(b"12345")
    with pytest.raises(SystemExit, match="different length"):
        P.sign_archive(signer, dmg)


def test_sigv4_matches_the_aws_reference_example():
    sys.path.insert(0, str(ROOT / "scripts"))
    import publish_update as P
    empty = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    headers = P.sigv4("GET", "https://examplebucket.s3.amazonaws.com/test.txt", {"Range": "bytes=0-9"},
                      access_key="AKIAIOSFODNN7EXAMPLE", secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                      region="us-east-1", payload_sha256=empty, amz_date="20130524T000000Z")
    assert headers["authorization"].endswith("Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")
    assert "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date" in headers["authorization"]


def test_redaction_covers_the_bare_prefix_and_object_keys():
    prefix = "k7Qz9-unguessable-0f3c"
    for text in (f"Upload of {prefix}/AVE.dmg failed", f"<Error><Key>{prefix}/appcast.xml</Key><Resource>/ave-updates/{prefix}/appcast.xml</Resource></Error>",
                 SECRET, SECRET.replace("-", "%2D"), f"https://updates.example.test/{prefix}"):
        out = U.redact(text, SECRET)
        assert prefix not in out and prefix.replace("-", "%2D") not in out and U.REDACTED in out, text


class _Response:
    def __init__(self, status, body):
        self.status, self.body = status, body

    def read(self, n=-1):
        return self.body


@pytest.mark.parametrize("failure", ["oserror", "httpexception", "s3error", "403"])
def test_publish_upload_errors_never_show_the_private_prefix(failure, tmp_path, monkeypatch, capsys):
    import http.client
    sys.path.insert(0, str(ROOT / "scripts"))
    import publish_update as P
    prefix = "k7Qz9-unguessable-0f3c"
    signer = tmp_path / "sign_update"
    signer.write_text(FAKE_SIGNER)
    signer.chmod(0o755)
    dmg = tmp_path / "AVE-Studio-0.2.0-arm64.dmg"
    dmg.write_bytes(b"x" * 100)
    for k, v in {U.FEED_ENV: SECRET, "AVE_R2_BUCKET": "ave-updates", "AVE_R2_ENDPOINT": "https://acct.r2.cloudflarestorage.com",
                 "AVE_R2_ACCESS_KEY_ID": "AKIDEXAMPLE", "AVE_R2_SECRET_ACCESS_KEY": "secretexample"}.items():
        monkeypatch.setenv(k, v)

    class Fake:
        def __init__(self, host, timeout=None):
            pass

        def request(self, method, path, body=None, headers=None):
            if failure == "oserror":
                raise ConnectionResetError(f"reset while sending {path}")
            if failure == "httpexception":
                raise http.client.InvalidURL(f"URL can't contain control characters. {path!r}")
            if failure == "s3error":
                raise RuntimeError(f"S3 client error: NoSuchBucket for key {prefix}/AVE.dmg")

        def getresponse(self):
            return _Response(403, f"<Error><Code>AccessDenied</Code><Key>{prefix}/AVE-Studio-0.2.0-arm64.dmg</Key>"
                                  f"<Resource>/ave-updates/{prefix}/AVE-Studio-0.2.0-arm64.dmg</Resource></Error>".encode())

        def close(self):
            pass
    monkeypatch.setattr(P.http.client, "HTTPSConnection", Fake)
    with pytest.raises(SystemExit) as err:
        P.main(["--dmg", str(dmg), "--version", "0.2.0", "--build", "20261110", "--sign-update", str(signer),
                "--out", str(tmp_path / "out"), "--skip-notarization-check"])
    printed = capsys.readouterr()
    message = str(err.value.code)
    assert message.startswith("Upload") and U.REDACTED in message
    for text in (message, printed.out, printed.err):
        assert prefix not in text and "Traceback" not in text


def test_once_the_restart_is_approved_new_work_is_refused(native):
    import threading
    from ave.desktop_files import NativeFileWork
    from ave.episode import Episode
    from ave.jobs import JobRunner
    server = native.session.server
    bench = server.app.workbench
    U.end_drain()
    # A render started before the update is approved holds the restart.
    hold = threading.Event()
    assert server._background(("render", "ep", "rough.v1"), hold.wait)
    gate, ran = U.RelaunchGate(native), []
    assert gate.postpone(lambda: ran.append(1))
    assert gate.poll() is False and not U.draining() and ran == []
    hold.set()
    for t in list(server._jobs.values()):
        t.join(5)
    # Safe and approved: the relaunch goes ahead and nothing new may start until it happens.
    assert gate.poll() is True and ran == [1] and U.draining()
    starts = {
        "room render": lambda: server._background(("render", "ep", "rough.v2"), lambda: None),
        "workbench job": lambda: bench.start_render({}),
        "workbench retry": lambda: bench.retry({"job_id": "job_" + "b" * 24}),
        "jobs panel": lambda: JobRunner(Episode(bench.sp.root)).start("status", {}),
        "file operation": lambda: NativeFileWork(lambda fn: fn()).start(lambda c, r: None, lambda *a: None),
    }
    for name, start in starts.items():
        with pytest.raises(U.Restarting, match="AVE is restarting to update"):
            start()
    assert not server._jobs
    # An update that is cancelled clears the flag, and work starts again.
    U.end_drain()
    assert not U.draining() and server._background(("render", "ep", "rough.v3"), lambda: None)
    for t in list(server._jobs.values()):
        t.join(5)


def test_the_last_moment_job_keeps_the_restart_waiting(native):
    """A job that starts between the safe check and the approval is seen by the re-check made with the flag up."""
    U.end_drain()
    server = native.session.server
    import threading
    hold = threading.Event()
    calls = []

    def reasons(app):
        calls.append(U.draining())
        if len(calls) == 1:                                  # the poll's check: safe, then a render slips in
            server._background(("render", "ep", "late"), hold.wait)
            return []
        return U.busy_reasons(app)
    gate, ran = U.RelaunchGate(native, reasons), []
    gate._pending = lambda: ran.append(1)
    assert gate.poll() is False and ran == [] and gate.waiting and not U.draining()
    assert calls == [False, True]                            # the re-check ran with the flag up
    hold.set()
    for t in list(server._jobs.values()):
        t.join(5)
    assert gate.poll() is True and ran == [1]
    U.end_drain()
