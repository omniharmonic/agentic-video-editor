"""`open_studio` wires every workstream into one real Studio, and the real server serves it without a 501 (integration, spec 11).

The wiring is where seams between workstreams hid: a hook nobody registered, an API call whose arguments the hook did not take, an
`accounts()` that said `live: False` for everything. These tests run the real thing end to end on `build_studio` data.
"""

import hashlib
import json
import threading
from datetime import date

import pytest

from ave.platforms import attest
from ave.studio import service
from ave.studio.service import HOOKS, INTENTIONALLY_UNREGISTERED, NotImplementedYet, Studio, open_studio, wire
from ave.studio.ui.server import StudioServer
from studio_helpers import build_studio
from studio.test_api import Client


@pytest.fixture
def built(tmp_path):
    return build_studio(tmp_path)


def test_open_studio_registers_every_hook_except_the_intentional_ones(built):
    st = open_studio(built.sp.root)
    missing = sorted(h for h in HOOKS if not st.has(h))
    assert missing == sorted(INTENTIONALLY_UNREGISTERED) == ["approve", "record_view", "reject"]
    for h in HOOKS:
        if h in INTENTIONALLY_UNREGISTERED:
            with pytest.raises(NotImplementedYet):
                st.call(h)


def test_a_plain_studio_has_no_hooks_and_wire_adds_them_all(built):
    st = Studio(built.sp)
    assert not any(st.has(h) for h in HOOKS)
    wire(st)
    assert all(st.has(h) for h in HOOKS if h not in INTENTIONALLY_UNREGISTERED)


def test_accounts_report_the_true_live_and_paused_state(built):
    st = open_studio(built.sp.root, clock=built.clock)
    rows = {a["id"]: a for a in st.accounts()}
    assert set(rows) == {"ig-main", "fb-page", "ig-alt"} and not any(a["live"] for a in rows.values())
    attest.write_live(built.sp.accounts_dir, "ig-main", "maren")
    assert {a["id"]: a["live"] for a in st.accounts()} == {"ig-main": True, "fb-page": False, "ig-alt": False}
    # a hand-edit, a copy onto another account and a signed "off" are all NOT live
    p = attest.live_path(built.sp.accounts_dir, "ig-main")
    good = json.loads(p.read_text())
    p.write_text(json.dumps({**good, "by": "claude"}))
    assert not {a["id"]: a for a in st.accounts()}["ig-main"]["live"]
    p.write_text(json.dumps(good))
    attest.live_path(built.sp.accounts_dir, "fb-page").write_text(json.dumps({**good, "account": "fb-page"}))
    assert not {a["id"]: a for a in st.accounts()}["fb-page"]["live"]
    attest.write_live(built.sp.accounts_dir, "ig-main", "maren", live=False)
    assert not {a["id"]: a for a in st.accounts()}["ig-main"]["live"]
    attest.write_live(built.sp.accounts_dir, "ig-main", "maren")
    (built.sp.accounts_dir / "ig-main.paused").write_text("{}")
    row = {a["id"]: a for a in st.accounts()}["ig-main"]
    assert row["live"] and row["paused"]
    # the same truth as the calendar's
    cal = st.calendar(date(2026, 10, 1), date(2026, 12, 31))
    assert {a["id"]: (a["live"], a["paused"]) for a in cal["accounts"]} == {a["id"]: (a["live"], a["paused"]) for a in st.accounts()}


def test_account_rows_carry_credential_names_but_never_values(built, monkeypatch):
    st = open_studio(built.sp.root, clock=built.clock)
    row = {a["id"]: a for a in st.accounts()}["ig-main"]
    assert row["credentials"]["ready"] is False and any(n.startswith("instagram_") and n.endswith("@ig-main") for n in row["credentials"]["missing"])
    assert row["quota"] == {"used_24h": 0, "limit_24h": 2} and row["last_tick"] is None
    monkeypatch.setenv("AVE_INSTAGRAM_TOKEN_IG_MAIN", "EAAB" + "z" * 40)
    monkeypatch.setenv("AVE_INSTAGRAM_USER_ID_IG_MAIN", "17841400000000000")
    row = {a["id"]: a for a in st.accounts()}["ig-main"]
    assert "EAAB" not in json.dumps(st.accounts())


