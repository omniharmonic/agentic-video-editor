"""Sharing one project with one person: real invitations between Syncthing homes on loopback (two, or three for a third
member or a third party), the six-digit check, expiry, a wrong code, membership from the owner's log, removal, a studio
joining with studio rules, and publisher handover. Uses tests/sync/harness.py; the Syncthing tests are skipped when there is
no syncthing binary. The fake manager below covers the membership rules without Syncthing."""

from __future__ import annotations

import json
import time

import pytest

from ave.collab_identity import IdentityError, whois
from ave.project import Device, Project, reduce, short_id, zones
from ave.project.log import read_logs
from ave.syncthing import members as M

from . import harness as H

PROXIES = "proxies"
SID = {k: "-".join([k * 7] * 8) for k in "ABCDEFGHIJX"}


@pytest.fixture
def sync_trio(short):
    homes = [H.make_home(short, n, d) for n, d in (("maren", "dev_A"), ("tomas", "dev_B"), ("cara", "dev_C"))]
    for h in homes:
        h.manager.start()
    yield homes
    for h in homes:
        h.manager.stop()


def _owner_project(base, pid="prj_SHARE", title="The Long Table", kind="episode", studio=False):
    root = base / "A" / title
    H.write_project_json(root, pid, title, "flat", [{"person": "maren", "devices": ["dev_A"], "media": "full"}], "dev_A")
    meta = json.loads((root / ".ave" / "project.json").read_text())
    meta["kind"] = kind
    (root / ".ave" / "project.json").write_text(json.dumps(meta, indent=2) + "\n")
    if studio:
        H.write_tree(root, ["studio.json", "posts/p1.json", "campaigns/c1.json", "accounts/acct1.yaml"])
    H.write_tree(root, ["edits/rough.v1.json"])
    return root


def _events(root, op):
    return [e for e in read_logs(root / ".ave" / "log") if e["op"] == op]


def _join(a, b, owner_root, tmp_path, person="tomas", media=PROXIES):
    """The whole invitation: invite, accept, compare the code both ways, both confirm. Returns the invitee's folder."""
    text = M.invite(owner_root, person, media, manager=a.manager)
    (tmp_path / "B").mkdir(exist_ok=True)
    joined = M.accept(text, tmp_path / "B", manager=b.manager)
    code_b = M.join_code(joined)                                        # only Tomas's screen shows it
    H.wait_until(lambda: M.answers(owner_root, manager=a.manager), timeout=60, what="the invitee to answer")
    M.confirm_invitee(owner_root, code_b, manager=a.manager)            # Maren types what Tomas read aloud
    M.confirm_join(joined, manager=b.manager)                           # Tomas's side checks it is Maren's computer
    return joined.root


def _folder_devices(manager, fid):
    f = next((f for f in manager.api.get("/rest/config/folders") if f["id"] == fid), None)
    return None if f is None else {d["deviceID"] for d in f["devices"]}


# ---------------------------------------------------------------------------------------------------- the brief's tests
def test_invitation_covers_one_project_only(sync_pair, tmp_path):
    a, b = sync_pair
    shared = _owner_project(tmp_path)
    other = _owner_project(tmp_path, "prj_OTHER", "Private Notes")
    a.manager.share(other)                                              # synced, but with nobody else
    rb = _join(a, b, shared, tmp_path)
    assert rb == tmp_path / "B" / "The Long Table"
    H.wait_until(lambda: (rb / "edits" / "rough.v1.json").is_file(), what="the shared project's files on B")
    # project.json was written with the same bytes on both sides, so it never conflicts
    assert (rb / ".ave" / "project.json").read_bytes() == (shared / ".ave" / "project.json").read_bytes()
    a.manager.wait(shared, "up_to_date", 60)
    assert not any("sync-conflict" in p for p in H.files_in(rb))
    # the other project was never offered to B, and B holds only the one it was invited to
    assert _folder_devices(a.manager, "prj_OTHER") == {a.manager.device_id()}
    assert [f["id"] for f in b.manager.api.get("/rest/config/folders")] == ["prj_SHARE"]
    assert "prj_OTHER" not in (b.manager.api.get("/rest/cluster/pending/folders") or {})
    meta = json.loads((shared / ".ave" / "project.json").read_text())
    tomas = next(m for m in meta["members"] if m["person"] == "tomas")
    assert tomas == {"person": "tomas", "devices": ["dev_B"], "media": PROXIES}
    assert meta["devices"]["dev_B"]["sync_id"] == b.manager.device_id()
    added = _events(shared, "member.added")
    assert [(e["data"]["person"], e["data"]["device"], e["person"]) for e in added] == [("maren", "dev_A", "maren"),
                                                                                        ("tomas", "dev_B", "maren")]


