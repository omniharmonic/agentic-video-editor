"""Where a file is, in the words the Sources view shows: people by name, each member's media choice, never a device id."""

from __future__ import annotations

import json

from ave.project import Project
from ave.project.where import Where, names, possessive


def _project(tmp_path, *others):
    p = Project.create(tmp_path / "ep", "Episode 12", person="Maren")
    meta = json.loads(p.project_json.read_text())
    for person, media in others:
        meta["members"].append({"person": person, "devices": [f"dev-{person.lower()}"], "media": media})
    p.project_json.write_text(json.dumps(meta))
    return p


def test_names_and_possessives_read_as_sentences():
    assert names(["Imani"]) == "Imani" and names(["Imani", "Sam"]) == "Imani and Sam" and names(["Imani", "Sam", "Lee"]) == "Imani, Sam and Lee"
    assert possessive(["Imani"]) == "Imani's computer" and possessive(["Imani", "James"]) == "Imani's and James' computers"


def test_each_zone_and_media_choice_in_plain_words(tmp_path):
    p = _project(tmp_path, ("Imani", "proxies"), ("Sam", "full"))
    w = Where.of(p.root, p.device.id)
    assert w.describe("raw/cam/a.mov", has_proxy=True)["text"] == "Shared with Sam · Proxies only on Imani's computer"
    assert w.describe("raw/cam/b.mov", has_proxy=False)["text"] == "Shared with Sam · Not on Imani's computer: no proxy yet"
    assert w.describe("raw/proxies/cam/a.mov")["text"] == "Shared with Imani and Sam"           # the proxy itself goes to everyone
    assert w.describe("work/music/bed.wav")["text"] == "Shared with Imani and Sam"
    local = w.describe("work/cache/render.mp4")
    assert local["text"] == "Only on this computer" and local["zone"] == "local"
    assert w.proxy_of("raw/cam/a.mp4") == "raw/proxies/cam/a.mov" and w.proxy_of("work/music/bed.wav") is None
    for text in (w.describe(r)["text"] for r in ("raw/cam/a.mov", "work/cache/x.mp4", ".ave/project.json")):
        assert "dev-" not in text


def test_a_project_nobody_else_is_on_is_not_shared_yet(tmp_path):
    p = _project(tmp_path)
    w = Where.of(p.root, p.device.id)
    assert w.describe("raw/a.mov")["text"] == "Not shared yet"
    assert w.describe("work/a.mp4")["text"] == "Only on this computer"


def test_a_computer_that_is_not_listed_gets_the_proxies_rules_and_never_names_its_own_person(tmp_path):
    p = _project(tmp_path, ("Imani", "full"))
    w = Where.of(p.root, "dev-unknown", "Maren")                                                # Maren's second Mac, not added yet
    assert w.describe("raw/a.mov")["text"] == "Only on this computer"                           # its own originals never leave
    assert w.describe("raw/proxies/a.mov")["text"] == "Shared with Imani"                       # never "Shared with Maren and Imani"


def test_one_person_on_two_computers_is_named_once(tmp_path):
    p = _project(tmp_path, ("Imani", "proxies"))
    meta = json.loads(p.project_json.read_text())
    meta["members"].append({"person": "Imani", "devices": ["dev-imani-2"], "media": "full"})
    p.project_json.write_text(json.dumps(meta))
    w = Where.of(p.root, p.device.id)
    assert w.describe("raw/a.mov", has_proxy=True)["text"] == "Shared with Imani"              # her full-media computer has it


def test_a_proxies_computer_reads_its_proxies_as_standing_in_for_the_originals(tmp_path):
    p = Project.create(tmp_path / "ep", "Episode 12", person="Imani")
    meta = json.loads(p.project_json.read_text())
    meta["members"][0]["media"] = "proxies"
    meta["members"].append({"person": "Maren", "devices": ["dev-maren"], "media": "full"})
    p.project_json.write_text(json.dumps(meta))
    w = Where.of(p.root, p.device.id)
    assert w.media == "proxies" and w.original_dir_of("raw/proxies/cam/a.mov") == ("raw/cam", "a")
    assert w.describe_proxy("raw/proxies/cam/a.mov")["text"] == "Proxy on this computer; original on Maren's computer"
    assert w.describe("raw/cam/a.mov")["text"] == "Only on this computer"


def test_a_studio_project_shares_its_plan_and_library_and_keeps_publishing_local(tmp_path):
    from ave.studio.project import StudioProject
    sp = StudioProject(tmp_path / "studio")
    sp.create(title="Acme", tz="UTC")
    p = Project.create(sp.root, "Acme", person="Maren")
    meta = json.loads(p.project_json.read_text())
    meta["members"].append({"person": "Imani", "devices": ["dev-imani"], "media": "full"})
    p.project_json.write_text(json.dumps(meta))
    w = Where.of(p.root, p.device.id)
    assert w.studio
    for rel in ("posts/p_1.json", "library/assets/ab/abc.png", "campaigns/c.json"):
        assert w.describe(rel)["text"] == "Shared with Imani", rel
    for rel in ("accounts/ig.yaml", "ledger/events.jsonl", "schedule/queue.json", "approval.key", "token.json"):
        assert w.describe(rel)["text"] == "Only on this computer", rel