# ---------------------------------------------------------------------------------------------------- the real server over the real Studio

@pytest.fixture
def real(built):
    st = open_studio(built.sp.root, clock=built.clock, publishers=built.publishers)
    s = StudioServer(st, port=0, by="tester", pin="424242")
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield Client(s), built, st
    s.shutdown()
    s.server_close()


def test_every_get_endpoint_answers_from_the_real_studio(real):
    cl, built, st = real
    pid = built.posts["scheduled"]
    vid = next(v["id"] for v in st.post(pid)["variants"])
    asset = built.assets[0]["id"]
    assert cl.get("summary")[0] == 200
    s, body = cl.get("accounts")
    assert s == 200 and {a["id"] for a in body["accounts"]} == {"ig-main", "fb-page", "ig-alt"}
    s, body = cl.get("calendar?from=2026-10-01&to=2026-12-31")
    assert s == 200 and set(body) >= {"tz", "cursor", "accounts", "days", "slots", "unscheduled", "proposals"}
    s, body = cl.get("grid/ig-main")
    assert s == 200 and body["layout"]["kind"] == "profile_grid" and body["layout"]["cell_aspect"] == "3:4"
    assert body["account"]["id"] == "ig-main" and body["cells"] and set(body["cells"][0]) >= {"post", "variant", "at", "status", "thumb", "w", "h", "rev", "locked", "crop"}
    assert cl.get("grid/fb-page")[1]["layout"]["kind"] == "page_timeline"
    assert cl.get("grid/nobody")[0] == 404
    s, body = cl.get("posts")
    assert s == 200 and body["total"] == 12
    s, body = cl.get(f"posts/{pid}")
    assert s == 200 and set(body) >= {"post", "feedback", "events", "queue"}
    s, body = cl.get(f"posts/{pid}/preview/{vid}")
    assert s == 200 and body["approximate"] and body["caption"]["counter"]["unit"] and body["media"][0]["url"].startswith("/files/library/assets/")
    s, body = cl.get("library")
    assert s == 200 and body["total"] >= 1 and "license_ok" in body["assets"][0]
    assert cl.get("library?license=ok&source=own&kind=image")[0] == 200
    assert cl.get("library?license=bogus")[0] == 400
    s, body = cl.get(f"library/{asset}")
    assert s == 200 and body["asset"]["id"] == asset and "license" in body
    assert cl.get("feedback")[0] == 200 and cl.get("alerts")[0] == 200 and cl.get("changes")[0] == 200


def test_grid_orders_newest_first_and_marks_the_crop(real):
    cl, built, st = real
    cells = cl.get("grid/ig-main")[1]["cells"]
    ats = [c["at"] for c in cells if c["at"]]
    assert ats == sorted(ats, reverse=True)
    assert all(c["at"] for c in cells[:len(ats)]) and all(c["at"] is None for c in cells[len(ats):])
    ov = cl.get(f"posts/{built.posts['in_review']}/preview/v_003")[1]["media"][0]["crop_overlay"]
    assert ov["cell_aspect"] == "3:4" and ov["lost"]["left_pct"] == 3 and ov["lost"]["right_pct"] == 3    # a 4:5 image loses ~6 % of its width