def test_only_the_invitee_shows_the_code(sync_pair, tmp_path):
    a, b = sync_pair
    root = _owner_project(tmp_path)
    text = M.invite(root, "tomas", PROXIES, manager=a.manager)
    (tmp_path / "B").mkdir()
    joined = M.accept(text, tmp_path / "B", manager=b.manager)
    code = M.join_code(joined)
    got = H.wait_until(lambda: M.answers(root, manager=a.manager), timeout=60, what="an answer")
    assert len(code) == 6 and code.isdigit()
    assert got[0].device == "dev_B" and got[0].sync_id == b.manager.device_id() and not got[0].problem
    assert not hasattr(got[0], "code")                                  # the owner's side never holds a code to show
    inv = M.parse_invitation(text, allow_loopback=True)
    assert code == M.comparison_code(a.manager.device_id(), b.manager.device_id(), inv["nonce"])
    assert M.comparison_code(a.manager.device_id(), b.manager.device_id(), "0" * 32) != code    # the nonce is part of it
    # before either side confirms, nothing is shared
    assert _folder_devices(a.manager, "prj_SHARE") in (None, {a.manager.device_id()})
    assert b.manager.api.get("/rest/config/folders") == []
    assert M.joining(joined)["inviter"] == "maren"
    # Tomas's side cannot confirm on its own: it waits for Maren's computer to offer the project
    with pytest.raises(M.MemberError, match="not confirmed yet"):
        M.confirm_join(joined, manager=b.manager, wait_s=2)
    assert b.manager.api.get("/rest/config/folders") == []


def test_the_owners_sheet_shows_no_code():
    """Only the invitee's sheet shows digits; the owner's sheet asks for them."""
    import inspect
    import re as _re
    from ave import desktop_native as N
    for n in (1, 3):
        words = N.inviter_code_words("Tomas", n)
        assert not _re.search(r"\d{3}\s?\d{3}", words) and "type it here" in words
    assert "123 456" in N.invitee_code_words("Maren", "123456")
    src = inspect.getsource(N)
    assert src.count("display_code(") == 1 and "join_code(" in src and ".code" not in src


def test_a_third_partys_code_adds_only_that_third_party(sync_trio, tmp_path):
    """Mallory saw the invitation and answered too. Whatever code Maren types decides: Mallory's code adds Mallory's computer
    only, and Tomas's computer is never shared with."""
    a, b, c = sync_trio
    root = _owner_project(tmp_path)
    text = M.invite(root, "tomas", PROXIES, manager=a.manager)
    for side in ("B", "C"):
        (tmp_path / side).mkdir()
    tomas = M.accept(text, tmp_path / "B", manager=b.manager)
    mallory = M.accept(text, tmp_path / "C", manager=c.manager)
    H.wait_until(lambda: len(M.answers(root, manager=a.manager)) == 2, timeout=60, what="two answers")
    M.confirm_invitee(root, M.join_code(mallory), manager=a.manager)
    assert _folder_devices(a.manager, "prj_SHARE") == {a.manager.device_id(), c.manager.device_id()}
    added = _events(root, "member.added")
    assert [e["data"]["sync_id"] for e in added if e["data"]["device"] != "dev_A"] == [c.manager.device_id()]
    with pytest.raises(M.MemberError, match="not confirmed yet"):
        M.confirm_join(tomas, manager=b.manager, wait_s=3)
    assert b.manager.api.get("/rest/config/folders") == []
    with pytest.raises(M.MemberError):
        M.confirm_invitee(root, M.join_code(tomas), manager=a.manager)  # the invitation is used up


def test_invitation_expires(sync_pair, tmp_path):
    a, b = sync_pair
    root = _owner_project(tmp_path)
    t0 = time.time()
    text = M.invite(root, "tomas", PROXIES, manager=a.manager, now=t0)
    inv = M.parse_invitation(text, now=t0 + 599, allow_loopback=True)
    assert inv["expires"] == t0 + M.INVITE_TTL_S and M.INVITE_TTL_S == 600
    (tmp_path / "B").mkdir()
    with pytest.raises(M.MemberError, match="expired"):
        M.accept(text, tmp_path / "B", manager=b.manager, now=t0 + 601)
    assert list((tmp_path / "B").iterdir()) == []                       # nothing was written
    # accepted in time, but the owner confirms too late: refused, nothing shared, the invitation is gone
    joined = M.accept(text, tmp_path / "B", manager=b.manager, now=t0 + 60)
    code = M.join_code(joined)
    H.wait_until(lambda: M.answers(root, manager=a.manager, now=t0 + 61), timeout=60, what="an answer")
    with pytest.raises(M.MemberError, match="expired"):              # says expired, so the owner's sheet stops
        M.confirm_invitee(root, code, manager=a.manager, now=t0 + 601)
    assert not list((root / ".ave" / "local" / "invitations").glob("*.json"))
    with pytest.raises(M.MemberError, match="expired"):
        M.confirm_join(joined, manager=b.manager, now=t0 + 601)
    assert b.manager.api.get("/rest/config/folders") == []
    assert not joined.root.exists()                                     # the empty folder the invitation made is cleaned up


