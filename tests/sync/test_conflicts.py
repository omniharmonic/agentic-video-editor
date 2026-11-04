"""When two people disagree on a synced project, both sides are kept: settings merge or ask a person, conflict copies
anywhere else raise an alert, stage conflicts are settled on the board or in chat, and presence shows who is where."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ave.agent import AgentApp
from ave.project import Log, Project
from ave.project import presence
from ave.project.reducer import reduce
from ave.studio.ui.workspace import Workspace
from ave.syncthing import conflicts
from ave.syncthing.members import add_member, canonical

pytestmark = pytest.mark.timeout(60)

BEN_SYNC = "-".join(["AAAAAAA"] * 8)
TOMAS_SYNC = "-".join(["BBBBBBB"] * 8)
COPY = "sync-conflict-20261102-101500-BBBBBBB"          # a conflict copy Tomas's computer made


@pytest.fixture
def project(tmp_path):
    """Maren's episode project, shared with Tomas (dev_B): an episode.json and a home artifact."""
    root = tmp_path / "ep12"
    root.mkdir()
    (root / "episode.json").write_text(json.dumps({"title": "Episode 12", "layout": "flat", "tz": "UTC",
                                                   "show": {"host": "Maren", "season": 2}}, indent=2) + "\n")
    p = Project.create(root, "Episode 12", person="maren")
    meta = add_member(p.meta, "maren", p.device.id, "full", BEN_SYNC, "100.64.0.1")
    meta = add_member(meta, "tomas", "dev_B", "proxies", TOMAS_SYNC, "100.64.0.2")
    (root / ".ave" / "project.json").write_bytes(canonical(meta))
    p = Project.open(root)
    p.log.append("artifact.created", artifact="art_ep", data={"kind": "episode", "title": "Episode 12", "stage": "Plan"})
    presence.forget()
    return p


def _tomas(project: Project, wall=None) -> Log:
    kw = {"wall": wall} if wall else {}
    return Log(project.log_dir, "dev_B", "tomas", lock_dir=project.local_dir / "locks", **kw)


def _ep(project) -> dict:
    return json.loads((project.root / "episode.json").read_text())


def _write(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, indent=2) + "\n")


def _last(project) -> dict:
    from ave.project import hlc
    return max(project.read_events(), key=lambda e: (hlc.parse(e["hlc"]), e["id"]))


def _ws(project, clock=None) -> Workspace:
    return Workspace(None, roots=lambda: [project.root], clock=clock)


LOSER = "sync-conflict-20261102-101500-AAAAAAA"         # Maren's own version, renamed by Syncthing on his computer


class FakeSync:
    """What the watcher asks a running SyncManager: who last changed a file (Syncthing's modifiedBy)."""

    def __init__(self):
        self.mod = {}

    def modified_short(self, root, rel):
        return self.mod.get(rel)

    def refresh(self, root):
        pass


def _empty() -> dict:
    return {"asked": [], "settled": [], "waiting": [], "alerts": [], "retry": [], "errors": []}


def _decision(project, did):
    return Project.open(project.root).state().decisions[did]


def _home_row(ws):
    return next(r for p in ws.board()["projects"] for r in p["artifacts"] if r["artifact"] == "art_ep")


def test_unrelated_setting_changes_merge(project):
    """Unrelated changes are not merged on a guess: the person sees each setting side by side and picks, and exactly
    what was picked is written."""
    root = project.root
    assert conflicts.check(root).to_dict() == _empty()
    base = _ep(project)
    maren = {**base, "title": "Episode 12: the van"}
    tomas = {k: v for k, v in {**base, "tz": "America/Denver", "show": {**base["show"], "season": 3},
                               "draft": True}.items() if k != "layout"}
    _write(root / "episode.json", maren)
    copy = root / f"episode.{COPY}.json"
    _write(copy, tomas)

    rep = conflicts.check(root)
    (asked,) = rep.asked
    assert _ep(project) == maren and json.loads(copy.read_text()) == tomas           # nothing applied, nothing deleted
    d = _decision(project, asked["decision"])
    assert d["data"]["summary"] == "Maren and Tomas both changed episode.json (Title, Layout, Time zone, Show and more)"
    assert d["data"]["options"] == ["Use all of Maren's", "Use all of Tomas's"]
    assert d["data"]["keys"] == [
        {"key": "title", "label": "Title", "file": "“Episode 12: the van”", "copy": "“Episode 12”"},
        {"key": "layout", "label": "Layout", "file": "“flat”", "copy": "(not set)"},
        {"key": "tz", "label": "Time zone", "file": "“UTC”", "copy": "“America/Denver”"},
        {"key": "show", "label": "Show", "file": '{"host": "Maren", "season": 2}', "copy": '{"host": "Maren", "season": 3}'},
        {"key": "draft", "label": "Draft", "file": "(not set)", "copy": "true"}]
    assert conflicts.check(root).asked[0]["decision"] == d["id"]                   # asking twice records one decision
    assert len(Project.open(root).state().decisions) == 1

    ws = _ws(project)
    (dec,) = _home_row(ws)["decisions"]
    assert dec["kind"] == "setting" and dec["file_who"] == "Maren" and dec["copy_who"] == "Tomas" and len(dec["keys"]) == 5
    with pytest.raises(Exception, match="every setting"):
        ws.patch(_home_row(ws)["key"], {"resolve": d["id"], "choice": conflicts.PER_KEY, "picks": {"tz": "copy"}})
    picks = {"title": "file", "layout": "file", "tz": "copy", "show": "copy", "draft": "file"}
    ws.patch(_home_row(ws)["key"], {"resolve": d["id"], "choice": conflicts.PER_KEY, "picks": picks})
    assert conflicts.drain()
    assert _ep(project) == {**maren, "tz": "America/Denver", "show": {"host": "Maren", "season": 3}}   # exactly what was picked
    assert not copy.exists()
    note = next(n for n in Project.open(root).state().notes.values() if (n["data"] or {}).get("kind") == "settings_merged")
    assert note["data"]["text"] == "Settled the two copies of episode.json as chosen: Show, Time zone from the other copy."

    # project.json: membership is never offered and always keeps the file's own values
    pj = root / ".ave" / "project.json"
    mine = json.loads(pj.read_text())
    other = {**mine, "title": "Episode 12 (shared)", "publisher_device": "dev_E",
             "members": [m for m in mine["members"] if m["person"] != "tomas"] + [{"person": "eve", "devices": ["dev_E"], "media": "full"}]}
    (root / ".ave" / f"project.{COPY}.json").write_bytes(canonical(other))
    (asked,) = conflicts.check(root).asked
    d = _decision(project, asked["decision"])
    assert [k["key"] for k in d["data"]["keys"]] == ["title"]
    AgentApp.for_board(root).board("decision_resolve", {"request_id": "pj", "decision": d["id"], "choice": "Use all of Tomas's",
                                                       "quote": "take Tomas's"})
    assert conflicts.drain()
    now = json.loads(pj.read_text())
    assert now["title"] == "Episode 12 (shared)" and now["members"] == mine["members"]
    assert now["publisher_device"] == mine["publisher_device"] and pj.read_bytes() == canonical(now)