def test_pause_resume_upload_library_and_swap_asset_through_the_real_hooks(real, tmp_path):
    cl, built, st = real
    assert cl.post("pause", {})[1] == {"paused": True, "scope": "studio"}
    assert cl.post("resume", {"pin": "424242"})[0] == 404                       # resuming is the app's native act now
    assert cl.post("pause", {"account": "ig-main"})[1]["scope"] == "ig-main"
    from studio_helpers import make_png
    png = make_png(tmp_path / "up.png", (1080, 1350), (9, 99, 199))
    data = png.read_bytes()
    assert cl.api("PUT", "library/upload?name=up.png", raw=data)[0] == 404        # files enter the library only through the app's panel
    body = st.upload("up.png", png, by="tester")                                   # what the app's native import calls
    assert body["duplicate"] is False and body["asset"]["sha256"] == hashlib.sha256(data).hexdigest()
    new = body["asset"]["id"]
    assert st.upload("up.png", png, by="tester")["duplicate"] is True
    # swap the asset of an in-review variant: the queue item follows the new bytes
    pid = built.posts["in_review"]
    post = cl.get(f"posts/{pid}")[1]["post"]
    v = post["variants"][0]
    before = cl.get(f"posts/{pid}")[1]["queue"][v["id"]]["sha256"]
    old_alt = post["media"][0]["alt"]
    s, body = cl.post(f"posts/{pid}/swap_asset", {"variant": v["id"], "expected_rev": post["rev"], "index": 0, "asset": new})
    assert s == 200 and body["post"]["rev"] > post["rev"]
    assert body["post"]["variants"][0]["media"][0]["asset"] == new and body["post"]["variants"][0]["media"][0]["alt"] == old_alt   # alt carried over
    assert cl.post(f"posts/{pid}/swap_asset", {"variant": v["id"], "expected_rev": post["rev"], "index": 0, "asset": new})[0] == 409    # stale rev
    assert cl.post(f"posts/{pid}/swap_asset", {"variant": v["id"], "expected_rev": body["post"]["rev"], "index": 5, "asset": new})[0] == 400
    after = cl.get(f"posts/{pid}")[1]["queue"][v["id"]]["sha256"]
    assert after != before and cl.get(f"posts/{pid}")[1]["queue"][v["id"]]["status"] == "awaiting_approval"


def test_swap_asset_on_an_approved_variant_voids_the_approval(real, tmp_path):
    cl, built, st = real
    from studio_helpers import make_png
    pid = built.posts["approved"]
    post = cl.get(f"posts/{pid}")[1]["post"]
    v = post["variants"][0]
    assert v["status"] in ("approved", "scheduled")
    body = st.upload("b.png", make_png(tmp_path / "b.png", (1080, 1350), (200, 9, 9)), by="tester")
    from ave.library import update_asset
    update_asset(built.sp, body["asset"]["id"], alt="A red square test image.")
    s, body = cl.post(f"posts/{pid}/swap_asset", {"variant": v["id"], "expected_rev": post["rev"], "index": 0, "asset": body["asset"]["id"]})
    assert s == 200 and any("approval dropped" in w for w in body["warnings"])
    assert cl.get(f"posts/{pid}")[1]["post"]["variants"][0]["status"] == "in_review"