def test_wrong_code_is_refused_and_shares_nothing(sync_pair, tmp_path):
    a, b = sync_pair
    root = _owner_project(tmp_path)
    text = M.invite(root, "tomas", PROXIES, manager=a.manager)
    (tmp_path / "B").mkdir()
    joined = M.accept(text, tmp_path / "B", manager=b.manager)
    right = M.join_code(joined)
    wrong = f"{(int(right) + 1) % 1_000_000:06d}"
    H.wait_until(lambda: M.answers(root, manager=a.manager), timeout=60, what="an answer")
    with pytest.raises(M.MemberError, match="does not match"):
        M.confirm_invitee(root, wrong, manager=a.manager)
    with pytest.raises(M.MemberError, match="six digits"):
        M.confirm_invitee(root, "12345", manager=a.manager)
    with pytest.raises(M.MemberError, match="not confirmed yet"):
        M.confirm_join(joined, manager=b.manager, wait_s=2)
    meta = json.loads((root / ".ave" / "project.json").read_text())
    assert [m["person"] for m in meta["members"]] == ["maren"]
    assert _folder_devices(a.manager, "prj_SHARE") in (None, {a.manager.device_id()})
    assert b.manager.api.get("/rest/config/folders") == []
    assert [e["data"]["device"] for e in _events(root, "member.added")] == ["dev_A"]     # only Maren himself
    # the right code still works afterwards; until Tomas confirms too, nothing reaches her
    M.confirm_invitee(root, right, manager=a.manager)
    time.sleep(3)
    assert b.manager.api.get("/rest/config/folders") == [] and not (joined.root / "edits").exists()
    M.confirm_join(joined, manager=b.manager)
    H.wait_until(lambda: (joined.root / "edits" / "rough.v1.json").is_file(), what="files after the right code")


def test_removing_a_member_stops_new_data(sync_pair, tmp_path):
    a, b = sync_pair
    root = _owner_project(tmp_path)
    rb = _join(a, b, root, tmp_path)
    H.wait_until(lambda: (rb / "edits" / "rough.v1.json").is_file(), what="first sync")
    a.manager.wait(root, "up_to_date", 60)
    said = M.remove(root, "Tomas", manager=a.manager)
    assert "keep everything they already received" in said and "Nothing new reaches" in said and "other members" in said
    assert _folder_devices(a.manager, "prj_SHARE") == {a.manager.device_id()}
    assert b.manager.device_id() not in {d["deviceID"] for d in a.manager.api.get("/rest/config/devices")}
    meta = json.loads((root / ".ave" / "project.json").read_text())
    assert [m["person"] for m in meta["members"]] == ["maren"] and "dev_B" not in meta["devices"]
    removed = _events(root, "member.removed")
    assert [(e["data"]["person"], e["data"]["devices"], e["data"]["sync_ids"]) for e in removed] == \
        [("tomas", ["dev_B"], [b.manager.device_id()])]
    (root / "edits" / "rough.v2.json").write_text("after removal\n")
    a.manager.rescan(root)
    time.sleep(8)
    assert not (rb / "edits" / "rough.v2.json").exists()                # nothing new arrives
    assert (rb / "edits" / "rough.v1.json").is_file()                   # what she had stays with her
    assert "keep everything" in M.remove(root, "tomas", manager=a.manager)    # a retry is safe and still unshares
    assert _folder_devices(a.manager, "prj_SHARE") == {a.manager.device_id()}
    with pytest.raises(M.MemberError, match="not on this project"):
        M.remove(root, "nobody", manager=a.manager)


def test_studio_project_joins_with_studio_rules(sync_pair, tmp_path):
    a, b = sync_pair
    root = _owner_project(tmp_path, "prj_STUDIO", "Grounded", kind="studio", studio=True)
    text = M.invite(root, "tomas", "full", manager=a.manager)
    (tmp_path / "B").mkdir()
    joined = M.accept(text, tmp_path / "B", manager=b.manager)
    rb = joined.root
    assert not (rb / "studio.json").exists()
    assert zones.project_rules(rb, "dev_B") == zones.rules(zones_layout("flat"), {"media": "full"}, studio=True)
    code = M.join_code(joined)
    H.wait_until(lambda: M.answers(root, manager=a.manager), timeout=60, what="an answer")
    M.confirm_invitee(root, code, manager=a.manager)
    M.confirm_join(joined, manager=b.manager)
    assert "!/posts" in (rb / ".stignore").read_text()                  # studio rules before studio.json arrived
    H.wait_until(lambda: (rb / "posts" / "p1.json").is_file() and (rb / "studio.json").is_file(), what="studio files on B")
    H.wait_until(lambda: (rb / "campaigns" / "c1.json").is_file(), what="campaigns on B")   # files arrive in any order
    time.sleep(2)
    assert not (rb / "accounts").exists()                               # publisher-only state stays with the publisher