def test_same_setting_asks_a_person(project):
    root = project.root
    base = _ep(project)
    _write(root / "episode.json", {**base, "title": "The van"})
    copy = root / f"episode.{COPY}.json"
    _write(copy, {**base, "title": "Van stories"})
    (asked,) = conflicts.check(root).asked
    d = _decision(project, asked["decision"])
    assert d["data"]["summary"] == "Maren and Tomas both changed episode.json (Title)"
    assert d["data"]["keys"] == [{"key": "title", "label": "Title", "file": "“The van”", "copy": "“Van stories”"}]

    ws = _ws(project)
    ws.patch(_home_row(ws)["key"], {"resolve": d["id"], "choice": "Use all of Tomas's"})       # on the board
    assert conflicts.drain()
    assert _ep(project)["title"] == "Van stories" and not copy.exists()
    assert not _home_row(ws)["decisions"]

    # in chat: Claude answers with the person's words, setting by setting
    base = _ep(project)
    _write(root / "episode.json", {**base, "tz": "Asia/Tokyo"})
    copy = root / f"episode.{COPY.replace('1015', '1016')}.json"
    _write(copy, {**base, "tz": "America/Denver"})
    (asked,) = conflicts.check(root).asked
    app = AgentApp.for_board(root)
    with pytest.raises(ValueError, match="one of the options"):
        app.board("decision_resolve", {"request_id": "r1", "decision": asked["decision"], "choice": "Denver", "quote": "Tomas's"})
    with pytest.raises(ValueError, match="every key"):
        app.board("decision_resolve", {"request_id": "r2", "decision": asked["decision"], "choice": conflicts.PER_KEY,
                                       "quote": "Tomas's time zone", "picks": {}})
    out = app.board("decision_resolve", {"request_id": "r3", "decision": asked["decision"], "choice": conflicts.PER_KEY,
                                         "quote": "Tomas's time zone", "picks": {"tz": "copy"}})
    assert out["status"] == "resolved"
    assert conflicts.drain()
    assert _ep(project)["tz"] == "America/Denver" and conflicts.find(root) == []


@pytest.mark.parametrize("ben_edit,tomas_edit", [
    (lambda b: {**b, "title": "The van", "tz": "Europe/Paris"}, lambda b: {**b, "title": "Van stories"}),
    (lambda b: {**b, "title": "The van"}, lambda b: {k: v for k, v in b.items() if k != "tz"}),
], ids=["same-key", "removal"])
def test_with_sync_running_the_losers_computer_asks_and_changes_nothing(project, ben_edit, tomas_edit):
    """break1: the real Syncthing order with a manager. Maren's version loses: Syncthing renames it to the copy under his own
    id and writes Tomas's pulled version as the file. The pass that follows asks; it changes and deletes nothing."""
    root, m, ep = project.root, FakeSync(), project.root / "episode.json"
    m.mod["episode.json"] = "BBBBBBB"
    conflicts.check(root, manager=m, items=["episode.json"])
    base = _ep(project)
    maren = ben_edit(dict(base))
    _write(ep, maren)
    m.mod["episode.json"] = "AAAAAAA"
    conflicts.check(root, manager=m)
    jos = tomas_edit(dict(base))
    copy = root / f"episode.{LOSER}.json"
    ep.rename(copy)
    _write(ep, jos)
    m.mod["episode.json"] = "BBBBBBB"
    rep = conflicts.check(root, manager=m, items=["episode.json"])
    assert len(rep.asked) == 1 and rep.settled == []
    assert _ep(project) == jos and json.loads(copy.read_text()) == maren
    d = _decision(project, rep.asked[0]["decision"])
    assert (d["data"]["file_who"], d["data"]["copy_who"]) == ("Tomas", "Maren")       # who last wrote each copy
    assert conflicts.check(root, manager=m).asked[0]["decision"] == d["id"]          # and a later pass still only asks
    assert _ep(project) == jos and copy.exists() and board_open(d["id"], project)


def board_open(did, project) -> bool:
    from ave.project import board
    return board.decision_open(Project.open(project.root).state().decisions[did])


def test_a_revert_is_asked_about_too(project):
    """break2: a version both had before (a revert) is no reason to take either side."""
    root, m, ep = project.root, FakeSync(), project.root / "episode.json"
    v0 = _ep(project)
    m.mod["episode.json"] = "BBBBBBB"
    for v in (v0, {**v0, "draft": True}, v0):
        _write(ep, v)
        conflicts.check(root, manager=m, items=["episode.json"])
    v1 = {**v0, "draft": True}
    _write(ep, v1)
    m.mod["episode.json"] = "AAAAAAA"
    conflicts.check(root, manager=m)
    jos = {**v0, "title": "Van stories"}
    copy = root / f"episode.{COPY}.json"
    _write(copy, jos)
    rep = conflicts.check(root, manager=m, items=[copy.name])
    assert len(rep.asked) == 1 and _ep(project) == v1 and json.loads(copy.read_text()) == jos


def test_an_open_question_is_never_closed_or_taken_while_the_copy_is_there(project):
    """break3: the sweep sees the conflict first, then any number of later passes: still one open question, nothing
    written, nothing removed."""
    root, m, ep = project.root, FakeSync(), project.root / "episode.json"
    m.mod["episode.json"] = "BBBBBBB"
    conflicts.check(root, manager=m, items=["episode.json"])
    base = _ep(project)
    maren = {**base, "title": "The van", "draft": True}
    _write(ep, maren)
    m.mod["episode.json"] = "AAAAAAA"
    conflicts.check(root, manager=m)
    jos = {**base, "title": "Van stories", "tz": "America/Denver"}
    copy = root / f"episode.{LOSER}.json"
    ep.rename(copy)
    _write(ep, jos)
    m.mod["episode.json"] = "BBBBBBB"
    first = conflicts.check(root, manager=m).asked
    for _ in range(3):
        assert conflicts.check(root, manager=m).asked == first
    assert _ep(project) == jos and json.loads(copy.read_text()) == maren
    st = Project.open(root).state()
    assert [d["status"] for d in st.decisions.values()] == ["open"] and not st.notes