def test_move_swap_unschedule_feedback_rehearse_and_broll_through_the_real_hooks(built, tmp_path):
    from studio_broll_helpers import StockHttp, jpeg_bytes
    from ave.broll.providers import Pexels
    http = StockHttp("pexels.json", downloads={
        "https://images.pexels.com/photos/2284/pexels-photo-2284.jpeg?auto=compress&cs=tinysrgb&h=350": jpeg_bytes(),
        "https://images.pexels.com/photos/2284/pexels-photo-2284.jpeg": jpeg_bytes((200, 100, 50), (640, 800)),
        "https://images.pexels.com/photos/3301/m.jpeg": jpeg_bytes()})
    prov = Pexels(api_key="sk-test-0123456789abcdef", http=http, cache_dir=tmp_path / "cache", now=lambda: built.clock.now().timestamp())
    st = open_studio(built.sp.root, clock=built.clock, publishers=built.publishers, providers={"pexels": prov})
    s = StudioServer(st, port=0, by="tester", pin="424242")
    threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        cl = Client(s)
        pid = built.posts["scheduled"]
        post = cl.get(f"posts/{pid}")[1]["post"]
        v = post["variants"][0]
        # a drag to another time on the same account
        to = {"local": "2026-11-12T10:30", "tz": "America/Denver"}
        s1, body = cl.post(f"posts/{pid}/move", {"variant": v["id"], "expected_rev": post["rev"], "to": to, "snap": "none"})
        assert s1 == 200 and body["post"]["variants"][0]["schedule"]["local"] == "2026-11-12T10:30", body
        rev = body["post"]["rev"]
        assert cl.post(f"posts/{pid}/move", {"variant": v["id"], "expected_rev": post["rev"], "to": to})[0] == 409          # stale rev
        s1, body = cl.post(f"posts/{pid}/unschedule", {"variant": v["id"], "expected_rev": rev})
        assert s1 == 200 and not (body["post"]["variants"][0]["schedule"] or {}).get("utc")
        # a dry rehearsal tick posts nothing (the CLI's `ave studio tick`; its old web route is gone)
        assert cl.post("tick/rehearse", {})[0] == 404
        body = st.rehearse()
        assert body["mode"] == "dry-run" and body["posted"] == []
        # send back to Claude with a rework note (a feedback line; the open note blocks dispatch)
        s1, body = cl.post(f"posts/{pid}/feedback", {"variant": v["id"], "kinds": ["rework_copy"], "note": "too salesy"})
        assert s1 == 201 and body["feedback"][0]["kind"] == "rework_copy"
        assert cl.get(f"posts/{pid}")[1]["post"]["variants"][0]["status"] == "needs_rework"
        # B-roll: search stores a local thumb; a pick needs a name and lands in the library with a license entry
        s1, body = cl.post("broll/search", {"query": "bread", "providers": ["pexels"], "kind": "photo"})
        assert s1 == 201, body
        sid = body["search"]["id"]
        cand = body["search"]["candidates"][0]
        assert cand["thumb"].startswith("/files/work/broll/")
        assert cl.req("GET", cand["thumb"])[0] == 200 and cl.req("GET", cand["thumb"])[2][:3] == b"\xff\xd8\xff"     # served by the shared /files/ route
        assert cl.get(f"broll/{sid}")[1]["id"] == sid
        s1, body = cl.post("broll/pick", {"search": sid, "candidate": f"{cand['provider']}:{cand['asset_id']}", "by": "Maren"})
        assert s1 == 201 and body["asset"]["id"].startswith("a_") and body["license_entry"], body
    finally:
        s.shutdown()
        s.server_close()


def test_review_then_approve_over_the_real_hooks_never_sends(real):
    cl, built, st = real
    pid = built.posts["in_review"]
    detail = cl.get(f"posts/{pid}")[1]
    v = detail["post"]["variants"][0]
    sha = detail["queue"][v["id"]]["sha256"]
    assert cl.post(f"posts/{pid}/view", {"variant": v["id"], "sha": sha, "displayed_ms": 2500})[0] in (400, 404)     # no web route records a view
    assert cl.post("approve", {"items": [{"post": pid, "variant": v["id"], "sha": sha}], "by": "Maren", "pin": "424242"})[0] == 404
    app = cl.srv.app                                                       # what the app's approval sheet does in process, after its check
    app.record_view(pid, {"variant": v["id"], "sha": sha, "displayed_ms": 2500}, via="native-sheet")
    body = app.approve({"items": [{"post": pid, "variant": v["id"], "sha": sha}], "by": "Maren", "pin": "424242"})
    assert len(body["approved"]) == 1 and body["refused"] == []
    assert cl.get(f"posts/{pid}")[1]["queue"][v["id"]]["status"] == "approved"
    assert all(p.publish_calls == 0 for p in built.publishers.values())
