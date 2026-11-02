"""`ave studio demo`: a synthetic, populated studio. Smoke tests: it builds fast and offline, every record validates against its schema, every status is
present, every media file exists, nothing in it is a secret, it refuses a non-empty folder, and it is deterministic."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ave import cli
from ave.platforms.accounts import find_secrets, list_accounts
from ave.studio import demo, doctor, ledger, model, state, store
from ave.studio.service import open_studio

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=ZoneInfo(demo.TZ))
ALL_STATUSES = {"draft", "in_review", "needs_rework", "approved", "scheduled", "published", "failed", "missed", "publishing", "dropped"}


KEY = "demo-test-key"


def _env(mp) -> None:
    from content_helpers import BROWSERS               # the suite repoints HOME; Chromium lives under the real one
    if BROWSERS:
        mp.setenv("PLAYWRIGHT_BROWSERS_PATH", BROWSERS)
    mp.setenv("AVE_APPROVAL_KEY", KEY)                  # a fixed approval key: the keychain is a fresh in-memory one per test


@pytest.fixture(autouse=True)
def demo_env(monkeypatch):
    _env(monkeypatch)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    import time
    root = tmp_path_factory.mktemp("demo") / "studio"
    with pytest.MonkeyPatch.context() as mp:
        _env(mp)
        t = time.time()
        res = demo.build_demo(root)                  # the real clock: statuses depend on "now", so the data is laid out around it
        res["_elapsed"] = time.time() - t
    return root, res


def test_builds_quickly_with_the_promised_shape(built):
    root, res = built
    # This is a runaway-build smoke check, including concurrent Chromium/FFmpeg
    # workers in the full suite; precise performance belongs in a serial benchmark.
    assert res["_elapsed"] < 60, res["_elapsed"]
    assert res["accounts"] == 7 and res["posts"] >= 38
    assert (root / "studio.json").is_file() and (root / "DEMO.md").is_file()
    assert res["assets"] >= 30


def test_every_status_and_stage_is_present(built):
    root, res = built
    assert ALL_STATUSES <= set(res["variants_by_status"]), res["variants_by_status"]
    assert {"idea", "draft", "active", "archived"} <= {k for k, v in res["stages"].items() if v}
    s = open_studio(root)
    assert len(s.feedback("open")) >= 4
    assert s.feedback("resolved")                                              # history exists too
    assert len(s.alerts()) >= 5 and len(s.alerts(include_acked=True)) > len(s.alerts())
    assert {a["severity"] for a in s.alerts()} == {"error", "warn", "info"}
    from ave.schedule import proposals
    assert len(proposals.pending(s.sp)) == 2
    assert len(list(s.sp.campaigns_dir.glob("c_*.json"))) == 2


def test_every_record_validates_against_its_schema(built):
    root, _ = built
    s = open_studio(root)
    sp = s.sp
    assert model.validate("studio", sp.read_meta()) == []
    for p in sp.posts_dir.glob("*.json"):
        assert model.validate("post", json.loads(p.read_text())) == [], p.name
    for c in sp.campaigns_dir.glob("c_*.json"):
        assert model.validate("campaign", json.loads(c.read_text())) == [], c.name
    for a in store.read_assets(sp).values():
        assert model.validate("asset", a) == [], a["id"]
    assert len(list_accounts(sp)) == 7
    for line in (sp.ledger_dir / "events.jsonl").read_text().splitlines():
        assert model.validate("event", json.loads(line)) == []
    for line in (sp.ledger_dir / "alerts.jsonl").read_text().splitlines():
        r = json.loads(line)
        if r.get("id"):
            assert model.validate("alert", r) == []
    for line in (sp.ledger_dir / "feedback.jsonl").read_text().splitlines():
        assert model.validate("feedback", json.loads(line)) == []
    for p in (sp.schedule_dir).glob("*.yaml"):
        import yaml
        assert model.validate("schedule_rules", yaml.safe_load(p.read_text())) == [], p.name
    assert library_ok(sp)


def library_ok(sp) -> bool:
    from ave import library
    rep = library.verify(sp)
    assert rep["ok"], rep["problems"][:3]
    return True


def test_every_media_file_exists_and_licenses_are_covered(built):
    root, _ = built
    s = open_studio(root)
    sp = s.sp
    assets = store.read_assets(sp)
    assert all((root / a["file"]).is_file() for a in assets.values())
    from ave import library
    cov = library.coverage(sp, list(assets))
    assert not cov["missing"], cov["missing"][:3]
    used = set()
    for p in store.list_posts(sp):
        used |= set(model.media_asset_ids(p))
        for v in p["variants"]:
            if v.get("queue"):
                item = state.load_queue_item(sp, v)
                assert item is not None, (p["id"], v["id"])
                for f in __import__("ave.publish.queue", fromlist=["x"]).resolve_files(sp, item):
                    assert f.is_file(), f
    assert used <= set(assets)
    stock = [a for a in assets.values() if (a["source"] or {}).get("type") == "stock"]
    assert any(a["kind"] == "video" for a in stock) and any(a["license"]["attribution"]["required"] for a in stock)
    assert any(a["ai_generated"] for a in assets.values())
    # the renderer's pictures: exact sizes, nothing empty
    from PIL import Image
    cards = [a for a in assets.values() if (a["source"] or {}).get("type") == "render" and a["ext"] == ".png"]
    assert len(cards) >= 30
    for a in cards[:6]:
        with Image.open(root / a["file"]) as im:
            assert im.size in ((1080, 1350), (1080, 1080), (1080, 1920), (1200, 628)), im.size


def test_no_secrets_no_live_accounts_nothing_sendable(built):
    root, _ = built
    assert find_secrets(root) == []
    s = open_studio(root)
    for a in s.accounts():
        assert a["live"] is False and not (root / "accounts" / f"{a['id']}.live.json").exists()
    fictional = next(a for a in list_accounts(s.sp) if a.id == "li-longtable")
    assert fictional.connection["scopes"] == ["w_organization_social"]
    assert "synthetic offline demo" in fictional.connection["source"]
    assert not fictional.live_ok()
    assert not (root / "send.enabled").exists()
    photos = [v for p in store.list_posts(s.sp) for v in p['variants'] if v['platform']=='tiktok']
    assert photos and all(v['delivery']['method']=='manual' for v in photos)
    assert all('verified_domains' not in a.connection for a in list_accounts(s.sp))
    rep = doctor.run(s)
    bad = [c for c in rep["checks"] if c["status"] == "fail"]
    assert not bad, bad
    text = "\n".join(p.read_text(errors="ignore") for p in root.rglob("*.json") if p.stat().st_size < 1_000_000) + \
        "\n".join(p.read_text(errors="ignore") for p in root.rglob("*.yaml"))
    assert ".invalid" in text
    for needle in ("http://", "https://"):
        for line in text.splitlines():
            for tok in line.split(needle)[1:]:
                host = tok.split("/")[0].split('"')[0]
                assert host.endswith(".invalid") or host in ("creativecommons.org", "json-schema.org"), host


def test_approvals_exist_and_are_valid_so_the_ui_shows_scheduled_posts(built):
    root, _ = built
    s = open_studio(root)
    posts = s.posts()
    sched = [v for p in posts for v in p["variants"] if v["status"] == "scheduled"]
    assert sched and all(v["approval"] and v["approval"]["by"] == "demo" for v in sched)
    assert not [v for p in posts for v in p["variants"] if "approval_stale" in v["flags"]]


def test_refuses_a_non_empty_folder_and_never_overwrites(tmp_path):
    d = tmp_path / "taken"
    d.mkdir()
    (d / "keep.txt").write_text("mine")
    with pytest.raises(demo.DemoError, match="not empty"):
        demo.build_demo(d)
    assert (d / "keep.txt").read_text() == "mine" and len(list(d.iterdir())) == 1
    f = tmp_path / "afile"
    f.write_text("x")
    with pytest.raises(demo.DemoError):
        demo.build_demo(f)


def test_cli_builds_prints_next_steps_and_refuses_the_second_run(tmp_path, capsys):
    d = tmp_path / "cli-demo"
    rc = cli.main(["studio", "demo", str(d)])
    cap = capsys.readouterr()
    res = json.loads(cap.out)
    assert rc == 0 and res["ok"] and res["posts"] >= 38
    assert f"ave studio ui {d} --open" in res["next"][0] and "ave studio ui" in cap.err and "PIN" in cap.err
    rc = cli.main(["studio", "demo", str(d)])
    cap = capsys.readouterr()
    assert rc == 2 and json.loads(cap.out)["blocked"] is True


def test_the_same_day_gives_the_same_pictures_and_posts(tmp_path, built):
    root, _ = built
    other = tmp_path / "again"
    demo.build_demo(other)
    a = {r["sha256"] for r in store.read_assets(open_studio(root).sp).values()}
    b = {r["sha256"] for r in store.read_assets(open_studio(other).sp).values()}
    assert a == b
    pa = {p["id"]: [(v["account"], v["post_type"]) for v in p["variants"]] for p in store.list_posts(open_studio(root).sp)}
    pb = {p["id"]: [(v["account"], v["post_type"]) for v in p["variants"]] for p in store.list_posts(open_studio(other).sp)}
    assert pa == pb
    assert ledger.open_feedback(open_studio(root).sp) and len(ledger.all_feedback(open_studio(root).sp)) == len(ledger.all_feedback(open_studio(other).sp))


def test_captions_fit_each_platform(built):
    root, _ = built
    from ave.publish.adapters.base import load_platforms
    cfg = load_platforms()
    for p in store.list_posts(open_studio(root).sp):
        for v in p["variants"]:
            cap = (v["copy"]["caption"] or "")
            lim = ((cfg.get(v["platform"]) or {}).get("image") or {}).get("caption_max_chars")
            if lim:
                assert len(cap) <= lim, (p["id"], v["account"], len(cap), lim)


def test_the_ui_command_opens_a_wired_studio(built, monkeypatch, capsys):
    """`ave studio ui DIR` once built a bare `Studio` (no calendar, grid or library hooks: every page answered 501). It must open a wired one."""
    from types import SimpleNamespace

    from ave import humanpresence
    from ave.studio.ui import server as srv

    seen = {}

    class Fake:
        port = 1

        def __init__(self, studio, **kw):
            seen["studio"] = studio
            self.app = SimpleNamespace(pin=SimpleNamespace(reveal=lambda: "000000"))

        def url(self):
            return "http://127.0.0.1:1/?t=x"

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr(humanpresence, "require_human", lambda *a, **k: None)
    monkeypatch.setattr(srv, "StudioServer", Fake)
    args = SimpleNamespace(dir=str(built[0]), port=0, open=False, by="tester", fixtures=False)
    assert srv.run_cli(args) == 0
    st = seen["studio"]
    assert st.has("calendar") and st.has("grid") and st.has("library")
    assert "approval PIN" in capsys.readouterr().out