def test_a_file_that_changes_during_the_pass_is_never_overwritten(project, monkeypatch):
    root = project.root
    base = _ep(project)
    _write(root / "episode.json", {**base, "title": "The van"})
    copy = root / f"episode.{COPY}.json"
    _write(copy, {**base, "tz": "America/Denver"})
    (asked,) = conflicts.check(root).asked
    _tomas(project).append("decision.resolved", artifact="art_ep",
                           data={"decision": asked["decision"], "choice": "Use all of Tomas's"})
    meanwhile = {**base, "title": "The van, again"}                    # an editor saves just before the replace

    def save(path):
        if path.name == "episode.json":
            _write(path, meanwhile)
            monkeypatch.setattr(conflicts, "_before_replace", None)
    monkeypatch.setattr(conflicts, "_before_replace", save)
    rep = conflicts.check(root)
    assert rep.retry == [f"episode.{COPY}.json"] and rep.settled == []
    assert _ep(project) == meanwhile and copy.exists()                   # the save stands, the copy waits

    rep = conflicts.check(root)                                         # the file changed after the answer: ask again
    assert _ep(project) == meanwhile and copy.exists()
    (again,) = rep.asked
    assert again["decision"] != asked["decision"]
    assert _decision(project, again["decision"])["data"]["reason"].startswith("The file changed after the question was answered")


def test_synced_copies_are_applied_by_the_owners_computer(tmp_path):
    """A copy every member has (studio posts/) may be asked and answered on any computer; only the owner's computer (as
    pinned in .ave/local/owner.json) applies it, and the others say they are waiting for it."""
    root = tmp_path / "studio"
    root.mkdir()
    (root / "studio.json").write_text(json.dumps({"title": "The Long Table"}) + "\n")
    p = Project.create(root, "The Long Table", person="maren", kind="studio", layout="flat")
    meta = add_member(add_member(p.meta, "maren", p.device.id, "full", BEN_SYNC, "a"), "tomas", "dev_B", "full", TOMAS_SYNC, "b")
    (root / ".ave" / "project.json").write_bytes(canonical(meta))
    p = Project.open(root)
    p.log.append("artifact.created", artifact="art_studio", data={"kind": "studio", "title": "The Long Table"})
    (root / "posts").mkdir()
    post = root / "posts" / "launch.json"
    _write(post, {"id": "launch", "text": "Out now"})
    copy = root / "posts" / f"launch.{COPY}.json"
    _write(copy, {"id": "launch", "text": "Out today"})
    pin = root / ".ave" / "local" / "owner.json"
    pin.parent.mkdir(parents=True, exist_ok=True)

    pin.write_text(json.dumps({"device": "dev_B", "person": "tomas"}))        # this computer is not the owner
    (asked,) = conflicts.check(root).asked                                   # it may still ask
    p.log.append("decision.resolved", artifact="art_studio", data={"decision": asked["decision"], "choice": "Use all of Tomas's"})
    rep = conflicts.check(root, items=[])
    assert rep.waiting == [{"file": "posts/launch.json", "conflict": f"posts/launch.{COPY}.json", "decision": asked["decision"],
                            "owner": "Tomas", "text": "The two copies of launch.json are answered. Waiting for Tomas's "
                                                      "computer to apply this."}]
    assert json.loads(post.read_text())["text"] == "Out now" and copy.exists()
    ws = Workspace(None, roots=lambda: [root])
    assert ws.board()["projects"][0]["notices"][0]["text"].endswith("Waiting for Tomas's computer to apply this.")
    assert AgentApp.for_board(root).board("artifact_list", {})["waiting_for_owner"][0]["file"] == "posts/launch.json"

    pin.write_text(json.dumps({"device": p.device.id}))                      # the owner's computer applies the answer
    rep = conflicts.check(root, items=[".ave/log/dev_B.jsonl"])
    assert rep.settled and json.loads(post.read_text())["text"] == "Out today" and not copy.exists()
    assert conflicts.waiting(root) == []


def _studio(root: Path, owner_device: str) -> Project:
    root.mkdir()
    (root / "studio.json").write_text(json.dumps({"title": "The Long Table"}) + "\n")
    p = Project.create(root, "The Long Table", person="maren", kind="studio", layout="flat")
    meta = add_member(add_member(p.meta, "maren", p.device.id, "full", BEN_SYNC, "a"), "tomas", "dev_B", "full", TOMAS_SYNC, "b")
    (root / ".ave" / "project.json").write_bytes(canonical(meta))
    p = Project.open(root)
    p.log.append("artifact.created", artifact="art_studio", data={"kind": "studio", "title": "The Long Table"})
    (root / "posts").mkdir()
    _write(root / "posts" / "launch.json", {"id": "launch", "text": "Out now"})
    _write(root / "posts" / f"launch.{COPY}.json", {"id": "launch", "text": "Out today"})
    (root / ".ave" / "local").mkdir(exist_ok=True)
    (root / ".ave" / "local" / "owner.json").write_text(json.dumps({"device": owner_device}))
    return p


def test_the_owner_applies_an_answer_it_never_asked(tmp_path):
    """Tomas's computer (not the owner) asks and the answer is given there; the owner's computer, whose own record knows
    nothing about it, applies it on the pass after the log arrives, without waiting for the sweep."""
    import shutil
    tomass = _studio(tmp_path / "tomas", "dev_OWNER")
    (asked,) = conflicts.check(tomass.root).asked
    tomass.log.append("decision.resolved", artifact="art_studio", data={"decision": asked["decision"], "choice": "Use all of Tomas's"})
    assert conflicts.check(tomass.root, items=[]).waiting

    owners = tmp_path / "owner"
    shutil.copytree(tomass.root, owners, ignore=shutil.ignore_patterns("local"))      # what sync brings: no local state
    (owners / ".ave" / "local").mkdir()
    (owners / ".ave" / "local" / "owner.json").write_text(json.dumps({"device": tomass.device.id}))
    rep = conflicts.check(owners, items=[".ave/log/dev_B.jsonl"])
    assert rep.settled and json.loads((owners / "posts" / "launch.json").read_text())["text"] == "Out today"
    assert not (owners / "posts" / f"launch.{COPY}.json").exists()


def test_an_answer_that_no_longer_fits_is_asked_again_and_says_so(project):
    root = project.root
    base = _ep(project)
    _write(root / "episode.json", {**base, "title": "The van"})
    copy = root / f"episode.{COPY}.json"
    _write(copy, {**base, "title": "Van stories"})
    (asked,) = conflicts.check(root).asked
    _tomas(project).append("decision.resolved", artifact="art_ep", data={"decision": asked["decision"], "choice": "Use all of Someone's"})
    (again,) = conflicts.check(root).asked
    d = _decision(project, again["decision"])
    assert d["data"]["supersedes"] == asked["decision"] and d["status"] == "open"
    assert d["data"]["reason"].startswith("That answer no longer matches the two files, so nothing was written. Choose again.")
    assert _ep(project)["title"] == "The van" and copy.exists()
    assert conflicts.check(root).asked[0]["decision"] == d["id"]                    # and it stays the one open question


