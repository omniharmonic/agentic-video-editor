"""A human's copy edit through the UI must reach the queue (spec 2.8): otherwise the editor shows an edit as live that the approved item does not contain."""

import threading

import pytest

from ave.publish import queue as pq
from ave.studio.service import open_studio
from ave.studio.ui.server import StudioServer
from studio.test_api import Client
from studio_helpers import build_studio


@pytest.fixture
def real(tmp_path):
    built = build_studio(tmp_path)
    from ave.library import registry
    from ave.library.ingest import default_own_entry
    for a in built.assets:                      # the builder registers assets without a license entry; give them the real own-work one
        registry.license_log(built.sp).append({**default_own_entry("test", "2026-10-01T00:00:00"), "sha256": a["sha256"], "file": a["file"],
                                               "bytes": a["bytes"], "approved_by": "test"})
    st = open_studio(built.sp.root, clock=built.clock, publishers=built.publishers)
    s = StudioServer(st, port=0, by="tester", pin="424242")
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield Client(s), built
    s.shutdown()
    s.server_close()


def test_a_humans_copy_edit_reaches_the_queue_and_voids_the_approval(real):
    cl, built = real
    pid = built.posts["approved"]
    post = cl.get(f"posts/{pid}")[1]["post"]
    v = post["variants"][0]
    assert v["status"] in ("approved", "scheduled")
    s, body = cl.post(f"posts/{pid}/edit", {"expected_rev": post["rev"], "patch": {"copy.base": "A plainer caption, edited by the human."}, "by": "Maren"})
    assert s == 200 and any("approval dropped" in w for w in body["warnings"]), body
    after = cl.get(f"posts/{pid}")[1]
    assert after["post"]["variants"][0]["status"] == "in_review" and after["queue"][v["id"]]["status"] == "awaiting_approval"
    assert body["post"]["rev"] == after["post"]["rev"]                                  # the answer carries the post as it is now
    assert pq.load(built.sp, after["queue"][v["id"]]["id"])["caption"] == "A plainer caption, edited by the human."
    # saving the same words again changes nothing and warns about nothing
    s, body = cl.post(f"posts/{pid}/edit", {"expected_rev": after["post"]["rev"], "patch": {"copy.base": "A plainer caption, edited by the human."}, "by": "Maren"})
    assert s == 200 and body["warnings"] == []


def test_an_edit_of_a_post_with_no_queue_item_just_saves(real):
    cl, built = real
    pid = built.posts["draft"]
    post = cl.get(f"posts/{pid}")[1]["post"]
    s, body = cl.post(f"posts/{pid}/edit", {"expected_rev": post["rev"], "patch": {"copy.base": "Hello"}, "by": "Maren"})
    assert s == 200 and body["warnings"] == [] and body["post"]["copy"]["base"] == "Hello" and body["post"]["rev"] == post["rev"] + 1
