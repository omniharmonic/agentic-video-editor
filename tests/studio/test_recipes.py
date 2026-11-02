"""Recipes (spec 9): the shipped ones are well formed, the validator enforces the safety rules, and `check` derives state from what is on disk and
never skips a human step."""

import json
from pathlib import Path

import pytest
import yaml

from ave import cli, resources
from ave.studio import recipes
from ave.studio.project import StudioProject
from ave.studio.service import open_studio

SHIPPED = ["audio-to-clips", "blog-to-posts", "episode-shorts-to-posts", "episode-to-campaign", "idea-to-visuals", "product-spotlight", "quote-drop",
           "video-to-shorts"]


@pytest.fixture
def sp(tmp_path):
    p = StudioProject(tmp_path / "studio")
    p.create(title="Acme", tz="America/Denver")
    return p


def good() -> dict:
    return {"name": "tiny", "title": "Tiny", "summary": "x", "inputs": [{"name": "file", "required": True}],
            "steps": [{"id": "a", "kind": "mechanical", "title": "A", "command": "ave studio ingest blog {file}", "done_when": {"file": "work/a.json"}},
                      {"id": "b", "kind": "agent", "title": "B", "skill": "ave-studio-ideas", "checklist": ["look"], "done_when": {"file": "work/b.json"}},
                      {"id": "c", "kind": "human", "title": "C", "prompt": "look", "done_when": {"file": "work/c.json"}}]}


def test_every_shipped_recipe_is_valid_and_all_the_spec_recipes_exist():
    allr = recipes.load_all()
    assert sorted(allr) == SHIPPED
    for name, r in allr.items():
        assert recipes.validate(r) == [], name
        assert r["steps"][-1]["kind"] == "human" or any(s["kind"] == "human" for s in r["steps"]), name
    skills = {p.name for p in (resources.home() / "skills").iterdir() if (p / "SKILL.md").is_file()}
    for r in allr.values():
        for s in r["steps"]:
            if s["kind"] == "agent":
                assert s["skill"] in skills


def test_validator_names_every_kind_of_mistake():
    assert recipes.validate(good()) == []
    r = good()
    r["steps"][0]["command"] = "rm -rf /"
    r["steps"][1]["skill"] = "no-such-skill"
    r["steps"][1]["checklist"] = []
    r["steps"].append({"id": "a", "kind": "wat", "title": "dup"})
    r["steps"][2]["prompt"] = ""
    r["steps"][2]["done_when"] = {"bogus": 1}
    p = " | ".join(recipes.validate(r))
    for frag in ("one `ave ...` command", "not a skill", "non-empty checklist", "used twice", "kind must be one of", "human step needs a prompt", "unknown done_when key"):
        assert frag in p, (frag, p)
    r = good()
    r["steps"] = r["steps"][:2]
    assert any("ends in a human decision" in m for m in recipes.validate(r))
    r = good()
    r["steps"][0]["command"] = "ave studio ingest blog {nope}"
    assert any("{nope} is not a declared input" in m for m in recipes.validate(r))


@pytest.mark.parametrize("cmd", ["ave studio approve p v", "ave publish approve EP id", "ave studio account live ig-main --on", "ave studio resume",
                                 "ave studio tick --send", "ave studio run --send", "ave studio broll pick s c --by maren", "ave studio schedule apply sp_0001",
                                 "ave studio resolve p v --retry", "ave studio account auth ig-main", "ave studio library attest --all", "ave studio library attest a_0123456789ab",
                                 "ave studio send enable", "ave studio ui", "ave ui EP", "ave desktop STUDIO", "ave trim set EP s1 --in 1 --out 2", "ave learn taste apply EP"])
def test_a_human_act_may_only_appear_in_a_human_step(cmd):
    r = good()
    r["steps"][0]["command"] = cmd
    assert any("human act" in m for m in recipes.validate(r)), cmd
    r = good()
    r["steps"][2]["command"] = cmd                      # the same command is fine in a human step
    assert not any("human act" in m for m in recipes.validate(r))


def test_a_tick_may_not_come_before_a_human_step():
    r = good()
    r["steps"].insert(0, {"id": "t", "kind": "mechanical", "title": "T", "command": "ave studio tick", "done_when": {"file": "x"}})
    assert any("must come after a human step" in m for m in recipes.validate(r))


def test_check_derives_state_from_disk_and_names_the_next_step(sp, tmp_path):
    st = open_studio(sp.root)
    md = tmp_path / "essay.md"
    md.write_text("# E\n\nA paragraph of the essay.\n")
    res = recipes.check(st, "blog-to-posts", {"file": str(md)})
    assert res["problems"] == [] and res["next"]["id"] == "ingest" and res["next"]["kind"] == "mechanical"
    assert res["next"]["command"] == f"ave studio ingest blog {md} --studio {sp.root}" and not res["needs_human"] and not res["complete"]
    assert [s["state"] for s in res["steps"]][:3] == ["next", "pending", "pending"]
    from ave.studio import ingest
    ingest.blog(sp, md)                                  # the artifact now exists: the step is done without anyone saying so
    res = recipes.check(st, "blog-to-posts", {"file": str(md)})
    assert res["steps"][0]["state"] == "done" and res["next"]["id"] == "choose" and res["next"]["skill"] == "ave-studio-ideas" and res["next"]["checklist"]