def test_a_broken_project_json_copy_can_only_be_set_aside(project):
    """r2pj: a copy that is not a project.json object is never written; the only answer keeps the file."""
    root = project.root
    pj = root / ".ave" / "project.json"
    before = pj.read_bytes()
    copy = root / ".ave" / f"project.{COPY}.json"
    copy.write_text(json.dumps([{"members": [], "owner_device": "dev_B", "title": "Hijack"}]))
    (asked,) = conflicts.check(root, items=[copy.relative_to(root).as_posix()]).asked
    d = _decision(project, asked["decision"])
    assert d["data"]["whole"] and d["data"]["options"] == ["Keep this file"]
    assert d["data"]["texts"]["copy"].startswith('[{"members"') and '"project_id"' in d["data"]["texts"]["file"]
    _tomas(project).append("decision.resolved", artifact="art_ep", data={"decision": d["id"], "choice": "Use the other copy"})
    conflicts.check(root, items=[])
    assert pj.read_bytes() == before and copy.exists()                              # a forged answer writes nothing
    (now,) = conflicts.check(root, items=[]).asked
    ws = _ws(project)
    ws.patch(_home_row(ws)["key"], {"resolve": now["decision"], "choice": "Keep this file"})
    assert conflicts.drain()
    assert pj.read_bytes() == before and not copy.exists()


def test_one_and_true_are_different_settings(project):
    root = project.root
    _write(root / "episode.json", {**_ep(project), "draft": True})
    copy = root / f"episode.{COPY}.json"
    _write(copy, {**_ep(project), "draft": 1})
    (asked,) = conflicts.check(root).asked
    assert [k["key"] for k in _decision(project, asked["decision"])["data"]["keys"]] == ["draft"] and copy.exists()


def test_a_file_missing_at_question_time_is_checked_again_before_writing(project):
    root = project.root
    base = _ep(project)
    (root / "episode.json").unlink()
    copy = root / f"episode.{COPY}.json"
    _write(copy, {**base, "title": "Van stories"})
    (asked,) = conflicts.check(root).asked
    d = _decision(project, asked["decision"])
    assert d["data"]["options"] == ["Keep it removed", "Use the other copy"] and d["data"]["texts"]["file"] == "(no file)"
    _tomas(project).append("decision.resolved", artifact="art_ep", data={"decision": d["id"], "choice": "Use the other copy"})
    _write(root / "episode.json", {**base, "title": "Made again"})                 # the file appears before the pass
    (again,) = conflicts.check(root).asked
    assert _decision(project, again["decision"])["data"]["supersedes"] == d["id"]
    assert _ep(project)["title"] == "Made again" and copy.exists()


def test_a_copy_that_changes_or_comes_back_gets_a_fresh_question(project):
    root = project.root
    base = _ep(project)
    _write(root / "episode.json", {**base, "title": "The van"})
    copy = root / f"episode.{COPY}.json"
    _write(copy, {**base, "title": "Van stories"})
    (first,) = conflicts.check(root).asked
    _write(copy, {**base, "title": "Van stories, take two"})                       # C: the copy itself changed
    (second,) = conflicts.check(root).asked
    st = Project.open(root).state()
    assert second["decision"] != first["decision"]
    assert st.decisions[first["decision"]]["resolution"]["superseded"] and st.decisions[second["decision"]]["status"] == "open"
    saved = copy.read_bytes()
    copy.unlink()                                                                   # G: deleted by hand, closed...
    conflicts.check(root)
    assert Project.open(root).state().decisions[second["decision"]]["resolution"]["gone"]
    copy.write_bytes(saved)                                                         # ...then restored: asked again
    (third,) = conflicts.check(root).asked
    d = _decision(project, third["decision"])
    assert d["status"] == "open" and d["data"]["supersedes"] == second["decision"]
    assert "came back" in d["data"]["reason"]


@pytest.mark.parametrize("owner", ["neither", "y"])
def test_two_computers_holding_different_copies_never_supersede_each_other(tmp_path, monkeypatch, owner):
    """J: computer X still has the copy's old bytes, computer Y has newer ones (sync lag, or a copy one side cannot
    receive). Their logs go back and forth many times; only the applier ever closes or re-asks, so the number of
    decisions stays bounded and none of them says the copy "came back"."""
    import shutil
    hx, hy = tmp_path / "hx", tmp_path / "hy"
    hx.mkdir(), hy.mkdir()
    monkeypatch.setenv("HOME", str(hx))
    X = _studio(tmp_path / "X", "dev_OWNER").root                  # X is not the owner
    rel = f"posts/launch.{COPY}.json"
    (x_asked,) = conflicts.check(X).asked
    Y = tmp_path / "Y"
    shutil.copytree(X, Y, ignore=shutil.ignore_patterns("local"))
    (Y / ".ave" / "local").mkdir()
    monkeypatch.setenv("HOME", str(hy))
    y_dev = Project.open(Y).device.id
    (Y / ".ave" / "local" / "owner.json").write_text(json.dumps({"device": y_dev if owner == "y" else "dev_OWNER"}))
    _write(Y / rel, {"id": "launch", "text": "Out today, edited"})  # Y holds other bytes for the same copy
    x_copy = (X / rel).read_bytes()

    def carry(a, b):
        for f in (a / ".ave" / "log").glob("*.jsonl"):
            shutil.copy2(f, b / ".ave" / "log" / f.name)

    counts = []
    for _ in range(6):
        monkeypatch.setenv("HOME", str(hy))
        conflicts.check(Y, items=[".ave/log/x.jsonl"])
        carry(Y, X)
        monkeypatch.setenv("HOME", str(hx))
        conflicts.check(X, items=[".ave/log/y.jsonl"])
        carry(X, Y)
        counts.append(len(Project.open(X).state().decisions))
    from ave.project import board
    st = Project.open(X).state()
    assert counts == [counts[0]] * 6 and counts[0] <= 2, counts      # bounded: no decision per round trip
    assert not any("came back" in (d["data"].get("reason") or "") for d in st.decisions.values())
    opened = [d for d in st.decisions.values() if board.decision_open(d)]
    assert len(opened) == 1                                         # one question at a time
    if owner == "y":                                                # the owner asks about its own bytes, once, and says why
        (d,) = opened
        assert d["data"]["supersedes"] == x_asked["decision"] and d["data"]["reason"].startswith("The other copy changed")
        assert st.decisions[x_asked["decision"]]["resolution"]["superseded"]
    else:
        assert opened[0]["id"] == x_asked["decision"]
    assert (X / rel).read_bytes() == x_copy and (Y / rel).exists()   # nothing written, nothing removed