def test_media_choice_regenerates_the_ignores(sync_pair, tmp_path):
    a, b = sync_pair
    root = _owner_project(tmp_path)
    rb = _join(a, b, root, tmp_path, media=PROXIES)
    before = (rb / ".stignore").read_text()
    assert before == zones.stignore(zones.rules(zones_layout("flat"), {"media": PROXIES}))
    M.set_media(rb, "full", manager=b.manager)
    after = (rb / ".stignore").read_text()
    assert after != before and after == zones.stignore(zones.project_rules(rb, "dev_B"))
    meta = json.loads((rb / ".ave" / "project.json").read_text())
    assert next(m for m in meta["members"] if m["person"] == "tomas")["media"] == "full"


def test_shared_tailscale_node_is_accepted():
    obj = {"Node": {"StableID": "nShared1", "User": 7, "MachineAuthorized": True, "Addresses": ["100.101.102.103/32"],
                    "Sharer": 99, "ComputedName": "tomass-macbook"},
           "UserProfile": {"ID": 7}}
    with pytest.raises(IdentityError):
        whois(obj, "100.101.102.103")                                   # the old editorial transport still refuses it
    got = whois(obj, "100.101.102.103", shared_ok=True)
    assert got["node"] == "nShared1" and got["shared"] is True and got["name"] == "tomass-macbook"
    tagged = {**obj, "Node": {**obj["Node"], "Sharer": None, "Tags": ["tag:studio"]}}
    assert whois(tagged, "100.101.102.103", shared_ok=True)["shared"] is False
    # sync never asks Tailscale who a peer is before connecting: any Tailscale address is a valid peer address
    from ave.syncthing import config as C
    assert C.listen_url("100.101.102.103") == "tcp://100.101.102.103:22417"


def test_publisher_handover_needs_a_new_approval(tmp_path):
    root = tmp_path / "proj"
    H.write_project_json(root, "prj_PUB", "The Long Table", "flat",
                         [{"person": "maren", "devices": ["dev_A"], "media": "full"},
                          {"person": "tomas", "devices": ["dev_B"], "media": "full"}], "dev_A")
    maren = Project(root, json.loads((root / ".ave" / "project.json").read_text()), Device("dev_A", short_id("dev_A")))
    old_approval = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - 3600))
    assert M.publish_blockers(root, old_approval, "dev_A") == []        # no handover yet: nothing changes
    with pytest.raises(M.MemberError, match="Only the computer that publishes"):
        M.hand_over(root, "tomas", device_id="dev_B")
    said = M.hand_over(maren, "tomas")
    assert "approves it again" in said
    meta = json.loads((root / ".ave" / "project.json").read_text())
    assert meta["publisher_device"] == "dev_B"
    changed = _events(root, "project.publisher_changed")
    assert [(e["data"]["from"], e["data"]["to"], e["device"]) for e in changed] == [("dev_A", "dev_B", "dev_A")]
    st = reduce(read_logs(root / ".ave" / "log"))
    assert [m["op"] for m in st.membership] == ["project.publisher_changed"] and not st.warnings
    # the old publisher can no longer send at all
    assert "moved to tomas's computer" in M.publish_blockers(root, old_approval, "dev_A")[0]
    # the new publisher's approvals from before the handover do not count
    assert "Approve this again here" in M.publish_blockers(root, old_approval, "dev_B")[0]
    assert M.publish_blockers(root, None, "dev_B")
    since = M.note_publisher(root, "dev_B")["since"]
    fresh = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() + 1))
    assert M.publish_blockers(root, fresh, "dev_B", approval_device="dev_B") == []
    assert M.publish_blockers(root, fresh, "dev_B")                     # an approval that does not say it was made here
    assert M.publish_blockers(root, fresh, "dev_B", approval_device="dev_A")
    assert M.note_publisher(root, "dev_B")["since"] == since            # noticed once, on this computer's clock
    with pytest.raises(M.MemberError, match="publishes this project"):
        M.remove(root, "tomas", manager=FakeManager(root, "dev_A"))