def test_check_never_skips_a_human_step(sp):
    (sp.recipes_dir).mkdir(exist_ok=True)
    r = good()
    r["name"] = "gate"
    (sp.recipes_dir / "gate.yaml").write_text(yaml.safe_dump(r))
    st = open_studio(sp.root)
    (sp.root / "work").mkdir(exist_ok=True)
    (sp.root / "work" / "a.json").write_text("{}")
    (sp.root / "work" / "b.json").write_text("{}")
    res = recipes.check(st, "gate", {"file": "x"})
    assert res["next"]["id"] == "c" and res["next"]["kind"] == "human" and res["needs_human"] is True and "prompt" in res["next"]
    # even if a LATER step's artifact existed, the human step stays next and the later one is flagged out of order
    r["steps"].append({"id": "d", "kind": "mechanical", "title": "D", "command": "ave studio queue sync", "done_when": {"file": "work/d.json"}})
    (sp.recipes_dir / "gate.yaml").write_text(yaml.safe_dump(r))
    (sp.root / "work" / "d.json").write_text("{}")
    res = recipes.check(st, "gate", {"file": "x"})
    assert res["next"]["id"] == "c" and res["steps"][3]["state"] == "done" and res["steps"][3]["out_of_order"] is True
    (sp.root / "work" / "c.json").write_text("{}")                  # the human's artifact: now (and only now) it is done
    res = recipes.check(st, "gate", {"file": "x"})
    assert res["complete"] and res["next"] is None and not res["needs_human"]


def test_check_validates_inputs_and_studio_recipes_override_shipped(sp, tmp_path):
    st = open_studio(sp.root)
    res = recipes.check(st, "blog-to-posts", {})
    assert any("missing required argument file" in p for p in res["problems"])
    res = recipes.check(st, "blog-to-posts", {"file": str(tmp_path / "nope.md"), "extra": "1"})
    assert any("does not exist" in p for p in res["problems"]) and any("unknown argument 'extra'" in p for p in res["problems"])
    assert recipes.parse_args(["a=1", "b=two=2"]) == {"a": "1", "b": "two=2"}
    with pytest.raises(recipes.RecipeError, match="key=value"):
        recipes.parse_args(["bad"])
    with pytest.raises(recipes.RecipeError, match="no recipe"):
        recipes.load(sp, "nope")
    sp.recipes_dir.mkdir(exist_ok=True)
    r = good()
    r["name"] = "quote-drop"
    r["title"] = "Mine"
    (sp.recipes_dir / "quote-drop.yaml").write_text(yaml.safe_dump(r))
    assert recipes.load(sp, "quote-drop")["title"] == "Mine" and recipes.load_all()["quote-drop"]["title"] != "Mine"


def test_cli_recipe_list_show_check_and_exit_codes(sp, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("AVE_WORKSPACE", str(tmp_path))
    assert cli.main(["studio", "recipe", "list", "--studio", str(sp.root)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in out["recipes"]] == SHIPPED
    assert cli.main(["studio", "recipe", "show", "quote-drop", "--studio", str(sp.root)]) == 0
    assert json.loads(capsys.readouterr().out)["recipe"]["name"] == "quote-drop"
    md = tmp_path / "e.md"
    md.write_text("# E\n\nA paragraph of the essay.\n")
    assert cli.main(["studio", "recipe", "check", "quote-drop", "--args", f"file={md}", "passage=p001", "--studio", str(sp.root)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["next"]["id"] == "ingest"
    assert cli.main(["studio", "recipe", "check", "quote-drop", "--studio", str(sp.root)]) == 2                  # missing inputs: blocked
    assert json.loads(capsys.readouterr().out)["problems"]
    assert cli.main(["studio", "recipe", "show", "nope", "--studio", str(sp.root)]) == 2
    capsys.readouterr()


def test_the_episode_shorts_recipe_keeps_every_human_act_in_a_human_step():
    r = recipes.load_all()["episode-shorts-to-posts"]
    assert recipes.validate(r) == []
    kinds = {s["id"]: s["kind"] for s in r["steps"]}
    assert kinds == {"survey": "mechanical", "choose": "human", "shorts": "agent", "ingest": "agent", "attest": "human", "sync": "mechanical", "review": "human"}
    order = [s["id"] for s in r["steps"]]
    assert order.index("choose") < order.index("shorts") < order.index("ingest") < order.index("attest") < order.index("sync") < order.index("review")
    cmds = " ".join(str(s.get("command") or "") for s in r["steps"] if s["kind"] == "mechanical")
    assert "attest" not in cmds and "approve" not in cmds and "--send" not in cmds and "queue sync" in cmds
    text = " ".join(" ".join(s.get("checklist") or []) for s in r["steps"] if s["kind"] == "agent")
    assert "never approves" in text and "ave shorts recut" in text and "ave broll place" in text and "ave studio ingest shorts" in text


def test_done_when_library_covered_counts_only_attested_closures(sp, tmp_path):
    from ave import library
    from studio_helpers import make_png
    st = open_studio(sp.root)
    src = library.add_file(sp, make_png(tmp_path / "a.png", size=(8, 8)), source={"type": "own"}, by="claude", attested=False)
    library.add_file(sp, make_png(tmp_path / "r.png", size=(9, 9)), source={"type": "render"}, by="claude", derived_from=[src["id"]])
    step = {"done_when": {"library": {"source": "render", "covered": True, "min": 1}}}
    done, ev = recipes._evaluate(st, step, {})
    assert not done and "0 library asset(s) from render with a covered, attested license" in ev[0]
    assert recipes._evaluate(st, {"done_when": {"library": {"source": "render", "min": 1}}}, {})[0]                 # without `covered` it only counts
    library.attest_license(sp, src["id"], "Maren")
    done, ev = recipes._evaluate(st, step, {})
    assert done and "1 library asset(s)" in ev[0]