class NamedSync(FakeSync):
    """A running SyncManager that also knows the name of a computer from its Syncthing settings."""

    def __init__(self, names=None):
        super().__init__()
        self.names = names or {}

    def device_name(self, sync_id):
        return self.names.get(sync_id)


def test_three_members_get_labels_fixed_in_the_record(project):
    """K: with three members the file's writer cannot be inferred. The record says "This computer's copy" (a copy that
    never leaves this computer), or the member Syncthing's modifiedBy names, or the copy's computer by name, and every
    computer shows those words: never "This file's file" or "Use all of This file's"."""
    root = project.root
    meta = add_member(json.loads((root / ".ave" / "project.json").read_text()), "eve", "dev_E", "full", "-".join(["EEEEEEE"] * 8), "e")
    meta["devices"]["dev_S"] = {"sync_id": "-".join(["SSSSSSS"] * 8), "address": "s"}   # a computer no member is on
    (root / ".ave" / "project.json").write_bytes(canonical(meta))
    base = _ep(project)
    _write(root / "episode.json", {**base, "title": "The van"})
    copy = root / f"episode.{COPY}.json"
    _write(copy, {**base, "title": "Van stories"})
    (asked,) = conflicts.check(root).asked
    d = _decision(project, asked["decision"])["data"]
    assert d["summary"] == "episode.json was changed on two computers at once (Title)"
    assert (d["file_label"], d["copy_label"]) == ("This computer's copy", "Tomas's file")
    assert d["options"] == ["Use all of this computer's copy", "Use all of Tomas's"] and d["people"] == ["Tomas"]

    ws = _ws(project)
    (dec,) = _home_row(ws)["decisions"]
    assert (dec["file_label"], dec["copy_label"]) == ("This computer's copy", "Tomas's file")
    # every computer renders what was recorded: a later project.json (two members again) changes nothing shown
    two = {**meta, "members": [m for m in meta["members"] if m["person"] != "eve"]}
    (root / ".ave" / "project.json").write_bytes(canonical(two))
    (dec,) = _home_row(_ws(project))["decisions"]
    assert (dec["file_label"], dec["copy_label"], dec["options"]) == ("This computer's copy", "Tomas's file", d["options"])
    (root / ".ave" / "project.json").write_bytes(canonical(meta))
    ws.patch(_home_row(ws)["key"], {"resolve": asked["decision"], "choice": "Use all of Tomas's"})
    assert conflicts.drain() and _ep(project)["title"] == "Van stories"

    m = NamedSync({"-".join(["SSSSSSS"] * 8): "Studio Mac"})       # with sync running: Syncthing says Eve wrote the file
    m.mod["episode.json"] = "EEEEEEE"
    _write(root / "episode.json", {**_ep(project), "tz": "Asia/Tokyo"})
    other = root / "episode.sync-conflict-20261102-101500-SSSSSSS.json"
    _write(other, {**_ep(project), "tz": "America/Denver"})
    (asked,) = conflicts.check(root, manager=m).asked
    d = _decision(project, asked["decision"])["data"]
    assert (d["file_label"], d["copy_label"]) == ("Eve's file", "Studio Mac's copy")
    assert d["options"] == ["Use all of Eve's", "Use all of Studio Mac's copy"]
    assert d["summary"] == "episode.json was changed on two computers at once (Time zone)"
    for text in [d["summary"], d["file_label"], d["copy_label"], *d["options"], *(v["preview"] for v in d["values"])]:
        assert "This file" not in text and "'s file's" not in text and "copy's" not in text, text


def test_a_copy_that_changes_after_an_answer_says_why_it_is_asked_again(project):
    """L: the copy's bytes change after the person answered: the old answer is not used, and the new question names
    the one it replaces and says why."""
    root = project.root
    base = _ep(project)
    _write(root / "episode.json", {**base, "title": "The van"})
    copy = root / f"episode.{COPY}.json"
    _write(copy, {**base, "title": "Van stories"})
    (asked,) = conflicts.check(root).asked
    _tomas(project).append("decision.resolved", artifact="art_ep", data={"decision": asked["decision"], "choice": "Use all of Tomas's"})
    _write(copy, {**base, "title": "Van stories 2"})
    rep = conflicts.check(root)
    (again,) = rep.asked
    d = _decision(project, again["decision"])
    assert rep.settled == [] and _ep(project)["title"] == "The van" and copy.exists()
    assert d["data"]["supersedes"] == asked["decision"] and d["status"] == "open"
    assert d["data"]["reason"].startswith("The other copy changed after you answered, so nothing was written. Choose again.")


def test_a_post_copy_that_is_not_an_object_can_only_be_set_aside(tmp_path):
    """M: posts/*.json (like every JSON settings file) is read as an object; a copy that is a list can only be set aside,
    and a forged "Use the other copy" writes nothing."""
    p = _studio(tmp_path / "studio", "")
    root = p.root
    (root / ".ave" / "local" / "owner.json").write_text(json.dumps({"device": p.device.id}))
    post, copy = root / "posts" / "launch.json", root / "posts" / f"launch.{COPY}.json"
    copy.write_text('["not", "a post"]')
    (asked,) = conflicts.check(root).asked
    d = _decision(p, asked["decision"])["data"]
    assert d["whole"] and d["options"] == ["Keep this file"] and d["forms"] == {"file": None, "copy": "Not a settings object"}
    before = post.read_bytes()
    p.log.append("decision.resolved", artifact=_decision(p, asked["decision"])["artifact"],
                 data={"decision": asked["decision"], "choice": "Use the other copy"})
    conflicts.check(root, items=[])
    assert post.read_bytes() == before and copy.exists()
    assert conflicts.must_be_object("campaigns/spring.json") and conflicts.must_be_object("recipes/clip.json")
    assert not conflicts.must_be_object("schedule/youtube.yaml")