def test_the_handover_gate_holds_before_the_event_arrives(tmp_path):
    """project.json can arrive before the owner's log: the gate keys on publisher_device itself."""
    root = tmp_path / "proj"
    H.write_project_json(root, "prj_PUB", "P", "flat", [{"person": "maren", "devices": ["dev_A"], "media": "full"},
                                                     {"person": "tomas", "devices": ["dev_B"], "media": "full"}], "dev_A")
    old = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - 3600))
    assert M.publish_blockers(root, old, "dev_B", approval_device="dev_B") == []     # Tomas's computer sees Maren publish
    meta = json.loads((root / ".ave" / "project.json").read_text())
    meta["publisher_device"] = "dev_B"
    M.write_meta(root, meta)                                             # only project.json has arrived; no event yet
    assert not _events(root, "project.publisher_changed")
    assert "Approve this again here" in M.publish_blockers(root, old, "dev_B", approval_device="dev_B")[0]
    fresh = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() + 1))
    assert M.publish_blockers(root, fresh, "dev_B", approval_device="dev_B") == []
    # a computer seeing the project for the first time with a publisher other than its creator treats it as moved too
    other = tmp_path / "other"
    H.write_project_json(other, "prj_O", "O", "flat", [{"person": "maren", "devices": ["dev_A"], "media": "full"},
                                                    {"person": "tomas", "devices": ["dev_B"], "media": "full"}], "dev_B")
    m2 = json.loads((other / ".ave" / "project.json").read_text())
    m2["owner_device"] = "dev_A"
    M.write_meta(other, m2)
    assert M.publish_blockers(other, old, "dev_B", approval_device="dev_B")