def test_claude_reads_both_whole_files_and_answers_them(project):
    """Claude's view: artifact_get gives a whole-file question's two texts (capped) and the side labels; a per-key
    answer to it is refused with the options that fit."""
    root = project.root
    copy = root / f"episode.{COPY}.json"
    copy.write_text('{"title": "Episode 12", "tz": "America/Den')
    (asked,) = conflicts.check(root).asked
    app = AgentApp.for_board(root)
    got = app.board("artifact_get", {"artifact": "art_ep"})
    (d,) = [x for x in got["decisions_open"] if x["decision"] == asked["decision"]]
    assert d["whole"] and d["texts"]["copy"] == '{"title": "Episode 12", "tz": "America/Den'
    assert '"layout": "flat"' in d["texts"]["file"] and (d["file_label"], d["copy_label"]) == ("Maren's file", "Tomas's file")
    from ave.project import board
    assert len(board._capped("x" * 10_000)) < 2100
    with pytest.raises(ValueError, match="whole-file question"):
        app.board("decision_resolve", {"request_id": "w1", "decision": asked["decision"], "choice": conflicts.PER_KEY,
                                       "quote": "take hers", "picks": {}})
    desc = next(t for t in __import__("ave.agent", fromlist=["TOOLS"]).TOOLS if t["name"] == "decision_resolve")["description"]
    assert "Keep this file" in desc and "Picked setting by setting" in desc and "texts" in desc
    with pytest.raises(ValueError, match='"Keep this file"'):          # not an object: the copy is never offered
        app.board("decision_resolve", {"request_id": "w2", "decision": asked["decision"], "choice": "Use the other copy",
                                       "quote": "use Tomas's"})
    assert copy.exists() and _ep(project)["layout"] == "flat"
    app.board("decision_resolve", {"request_id": "w3", "decision": asked["decision"], "choice": "Keep this file",
                                   "quote": "keep mine"})
    assert conflicts.drain() and not copy.exists()


def test_the_sync_managers_sweep_applies_an_answer(tmp_path, monkeypatch):
    """The sync manager's event loop sweeps every shared project every SWEEP_S, on the watcher's worker, whether or not a
    window is open: the owner's computer applies an answer recorded elsewhere from that sweep alone."""
    import shutil
    from ave.syncthing import SyncManager
    tomass = _studio(tmp_path / "tomas", "dev_OWNER")
    (asked,) = conflicts.check(tomass.root).asked
    tomass.log.append("decision.resolved", artifact="art_studio", data={"decision": asked["decision"], "choice": "Use all of Tomas's"})
    owners = tmp_path / "owner"
    shutil.copytree(tomass.root, owners, ignore=shutil.ignore_patterns("local"))
    (owners / ".ave" / "local").mkdir()
    (owners / ".ave" / "local" / "owner.json").write_text(json.dumps({"device": tomass.device.id}))
    m = SyncManager(tmp_path / "st", ave_device="dev_A")
    monkeypatch.setattr(m, "_folders", lambda: [{"id": "f1", "path": str(owners)}, {"id": "f2", "path": str(tmp_path / "nothing")}])
    queued = []
    real = conflicts.queue
    monkeypatch.setattr(conflicts, "queue", lambda root, **kw: (queued.append((str(root), kw)), real(root, **kw))[1])
    m._sweep_conflicts()
    assert conflicts.drain()
    assert queued == [(str(owners), {"manager": m})]               # a sweep (no items), only for a real project
    assert json.loads((owners / "posts" / "launch.json").read_text())["text"] == "Out today"
    assert not (owners / "posts" / f"launch.{COPY}.json").exists()
    m._sweep_conflicts()                                            # not again within SWEEP_S
    assert len(queued) == 1


def test_conflict_file_outside_settings_alerts(project):
    root = project.root
    (root / "edits").mkdir()
    _write(root / "edits" / "rough.v1.json", {"stage": "rough"})
    copy = root / "edits" / f"rough.v1.{COPY}.json"
    _write(copy, {"stage": "rough", "other": True})

    rep = conflicts.check(root, items=[f"edits/rough.v1.{COPY}.json"])      # Syncthing finished the copy
    (alert,) = rep.alerts
    assert alert["file"] == "edits/rough.v1.json" and alert["zone"] == "permanent"
    assert "changed on two computers at once" in alert["text"] and "Tomas's" in alert["text"]
    assert "Nothing was merged: both copies are kept" in alert["text"]
    assert rep.settled == rep.asked == []
    assert copy.exists() and json.loads((root / "edits" / "rough.v1.json").read_text()) == {"stage": "rough"}
    assert not Project.open(root).state().decisions
    assert conflicts.check(root).alerts == [alert]                           # remembered by later passes

    notices = _ws(project).board()["projects"][0]["notices"]
    assert notices == [{"type": "alert", "text": alert["text"], "file": "edits/rough.v1.json", "conflict": alert["conflict"]}]
    got = AgentApp.for_board(root).board("artifact_list", {})               # Claude reads it too, with what to do
    assert got["conflict_copies"] == [{"file": "edits/rough.v1.json", "other_copy": alert["conflict"], "text": alert["text"]}]
    assert "ask the director which one to keep" in got["summary"]
    copy.unlink()
    assert _ws(project).board()["projects"][0]["notices"] == []
    assert conflicts.check(root).alerts == []


def test_the_sweep_never_lists_media_or_work_folders(project, monkeypatch):
    root = project.root
    for d in ("raw/camA/day1", "raw/camB", "work/render", "edits", "captions", "exports"):
        (root / d).mkdir(parents=True)
        (root / d / "file.bin").write_bytes(b"x")
    seen = []
    real = conflicts._scandir
    monkeypatch.setattr(conflicts, "_scandir", lambda d: (seen.append(Path(d).relative_to(root).as_posix()), real(d))[1])
    conflicts.check(root)
    assert set(seen) <= {".", ".ave"}, seen
    seen.clear()
    conflicts.check(root, items=["raw/camA/day1/file.bin", "edits/rough.v2.json"])   # ordinary arrivals: no listing at all
    assert seen == []


def test_presence_expires(project):
    t0 = 1_800_000_000_000
    clock = {"now": t0}
    p = project

    # writes are rare: never more than once a minute, renewed each minute while work goes on (so presence lasts about the
    # full ten minutes after the last work), a new place written once the minute has passed
    assert presence.touch(p, "art_ep", "cut", now=t0) is not None
    assert presence.touch(p, "art_ep", "cut", now=t0 + 30_000) is None
    assert presence.touch(p, "art_ep", "script", now=t0 + 30_000) is None         # never more than once a minute
    assert presence.touch(p, "art_ep", "cut", now=t0 + 61_000) is not None         # work goes on: renewed
    assert presence.touch(p, "art_ep", "script", now=t0 + 90_000) is None
    assert presence.touch(p, "art_ep", "script", now=t0 + 200_000) is not None
    presence.forget()                                                              # a restarted app reads its last write
    assert presence.touch(p, "art_ep", "script", now=t0 + 230_000) is None
    writes = [e for e in p.read_events() if e["op"] == "presence.set"]
    assert len(writes) == 3 and writes[-1]["data"]["until"] == t0 + 200_000 + presence.TTL_S * 1000
    assert presence.active(reduce(p.read_events()), t0 + 200_000 + 599_999)          # ten minutes after the last work

    # Tomas, on her computer, opens the Room on the cut
    _tomas(p, wall=lambda: t0 + 5_000).append("presence.set", artifact="art_ep",
                                               data={"view": "cut", "at": t0 + 5_000, "until": t0 + 5_000 + 600_000})
    ws = _ws(p, clock=lambda: clock["now"])
    row = lambda: next(r for r in ws.board()["projects"][0]["artifacts"] if r["artifact"] == "art_ep")
    clock["now"] = t0 + 60_000
    assert row()["presence"] == [{"person": "Tomas", "where": "Cut", "text": "Tomas is in Cut", "until": t0 + 605_000}]
    assert row()["updated_by"]["name"] != "tomas"                 # being there is not a change to the artifact
    assert ws.room_info("art_ep")["presence"][0]["text"] == "Tomas is in Cut"
    clock["now"] = t0 + 604_999
    assert row()["presence"]
    clock["now"] = t0 + 605_000
    assert row()["presence"] == []                                # gone by itself, with no event
    assert ws.room_info("art_ep")["presence"] == []
    assert presence.active(reduce(p.read_events()), t0 + 605_000) != []           # this computer's own is still on
    assert presence.active(reduce(p.read_events()), t0 + 200_000 + 600_000) == []

    # an `until` beyond the ten minutes is never believed
    _tomas(p, wall=lambda: t0 + 700_000).append("presence.set", artifact="art_ep",
                                                data={"view": "selects", "at": t0 + 700_000, "until": t0 + 99_000_000})
    clock["now"] = t0 + 1_300_001
    assert row()["presence"] == []


def test_stage_conflict_is_resolved_in_chat_or_board(project, monkeypatch):
    from ave import humanpresence
    for k in humanpresence.AGENT_MARKERS:                    # the board's pick below comes through the app
        monkeypatch.delenv(k, raising=False)
    p = project
    tomas = _tomas(p)
    ws = _ws(p)

    def clash():
        """Both computers move the stage on from where they agreed, before either sees the other's change."""
        st = reduce(p.read_events())
        base = st.artifacts["art_ep"].stage
        fr = {d: h for d, h in st.frontier.items()}
        nxt = {"Plan": "Draft", "Draft": "Refine", "Refine": "Finish"}[base]
        p.log.append("stage.signed_off", artifact="art_ep", data={"from": base, "to": nxt}, seen=fr)
        tomas.append("stage.reopened" if base != "Plan" else "stage.signed_off", artifact="art_ep",
                     data={"from": base, "to": "Plan" if base != "Plan" else "Refine"}, seen=fr)
        return base

    base = clash()
    st = reduce(p.read_events())
    assert st.conflicts and st.artifacts["art_ep"].stage == base

    row = next(r for r in ws.board()["projects"][0]["artifacts"] if r["artifact"] == "art_ep")
    assert row["waiting"] == "you"
    c = row["conflict"]
    assert c["text"] == "Maren and Tomas both changed Plan"
    assert [(ch["stage"], ch["who"]) for ch in c["choices"]] == [("Draft", "Maren"), ("Refine", "Tomas")]
    assert c["choices"][1]["label"] == "Refine, as Tomas set it"
    assert ws.room_info("art_ep")["conflict"]["text"] == "Maren and Tomas both changed Plan"

    # Claude cannot sign off past it, nor settle it without the person's words, nor to a stage nobody chose
    app = AgentApp.for_board(p.root)
    got = app.board("artifact_get", {"artifact": "art_ep"})
    assert got["stage_conflict"]["text"] == "Maren and Tomas both changed Plan" and "stage_settle" in got["summary"]
    with pytest.raises(ValueError, match="two computers"):
        app.board("stage_sign_off", {"request_id": "s0", "artifact": "art_ep", "stage": "Plan", "quote": "lock it"})
    with pytest.raises(ValueError, match="quote"):
        app.board("stage_settle", {"request_id": "s1", "artifact": "art_ep", "stage": "Refine", "quote": "  "})
    with pytest.raises(ValueError, match="Draft or Refine"):
        app.board("stage_settle", {"request_id": "s2", "artifact": "art_ep", "stage": "Finish", "quote": "Finish"})

    # in chat: the person said which one
    out = app.board("stage_settle", {"request_id": "s3", "artifact": "art_ep", "stage": "Refine", "quote": "Tomas's right, Refine"})
    assert out["stage"] == "Refine"
    st = reduce(p.read_events())
    assert not st.conflicts and st.artifacts["art_ep"].stage == "Refine"
    e = _last(p)
    assert e["actor"] == "claude" and e["data"]["relayed_by"] == "claude" and e["data"]["settles"]
    row = next(r for r in ws.board()["projects"][0]["artifacts"] if r["artifact"] == "art_ep")
    assert row["conflict"] is None
    (rel,) = row["relayed"]
    assert rel["type"] == "settle" and rel["quote"] == "Tomas's right, Refine" and rel["can_undo"]
    assert app.board("stage_settle", {"request_id": "s3", "artifact": "art_ep", "stage": "Refine",
                                      "quote": "Tomas's right, Refine"})["replayed"]
    with pytest.raises(ValueError, match="no stage changed on two computers"):
        app.board("stage_settle", {"request_id": "s4", "artifact": "art_ep", "stage": "Refine", "quote": "Refine"})

    # on the board: the next disagreement is settled by picking one, which is a new log event that saw both
    base = clash()
    row = next(r for r in ws.board()["projects"][0]["artifacts"] if r["artifact"] == "art_ep")
    assert row["conflict"]["text"] == "Maren and Tomas both changed Refine"
    assert [ch["stage"] for ch in row["conflict"]["choices"]] == ["Plan", "Finish"]
    n = len(p.read_events())
    ws.patch(row["key"], {"stage": "Finish"}, via="app")
    events = p.read_events()
    assert len(events) == n + 1 and _last(p)["data"]["settles"] and _last(p)["actor"] == "human"
    st = reduce(events)
    assert not st.conflicts and st.artifacts["art_ep"].stage == "Finish"
    act = ws.board()["activity"][0]
    assert act["text"] == "settled the stage of Episode 12 on Finish"