def test_handover_blocks_the_publish_queue(tmp_path, monkeypatch):
    """The queue's send check carries the handover rule: an approval from before publishing moved here never sends."""
    from ave.publish import queue
    root = tmp_path / "proj"
    H.write_project_json(root, "prj_Q", "Q", "flat", [{"person": "maren", "devices": ["dev_A"], "media": "full"},
                                                    {"person": "tomas", "devices": ["dev_B"], "media": "full"}], "dev_A")
    M.hand_over(root, "tomas", device_id="dev_A")
    monkeypatch.setattr(Device, "load", classmethod(lambda cls: Device("dev_B", short_id("dev_B"))))
    ep = type("Ep", (), {"root": root})()
    old = {"approvals": [{"at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() - 60)), "device": "dev_B"}]}
    assert queue._handover_blockers(ep, old)
    new = {"approvals": [{"at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(time.time() + 1)), "device": "dev_B"}]}
    assert queue._handover_blockers(ep, new) == []
    assert queue._handover_blockers(ep, {"approvals": [{"at": new["approvals"][0]["at"]}]})    # no device: not fresh


def test_kind_is_recorded_and_read_first(tmp_path):
    p = Project.create(tmp_path / "s", "S", person="maren", kind="studio")
    assert p.meta["kind"] == "studio" and p.kind == "studio" and p.meta["owner_device"] == p.device.id
    assert not (tmp_path / "s" / "studio.json").exists()
    assert zones.project_rules(p.root, p.device.id) == zones.rules(zones_layout(p.layout), {"media": "full"}, studio=True)
    e = Project.create(tmp_path / "e", "E", person="maren")
    assert e.meta["kind"] == "episode"
    (tmp_path / "e" / "studio.json").write_text("{}")                   # kind wins over what the folder holds
    assert zones.project_rules(e.root, e.device.id) == zones.rules(zones_layout(e.layout), {"media": "full"}, studio=False)
    with pytest.raises(Exception):
        Project.create(tmp_path / "x", "X", kind="album")


def test_a_damaged_or_foreign_invitation_is_refused():
    for text in ("hello", M.PREFIX + "!!!", M.PREFIX + "e30"):
        with pytest.raises(M.MemberError):
            M.parse_invitation(text)


def zones_layout(spec):
    from ave.episode import resolve_layout
    return resolve_layout(spec)


def test_native_sharing_requests_name_a_project_only():
    """The web view can only say which project; the person, media, invitation, folder and code come from native sheets."""
    from ave import desktop_native as N
    origin = "http://127.0.0.1:5000/"
    for action in ("shareProject", "removeMember", "handOverPublishing"):
        rid, act, args = N.native_message({"id": "1", "action": action, "args": {"project": "prj_ABC"}}, origin, origin)
        assert N.sharing_args(act, args) == "prj_ABC"
        for bad in ({}, {"project": "../etc"}, {"project": "prj_ABC", "person": "tomas"}, {"project": 7}):
            with pytest.raises(ValueError):
                N.sharing_args(action, bad)
    assert N.sharing_args("joinProject", {}) is None
    with pytest.raises(ValueError):
        N.sharing_args("joinProject", {"text": "AVE-INVITE-1.x"})
    for name in ("shareProject:", "joinProject:", "removeMember:", "handOverPublishing:"):
        assert name in N.BRIDGE_SCRIPT
    assert "keeps everything already received" in N.removal_words("Tomas", "The Long Table")
    assert "other members" in N.removal_words("Tomas", "The Long Table")
    assert "approves it again" in N.handover_words("Tomas", "The Long Table")
    assert "10 minutes" in N.share_words("Tomas", "The Long Table", "proxies")


def test_a_project_json_change_from_another_member_regenerates_the_ignores(sync_pair, tmp_path):
    a, b = sync_pair
    root = _owner_project(tmp_path)
    rb = _join(a, b, root, tmp_path)
    a.manager.wait(root, "up_to_date", 60)
    meta = json.loads((root / ".ave" / "project.json").read_text())
    meta["layout"] = {"preset": "flat", "raw_dirs": ["inbox", "cards"]}
    M.write_meta(root, meta)
    a.manager.rescan(root)
    want = zones.stignore(zones.rules(zones_layout(meta["layout"]), {"media": PROXIES}))
    H.wait_until(lambda: (rb / ".stignore").read_text() == want, timeout=60, what="B to follow the new layout")
    assert "cards" in want


# ---------------------------------------------------------------------------------------------------- fix round 1
class FakeManager:
    """Enough of SyncManager for the membership rules, without Syncthing. `share` records what it was asked."""
    allow_loopback = True

    def __init__(self, root, ave_device, sync_id=None, folder=None, configs=None, pending=None):
        self.root, self.ave_device = root, ave_device
        self.sid = sync_id or SID[ave_device[-1]]
        self.state = "running"
        self.pairing: set[str] = set()
        self.folder = folder                     # list of sync ids the folder is shared with now, or None
        self.configs = configs or {}
        self.pending = pending or {}
        self.shared: list[list[str]] = []
        self.fail_share = 0
        self.dismissed: list[str] = []
        self.api = self

    def device_id(self):
        return self.sid

    def listen_address(self):
        return "tcp://127.0.0.1:4000"

    def _folders(self):
        if self.folder is None:
            return []
        pid = json.loads((self.root / ".ave" / "project.json").read_text())["project_id"]
        return [{"id": pid, "path": str(self.root), "devices": [{"deviceID": d} for d in [self.sid, *self.folder]]}]

    def get(self, path, **_):
        return self.configs[path.rsplit("/", 1)[1]]

    def pending_devices(self):
        return self.pending

    def dismiss_pending(self, sid):
        self.dismissed.append(sid)

    def share(self, root, peers=None, *, authorized=False):
        if self.fail_share:
            self.fail_share -= 1
            raise M.MemberError("Syncthing did not answer")
        peers = M.share_list(root, self) if peers is None else peers
        self.shared.append(sorted(p.sync_id for p in peers))
        self.folder = self.shared[-1]


def _plain_project(base, members, publisher="dev_A", name="P"):
    root = base / name
    H.write_project_json(root, "prj_UNIT", "Unit", "flat", members, publisher)
    return root


def _log_as(root, device, op, data):
    meta = json.loads((root / ".ave" / "project.json").read_text())
    return Project(root, meta, Device(device, short_id(device))).log.append(op, data=data, actor="human")


def _added(device, person, letter):
    return {"person": person, "device": device, "media": "full", "sync_id": SID[letter], "address": "tcp://127.0.0.1:50%02d" % ord(letter)}


def _answer(tag, device, letter, t):
    return SID[letter], {"name": f"{M.JOIN_NAME}:{tag}:{device}:tcp://127.0.0.1:60{ord(letter)}", "time": t}


def test_membership_comes_only_from_the_owners_log(tmp_path):
    maren = {"person": "maren", "devices": ["dev_A"], "media": "full"}
    tomas = {"person": "tomas", "devices": ["dev_B"], "media": "full"}
    cara = {"person": "cara", "devices": ["dev_C"], "media": "full"}
    root = _plain_project(tmp_path, [maren, tomas, cara])
    meta = json.loads((root / ".ave" / "project.json").read_text())
    meta["devices"] = {}
    meta["owner_device"] = "dev_A"
    M.write_meta(root, meta)
    _log_as(root, "dev_A", "member.added", _added("dev_A", "maren", "A"))
    _log_as(root, "dev_A", "member.added", _added("dev_B", "tomas", "B"))
    _log_as(root, "dev_A", "member.added", _added("dev_C", "cara", "C"))
    _log_as(root, "dev_A", "member.removed", {"person": "tomas", "devices": ["dev_B"], "sync_ids": [SID["B"]]})
    # Tomas re-adds herself and a stranger; her events are not the owner's, so they count for nothing
    _log_as(root, "dev_B", "member.added", _added("dev_B", "tomas", "B"))
    _log_as(root, "dev_B", "member.added", _added("dev_X", "stranger", "X"))
    # a stale or concurrent project.json still lists Tomas and the stranger
    meta["members"].append({"person": "stranger", "devices": ["dev_X"], "media": "full"})
    meta["devices"] = {"dev_B": {"sync_id": SID["B"], "address": "tcp://127.0.0.1:5066"},
                       "dev_X": {"sync_id": SID["X"], "address": "tcp://127.0.0.1:5088"}}
    M.write_meta(root, meta)
    cara_mgr = FakeManager(root, "dev_C", folder=[SID["A"], SID["B"]])
    assert [p.sync_id for p in M.share_list(root, cara_mgr)] == [SID["A"]]
    assert M.is_removed(root, FakeManager(root, "dev_B"))
    assert not M.is_removed(root, cara_mgr)
    # a non-owner cannot invite, confirm or remove
    with pytest.raises(M.MemberError, match="owns this project"):
        M.invite(root, "mallory", "full", manager=cara_mgr)
    with pytest.raises(M.MemberError, match="owns this project"):
        M.remove(root, "maren", manager=cara_mgr)
    # the owner adding Tomas again after the removal does count
    _log_as(root, "dev_A", "member.added", _added("dev_B", "tomas", "B"))
    assert [p.sync_id for p in M.share_list(root, cara_mgr)] == [SID["A"], SID["B"]]


def test_answers_that_claim_a_known_device_are_refused(tmp_path):
    root = _plain_project(tmp_path, [{"person": "maren", "devices": ["dev_A"], "media": "full"},
                                     {"person": "cara", "devices": ["dev_C"], "media": "full"}])
    mgr = FakeManager(root, "dev_A", folder=[])
    _log_as(root, "dev_A", "member.added", _added("dev_D", "dora", "D"))
    _log_as(root, "dev_A", "member.removed", {"person": "dora", "devices": ["dev_D"], "sync_ids": [SID["D"]]})
    text = M.invite(root, "tomas", PROXIES, manager=mgr)
    tag = M._tag(M.parse_invitation(text, allow_loopback=True)["nonce"])
    mgr.pending = dict([_answer(tag, "dev_C", "E", "1"), _answer(tag, "dev_D", "F", "2"), _answer(tag, "dev_J", "J", "3")])
    got = {a.device: a for a in M.answers(root, manager=mgr)}
    assert "already on this project" in got["dev_C"].problem             # claims Cara's device
    assert "another identity" in got["dev_D"].problem                   # Dora's device, but a different Syncthing id
    assert not got["dev_J"].problem
    nonce = got["dev_C"].nonce
    with pytest.raises(M.MemberError, match="already on this project"):
        M.confirm_invitee(root, M.comparison_code(SID["A"], SID["E"], nonce), manager=mgr)
    assert mgr.shared == [] and [m["person"] for m in M.members(root)] == ["maren", "cara"]


def test_at_most_five_answers_and_a_shared_code_is_refused(tmp_path, monkeypatch):
    root = _plain_project(tmp_path, [{"person": "maren", "devices": ["dev_A"], "media": "full"}])
    mgr = FakeManager(root, "dev_A", folder=[])
    text = M.invite(root, "tomas", PROXIES, manager=mgr)
    tag = M._tag(M.parse_invitation(text, allow_loopback=True)["nonce"])
    mgr.pending = dict(_answer(tag, f"dev_{k}", k, str(i)) for i, k in enumerate("BCDEFGH"))
    got = M.answers(root, manager=mgr)
    assert [a.device for a in got] == ["dev_B", "dev_C", "dev_D", "dev_E", "dev_F"]     # the earliest five
    monkeypatch.setattr(M, "comparison_code", lambda *a: "111111")
    with pytest.raises(M.MemberError, match="More than one computer"):
        M.confirm_invitee(root, "111111", manager=mgr)
    assert mgr.shared == [] and not list((root / ".ave" / "local" / "invitations").glob("*.json"))


def test_a_legacy_project_keeps_its_members_when_inviting_and_removing(tmp_path):
    """A project shared before membership was logged has no `devices`: who the folder is shared with now is kept."""
    root = _plain_project(tmp_path, [{"person": "maren", "devices": ["dev_A"], "media": "full"},
                                     {"person": "tomas", "devices": ["dev_B"], "media": "full"},
                                     {"person": "cara", "devices": ["dev_C"], "media": "full"}])
    cfg = {SID["B"]: {"name": "tomas", "addresses": ["tcp://127.0.0.1:5001"]},
           SID["C"]: {"name": "cara", "addresses": ["tcp://127.0.0.1:5002"]}}
    mgr = FakeManager(root, "dev_A", folder=[SID["B"], SID["C"]], configs=cfg)
    assert [p.sync_id for p in M.share_list(root, mgr)] == [SID["B"], SID["C"]]
    M.invite(root, "dora", PROXIES, manager=mgr)                         # logs who is already on it
    assert "devices" in json.loads((root / ".ave" / "project.json").read_text())
    assert [p.sync_id for p in M.share_list(root, mgr)] == [SID["B"], SID["C"]]
    assert {e["data"]["person"] for e in _events(root, "member.added")} == {"maren", "tomas", "cara"}
    M.remove(root, "tomas", manager=mgr)
    assert mgr.shared[-1] == [SID["C"]]                                  # Cara stays; only Tomas goes


def test_remove_and_confirm_share_first_and_are_safe_to_retry(tmp_path):
    root = _plain_project(tmp_path, [{"person": "maren", "devices": ["dev_A"], "media": "full"}])
    mgr = FakeManager(root, "dev_A", folder=[])
    text = M.invite(root, "tomas", PROXIES, manager=mgr)
    nonce = M.parse_invitation(text, allow_loopback=True)["nonce"]
    mgr.pending = dict([_answer(M._tag(nonce), "dev_B", "B", "1")])
    code = M.comparison_code(SID["A"], SID["B"], nonce)
    mgr.state = "starting"
    with pytest.raises(M.MemberError, match="not running"):
        M.confirm_invitee(root, code, manager=mgr)
    mgr.state, mgr.fail_share = "running", 1
    with pytest.raises(M.MemberError, match="did not answer"):
        M.confirm_invitee(root, code, manager=mgr)                       # nothing recorded; the invitation still stands
    assert [m["person"] for m in M.members(root)] == ["maren"] and SID["B"] not in M.membership(root).members
    M.confirm_invitee(root, code, manager=mgr)
    assert mgr.shared[-1] == [SID["B"]] and SID["B"] in M.membership(root).members
    mgr.fail_share = 1
    with pytest.raises(M.MemberError, match="did not answer"):
        M.remove(root, "tomas", manager=mgr)                             # still listed and still a member: retry works
    assert SID["B"] in M.membership(root).members and [m["person"] for m in M.members(root)] == ["maren", "tomas"]
    assert "keep everything" in M.remove(root, "tomas", manager=mgr)
    assert mgr.shared[-1] == [] and SID["B"] in M.membership(root).removed_syncs
    assert "keep everything" in M.remove(root, "tomas", manager=mgr)    # a second retry only makes sure
    assert mgr.shared[-1] == []


def test_a_third_member_follows_the_removal_while_the_owner_is_offline(sync_trio, tmp_path):
    a, b, c = sync_trio
    root = _owner_project(tmp_path)
    rb = _join(a, b, root, tmp_path)
    text = M.invite(root, "cara", "full", manager=a.manager)
    (tmp_path / "C").mkdir()
    cj = M.accept(text, tmp_path / "C", manager=c.manager)
    H.wait_until(lambda: M.answers(root, manager=a.manager), timeout=60, what="Cara to answer")
    M.confirm_invitee(root, M.join_code(cj), manager=a.manager)
    M.confirm_join(cj, manager=c.manager)
    rc = cj.root
    # Tomas and Cara learn of each other from Maren's log and share directly
    H.wait_until(lambda: c.manager.device_id() in (_folder_devices(b.manager, "prj_SHARE") or set())
                 and b.manager.device_id() in (_folder_devices(c.manager, "prj_SHARE") or set()), timeout=90,
                 what="Tomas and Cara to share with each other")
    M.remove(root, "tomas", manager=a.manager)
    H.wait_until(lambda: any(e["op"] == "member.removed" for e in read_logs(rc / ".ave" / "log")), timeout=60,
                 what="Maren's removal to reach Cara")
    a.manager.stop()                                                    # Maren goes offline
    H.wait_until(lambda: b.manager.device_id() not in (_folder_devices(c.manager, "prj_SHARE") or set()), timeout=60,
                 what="Cara to stop sharing with Tomas")
    # a stale or concurrent project.json that still lists Tomas does not bring her back
    meta = json.loads((rc / ".ave" / "project.json").read_text())
    if not any(m["person"] == "tomas" for m in meta["members"]):
        meta["members"].append({"person": "tomas", "devices": ["dev_B"], "media": "proxies"})
    next(m for m in meta["members"] if m["person"] == "cara")["media"] = PROXIES
    M.write_meta(rc, meta)
    c.manager.refresh(rc)
    assert b.manager.device_id() not in _folder_devices(c.manager, "prj_SHARE")
    (rc / "edits" / "rough.v3.json").write_text("from cara, after the removal\n")
    c.manager.rescan(rc)
    time.sleep(8)
    assert not (rb / "edits" / "rough.v3.json").exists()


def test_removing_from_a_legacy_project_before_any_invite(tmp_path):
    """No `devices` and nothing logged yet: removing Tomas must still stop sharing with her real Syncthing id."""
    root = _plain_project(tmp_path, [{"person": "maren", "devices": ["dev_A"], "media": "full"},
                                     {"person": "tomas", "devices": ["dev_B"], "media": "full"},
                                     {"person": "cara", "devices": ["dev_C"], "media": "full"}])
    cfg = {SID["B"]: {"name": "tomas", "addresses": ["tcp://127.0.0.1:5001"]},
           SID["C"]: {"name": "cara", "addresses": ["tcp://127.0.0.1:5002"]}}
    mgr = FakeManager(root, "dev_A", folder=[SID["B"], SID["C"]], configs=cfg)
    assert "keep everything" in M.remove(root, "tomas", manager=mgr)
    assert mgr.shared[-1] == [SID["C"]]
    removed = _events(root, "member.removed")
    assert [e["data"]["sync_ids"] for e in removed] == [[SID["B"]]]
    assert [p.sync_id for p in M.share_list(root, mgr)] == [SID["C"]]     # a later refresh does not bring her back