def test_presence_follows_work_not_an_open_tab(project):
    """Presence is refreshed by this person's own log events (app, CLI, MCP), at most about once a minute; old work and
    the watcher's own events are not a sign of anyone here."""
    from ave.project import hlc
    p = project
    t = {"now": hlc.parse(_last(p)["hlc"]).ms + 1_000}
    ws = _ws(p, clock=lambda: t["now"])
    assert ws.look_work() == [p.project_id]                         # the artifact was just made here: Maren is on it
    mine = [e for e in p.read_events() if e["op"] == "presence.set"]
    assert len(mine) == 1 and mine[0]["artifact"] == "art_ep" and mine[0]["data"]["view"] is None
    t["now"] += 30_000
    p.log.append("note.added", artifact="art_ep", data={"text": "Trim the intro"}, actor="claude")
    assert ws.look_work() == []                                     # within the minute: no second write
    t["now"] += 10 * 60_000
    assert ws.look_work() == []                                     # nothing new since: the presence lapses by itself
    assert presence.active(reduce(p.read_events()), t["now"]) == []


def test_an_answered_settings_conflict_is_not_applied_in_a_read_only_project(project, monkeypatch):
    """A newer AVE wrote to the project after the question was answered: the automatic apply rewrites nothing, deletes
    no copy, and the conflict stays open until this computer is updated."""
    import sys
    import ave
    root = project.root
    base = _ep(project)
    maren = {**base, "title": "The van"}
    _write(root / "episode.json", maren)
    copy = root / f"episode.{COPY}.json"
    _write(copy, {**base, "title": "Van stories"})
    (asked,) = conflicts.check(root).asked
    ws = _ws(project)
    with monkeypatch.context() as m:                         # the answer is recorded; its apply runs below, after Tomas's write
        m.setattr(conflicts, "queue", lambda *a, **k: None)
        ws.patch(_home_row(ws)["key"], {"resolve": asked["decision"], "choice": "Use all of Tomas's"})
    with monkeypatch.context() as m:                         # Tomas's computer, on a newer release, writes to the project
        m.setattr(ave, "__version__", "99.0.0")
        m.setattr(sys, "frozen", True, raising=False)
        _tomas(project).append("note.added", data={"text": "from the newer app"})
    before = (root / "episode.json").read_bytes()
    conflicts.drain()
    conflicts.check(root)
    assert (root / "episode.json").read_bytes() == before and copy.exists()
    assert not any((n["data"] or {}).get("kind") == "settings_merged" for n in Project.open(root).state().notes.values())


def test_undoing_a_relayed_settings_answer_puts_both_copies_back(project, monkeypatch):
    """Final review I4: an answer Claude relays is applied by the owner straight away, so before the losing copy is deleted the
    owner archives both sides (.ave/local/conflict-archive/<decision>.json). The director's Undo then restores the file and the
    other copy, byte for byte, and the question is open again with both sides to choose between."""
    from ave import humanpresence
    for k in humanpresence.AGENT_MARKERS:
        monkeypatch.delenv(k, raising=False)
    root = project.root
    base = _ep(project)
    _write(root / "episode.json", {**base, "title": "The van", "notes": "x" * 9000})          # longer than the record keeps
    mine = (root / "episode.json").read_bytes()
    copy = root / f"episode.{COPY}.json"
    _write(copy, {**base, "title": "Van stories"})
    theirs = copy.read_bytes()
    (asked,) = conflicts.check(root).asked
    AgentApp.for_board(root).board("decision_resolve", {"request_id": "u1", "decision": asked["decision"],
                                                       "choice": "Use all of Tomas's", "quote": "take Tomas's"})
    assert conflicts.drain()
    assert _ep(project)["title"] == "Van stories" and not copy.exists()                     # applied at once, copy removed
    kept = conflicts.archived(root, asked["decision"])
    assert kept and kept["file"] == "episode.json" and (root / ".ave/local/conflict-archive" / f"{asked['decision']}.json").is_file()
    relayed = next(e for e in Project.open(root).read_events() if e["op"] == "decision.resolved" and (e["data"] or {}).get("relayed_by"))
    ws = _ws(project)
    (r,) = [x for x in _home_row(ws)["relayed"] if x["type"] == "resolution"]
    assert r["event"] == relayed["id"] and r["can_undo"]
    ws.patch(_home_row(ws)["key"], {"undo": relayed["id"]}, via="app")                       # the director's Undo
    assert conflicts.drain()
    assert (root / "episode.json").read_bytes() == mine and copy.read_bytes() == theirs       # both sides back, byte for byte
    assert conflicts.archived(root, asked["decision"]) is None
    (dec,) = _home_row(_ws(project))["decisions"]
    assert dec["decision"] == asked["decision"] and dec["kind"] == "setting"                 # asked again, with both sides
    assert any((n["data"] or {}).get("kind") == "settings_restored" for n in Project.open(root).state().notes.values())
    ws.patch(_home_row(ws)["key"], {"resolve": asked["decision"], "choice": "Use all of Maren's"})
    assert conflicts.drain()
    assert (root / "episode.json").read_bytes() == mine and not copy.exists()


def test_settling_a_stage_on_finish_waits_for_the_directors_trim(project):
    """Final review M1: stage_settle onto Finish runs the same trim check as stage_sign_off (and so does a room-token pick)."""
    p = project
    meta = _ep(p)
    meta["trim"] = {"main": {"in": 3.2, "out": 60.0, "by": "claude", "draft_only": True, "at": "2026-11-02T10:00:00+0000"}}
    _write(p.root / "episode.json", meta)
    p.log.append("stage.signed_off", artifact="art_ep", data={"from": "Plan", "to": "Draft"})
    p.log.append("stage.signed_off", artifact="art_ep", data={"from": "Draft", "to": "Refine"})
    fr = dict(reduce(p.read_events()).frontier)
    p.log.append("stage.signed_off", artifact="art_ep", data={"from": "Refine", "to": "Finish"}, seen=fr)
    _tomas(p).append("stage.reopened", artifact="art_ep", data={"from": "Refine", "to": "Draft"}, seen=fr)
    assert reduce(p.read_events()).conflicts
    app = AgentApp.for_board(p.root)
    with pytest.raises(ValueError, match="cannot settle on Finish yet: a trim needs the director's confirmation"):
        app.board("stage_settle", {"request_id": "t1", "artifact": "art_ep", "stage": "Finish", "quote": "Maren's right, Finish"})
    ws = _ws(p)
    with pytest.raises(Exception, match="trim needs the director"):
        ws.patch(_home_row(ws)["key"], {"stage": "Finish"})                                # the room token: Claude's
    assert reduce(p.read_events()).conflicts                                              # nothing was settled
    assert app.board("stage_settle", {"request_id": "t2", "artifact": "art_ep", "stage": "Draft", "quote": "Draft then"})["stage"] == "Draft"
