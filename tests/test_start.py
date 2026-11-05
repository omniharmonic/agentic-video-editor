"""`ave start`: a folder of footage to a rough cut in one go, every choice made alone flagged on the board, resumable from disk.

Uses the demo's footage builder (`ave.synth`): two cameras and a recorder with known truth. Tones are not speech, so the one
stand-in is the transcriber, which writes the generator's known transcript without speaker names (as a real ASR run would).
"""

import hashlib
import json
import shutil

import pytest

from ave import humanpresence, start, synth, transcribe
from ave.agent import AgentApp
from ave.cli import main
from ave.episode import Episode, write_json
from ave.project import Project, board

from conftest import needs_ffmpeg

pytestmark = needs_ffmpeg
NAMES = {"S1": "Maren", "S2": "Ana"}


def _fake_transcribe(ep, **kw):
    t = synth.transcript("raw/camA.mp4")
    for v in t["speakers"].values():
        v["name"] = None
    write_json(ep.artifact("transcript.json"), t)
    return t


@pytest.fixture(scope="session")
def shared_home(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    return home


@pytest.fixture(autouse=True)
def _env(monkeypatch, shared_home):
    monkeypatch.setenv("HOME", str(shared_home))          # one computer for every copy of the project
    monkeypatch.setattr(transcribe, "transcribe", _fake_transcribe)


@pytest.fixture(scope="session")
def raw_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("raw") / "episode"
    ep = Episode(root)
    ep.create(title="Demo episode")
    synth.build_media(ep.raw)
    return root


@pytest.fixture(scope="session")
def done_root(tmp_path_factory, raw_root, shared_home):
    root = tmp_path_factory.mktemp("done") / "episode"
    shutil.copytree(raw_root, root)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOME", str(shared_home))
        mp.setattr(transcribe, "transcribe", _fake_transcribe)
        start.run(root, speakers=NAMES)
    return root


@pytest.fixture
def demo_raw(raw_root, tmp_path):
    dst = tmp_path / "episode"
    shutil.copytree(raw_root, dst)
    return dst


@pytest.fixture
def demo_done(done_root, tmp_path):
    dst = tmp_path / "episode"
    shutil.copytree(done_root, dst)
    return dst


def _hashes(root):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (root / "raw").iterdir() if p.is_file()}


def _edits(root):
    return sorted(p.name for p in (root / "edits").glob("*.json"))


def _events(root, op=None):
    return [e for e in Project.open(root).read_events() if op is None or e["op"] == op]


def test_runs_the_demo_to_a_rough_cut(demo_raw):
    before = _hashes(demo_raw)
    r = start.run(demo_raw, speakers=NAMES)
    assert [s["step"] for s in r.steps] == list(start.STEPS)
    assert {s["status"] for s in r.steps} <= {"done", "checked"} and r.stopped is None
    edits = _edits(demo_raw)
    assert any(n.startswith("assembly.v1") for n in edits) and any(n.startswith("rough.v1") for n in edits)
    assert list((demo_raw / "exports").glob("rough.v1*.proxy.mp4"))
    meta = json.loads((demo_raw / "episode.json").read_text())
    assert meta["speakers"]["S1"]["name"] == "Maren" and meta["cameras"]
    assert {v["file"] for v in meta["cameras"].values()} == {"raw/camA.mp4", "raw/camB.mp4"}
    assert _hashes(demo_raw) == before                       # original footage only read
    text = r.text()
    assert "rough cut" in text and "Nothing has been published" in text


def _open(root, prefix=""):
    return [d for d in board.open_decisions(Project.open(root).state(), "art_ep") if d["summary"].startswith(prefix)]


def test_each_choice_is_a_flagged_decision(demo_raw):
    r = start.run(demo_raw, speakers=NAMES)
    recorded = _events(demo_raw, "decision.recorded")
    assert recorded and all(e["actor"] == "claude" and e["artifact"] == "art_ep" for e in recorded)
    for e in recorded:
        d = e["data"]
        assert len(d["options"]) >= 2 and d["chosen"] in d["options"] and d["reason"]
    summaries = " | ".join(e["data"]["summary"] for e in recorded)
    for what in ("Sync reference", "Camera mapping", "Trim for the draft", "Assembly canvas", "pacing"):
        assert what in summaries, what
    # the demo cameras carry the same sound, so the mapping is a guess and says so where the director reads it
    assert "Camera mapping (a guess: same sound on both cameras):" in summaries
    still_open = board.open_decisions(Project.open(demo_raw).state(), "art_ep")
    assert {d["decision"] for d in still_open} == {e["data"]["decision"] for e in recorded}
    assert len(r.decisions_open) == len(recorded) and len(r.decisions_new) == len(recorded)
    tasks = Project.open(demo_raw).state().tasks.values()
    assert {t["fields"]["title"] for t in tasks} == {f"Start: {start.LABELS[s]}" for s in start.STEPS}
    assert all(t["fields"]["status"] == "done" for t in tasks)
    assert all(e["actor"] == "claude" for e in _events(demo_raw, "task.updated"))
    versions = _events(demo_raw, "version.added")            # the versions Claude made are logged as Claude's
    assert len(versions) == 2 and all(e["actor"] == "claude" for e in versions)
    meta = json.loads((demo_raw / "episode.json").read_text())
    assert meta["trim"]["main"]["by"] == "claude" and meta["trim"]["main"]["draft_only"] is True


def test_stops_for_unknown_speakers_and_resumes(demo_raw, capsys):
    assert main(["start", str(demo_raw)]) == 2
    out = capsys.readouterr().out
    assert "Who is speaking?" in out and "--speaker S1=NAME --speaker S2=NAME" in out
    assert not _edits(demo_raw)                              # nothing built on a guess
    asked = _open(demo_raw, "Who is speaking")
    assert len(asked) == 1 and asked[0]["chosen"] == "stop and ask the director"
    blocked = [t for t in Project.open(demo_raw).state().tasks.values() if t["fields"]["title"] == "Start: Who is speaking"]
    assert blocked[0]["fields"]["status"] == "blocked"

    # names given on the command line by an agent: the run goes on, the question stays open, nothing is quoted for the director
    assert main(["start", str(demo_raw), "--speaker", "S1=Maren", "--speaker", "s2=Ana"]) == 0
    out = capsys.readouterr().out
    assert "ingest     reused" in out and "speakers   checked" in out and "rough      done" in out
    assert "Names supplied on the command line" in out
    assert [d["decision"] for d in _open(demo_raw, "Who is speaking")] == [asked[0]["decision"]]
    assert not _events(demo_raw, "decision.resolved") or all(
        e["data"]["decision"] != asked[0]["decision"] for e in _events(demo_raw, "decision.resolved"))
    task = next(t for t in Project.open(demo_raw).state().tasks.values() if t["fields"]["title"] == "Start: Who is speaking")
    assert task["fields"]["note"].startswith("Names supplied on the command line")


def test_names_typed_at_a_real_terminal_answer_the_question(demo_raw, monkeypatch, capsys):
    assert main(["start", str(demo_raw), "--through", "speakers"]) == 2
    asked = _open(demo_raw, "Who is speaking")[0]["decision"]
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: True)
    assert main(["start", str(demo_raw), "--through", "speakers", "--speaker", "S1=Maren", "--speaker", "s2=Ana"]) == 0
    resolved = [e for e in _events(demo_raw, "decision.resolved") if e["data"]["decision"] == asked]
    assert len(resolved) == 1 and resolved[0]["actor"] == "human"
    assert resolved[0]["data"]["quote"] == "--speaker S1=Maren --speaker s2=Ana"      # the literal arguments, nothing made up
    assert not _open(demo_raw, "Who is speaking")


def test_names_clear_a_diarisation_doubt_only_in_answer_to_a_stop(demo_raw):
    start.run(demo_raw, through="transcribe")
    tp = demo_raw / "work" / "transcript.json"
    t = json.loads(tp.read_text())
    t["diarization_check"] = {"agreement": 0.55}
    tp.write_text(json.dumps(t))
    meta = json.loads((demo_raw / "episode.json").read_text())
    meta["speakers"] = {"S1": {"name": "Maren"}, "S2": {"name": "Ana"}}     # names already there: still a doubt to raise
    (demo_raw / "episode.json").write_text(json.dumps(meta))
    with pytest.raises(start.NeedsDirector, match="agrees with the microphone channels only 55%"):
        start.run(demo_raw, through="speakers")
    r = start.run(demo_raw, through="speakers", speakers=NAMES)           # the answer to that stop
    assert r.status("speakers") == "checked"
    assert start.run(demo_raw, through="speakers").status("speakers") == "reused"   # the answer is remembered

    other = demo_raw.parent / "fresh-doubt"                                 # names with no stop recorded do not clear a doubt
    shutil.copytree(demo_raw, other)
    shutil.rmtree(other / ".ave")
    meta = json.loads((other / "episode.json").read_text())
    meta.pop("speaker_answer")
    (other / "episode.json").write_text(json.dumps(meta))
    with pytest.raises(start.NeedsDirector):
        start.run(other, through="speakers", speakers=NAMES)


def test_existing_stages_are_reused_not_redone(demo_done):
    edits, events = _edits(demo_done), len(_events(demo_done))
    r = start.run(demo_done)
    assert {s["status"] for s in r.steps} == {"reused"}
    assert _edits(demo_done) == edits
    assert len(_events(demo_done)) == events                 # a run that changes nothing writes nothing to the board
    assert r.decisions_new == [] and r.decisions_open

    for p in (demo_done / "exports").glob("rough.*.proxy.mp4"):
        p.unlink()
    r = start.run(demo_done)
    assert {s["step"]: s["status"] for s in r.steps if s["status"] != "reused"} == {"rough": "done"}
    assert _edits(demo_done) == edits


def test_an_existing_mapping_is_kept_and_only_new_cameras_are_added(demo_done):
    meta = json.loads((demo_done / "episode.json").read_text())
    kept = {k: v for k, v in meta["cameras"].items() if v["file"] == "raw/camA.mp4"}
    (key, cam), = kept.items()
    cam["role"] = "host"
    meta["cameras"] = {key: cam}                          # the director's mapping knows camera A only
    meta["speakers"]["S2"].pop("camera")
    (demo_done / "episode.json").write_text(json.dumps(meta))
    r = start.run(demo_done, through="map")
    assert r.status("map") == "done"
    after = json.loads((demo_done / "episode.json").read_text())
    assert after["cameras"][key] == cam and after["speakers"]["S1"]["camera"] == key
    new = [k for k, v in after["cameras"].items() if v["file"] == "raw/camB.mp4"]
    assert len(new) == 1 and new[0] != key and after["speakers"]["S2"]["camera"] == new[0]
    added = _open(demo_done, "Added new camera(s) to the existing mapping")
    assert len(added) == 1 and "Ana on camB.mp4" in added[0]["chosen"] and "kept as they were" in added[0]["reason"]
    assert start.run(demo_done, through="map").status("map") == "reused"


def test_a_redo_that_chooses_differently_supersedes_the_old_decision(demo_done):
    (demo_done / "policy").mkdir()
    (demo_done / "policy" / "trim.yaml").write_text("draft_min_confidence: 0.95\n")     # the same suggestion is now too weak
    meta = json.loads((demo_done / "episode.json").read_text())
    old = _open(demo_done, "Trim for the draft")
    assert len(old) == 1
    meta.pop("trim")
    (demo_done / "episode.json").write_text(json.dumps(meta))
    r = start.run(demo_done)
    assert r.status("trim") == "reused" and r.status("assemble") == "done"      # whole recording now: the draft is re-made
    trims = _open(demo_done, "Trim for the draft") + _open(demo_done, "Kept the whole recording")
    assert [d["summary"] for d in trims] == ["Kept the whole recording for the draft (main): the trim suggestion was weak"]
    assert "trim in at 0.20s" in trims[0]["options"]
    gone = [e for e in _events(demo_done, "decision.resolved") if e["data"]["decision"] == old[0]["decision"]]
    assert gone[0]["data"]["choice"] == "superseded" and gone[0]["data"]["superseded_by"] == trims[0]["decision"]
    assert "quote" not in gone[0]["data"] and gone[0]["actor"] == "claude"
    assert "trim" not in json.loads((demo_done / "episode.json").read_text())   # a weak end is never applied


def test_clips_only_run_when_asked(demo_raw):
    r = start.run(demo_raw, speakers=NAMES)
    assert "clips" not in [s["step"] for s in r.steps] and not (demo_raw / "work" / "clips.json").exists()
    r = start.run(demo_raw, clips=True)
    assert r.status("clips") == "done" and r.status("rough") == "reused"
    clips = json.loads((demo_raw / "work" / "clips.json").read_text())["clips"]
    assert all(c["status"] == "proposed" for c in clips)     # proposed only: choosing clips stays the director's


def test_final_refuses_unconfirmed_trims(demo_done, monkeypatch):
    from ave import final, trim
    ep = Episode(demo_done)
    with pytest.raises(SystemExit, match=r"only Claude's proposal for the draft.*ave trim set .* main --in"):
        final.build_final(ep, render=False, do_retranscribe=False)
    with pytest.raises(SystemExit, match="human act"):
        main(["trim", "set", str(demo_done), "main", "--in", "0.2"])
    assert _open(demo_done, "Trim for the draft")
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: True)
    assert main(["trim", "set", str(demo_done), "main", "--in", "0.2"]) == 0
    assert not _open(demo_done, "Trim for the draft")        # the director's act answers Claude's draft-trim question
    closed = [e for e in _events(demo_done, "decision.resolved") if e["actor"] == "human"]
    assert closed and closed[-1]["data"]["quote"] == f"ave trim set {demo_done} main --in 0.2"
    with pytest.raises(SystemExit, match="cut from Claude's draft trim"):  # the rough cut still uses the proposal
        final.build_final(ep, render=False, do_retranscribe=False)
    assert main(["trim", "propose", str(demo_done)]) == 0     # never overwrites the director's trim
    assert json.loads((demo_done / "episode.json").read_text())["trim"]["main"]["by"] == "human"
    r = start.run(demo_done)
    assert r.status("trim") == "reused" and r.status("assemble") == "done" and r.status("cleanup") == "done"
    assert trim.final_block(ep, None, json.loads(sorted((demo_done / "edits").glob("rough.v2*.json"))[0].read_text())) is None


def test_finish_sign_off_waits_for_the_directors_trim(demo_done, monkeypatch):
    log = Project.open(demo_done).log
    for a, b in (("Plan", "Draft"), ("Draft", "Refine")):
        log.append("stage.signed_off", artifact="art_ep", data={"from": a, "to": b, "quote": "ok"}, actor="human")
    app = AgentApp.for_board(demo_done, by="claude-code")
    args = {"request_id": "lock-1", "artifact": "art_ep", "stage": "Refine", "quote": "lock that"}
    with pytest.raises(ValueError, match="cannot move to Finish yet: a trim needs the director's confirmation.*ave trim set"):
        app.board("stage_sign_off", args)
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: True)
    assert main(["trim", "set", str(demo_done), "main", "--in", "0.2"]) == 0
    start.run(demo_done)                                      # re-cut from the confirmed trim
    assert app.board("stage_sign_off", {**args, "request_id": "lock-2"})["stage"] == "Finish"


def test_final_package_and_publish_warn_about_open_decisions(demo_done, monkeypatch, capsys):
    from ave import final
    w = start.open_decisions_warning(demo_done)
    assert w and "decision(s) Claude made alone are still waiting" in w and "Camera mapping" in w and "does not stop anything" in w
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: True)
    assert main(["trim", "set", str(demo_done), "main", "--in", "0.2"]) == 0
    start.run(demo_done)
    rep = final.build_final(Episode(demo_done), render=False, do_retranscribe=False)
    assert any("Claude made alone are still waiting" in x for x in rep["warnings"])     # warns, does not refuse
    capsys.readouterr()
    main(["package", str(demo_done), "--dry-run"])
    assert "Claude made alone are still waiting" in capsys.readouterr().err


def test_sessions_are_mapped_and_trim_gated_one_by_one(tmp_path):
    import fixtures
    root = tmp_path / "ms"
    fixtures.build_multisession(root, upto="sync")
    r = start.run(root, through="map", speakers={"S1": "Host", "S2": "Guest"})
    meta = json.loads((root / "episode.json").read_text())
    by_session = {}
    for k, v in meta["cameras"].items():
        by_session.setdefault(v["session"], set()).add(k)
    assert set(by_session) == {"s1", "s2"} and all(len(v) == 2 for v in by_session.values())
    for s in ("S1", "S2"):
        cams = meta["speakers"][s]["cameras"]
        assert set(cams) == {"s1", "s2"} and all(meta["cameras"][cams[sid]]["session"] == sid for sid in cams)
    assert len(_open(root, "Camera mapping for session s1")) == 1 and len(_open(root, "Camera mapping for session s2")) == 1
    assert r.status("map") == "done"

    from ave import trim
    ep = Episode(root)
    meta["trim"] = {"s1": {"in": 5.0, "by": "claude", "draft_only": True}, "s2": {"in": 5.0, "by": "human"}}
    (root / "episode.json").write_text(json.dumps(meta))
    assert trim.final_block(ep, "s2", None) is None
    assert "trim for s1" in trim.final_block(ep, "s1", None) and "ave trim set" in trim.final_block(ep, "s1", None)
    assert "trim for s1" in trim.final_block(ep, None, None)        # an unnamed (combined) edit answers to every trim


def test_loose_footage_is_refused_and_the_exit_code_says_so(tmp_path, capsys):
    folder = tmp_path / "loose"
    folder.mkdir()
    synth.build_media(tmp_path / "make")
    shutil.copy(tmp_path / "make" / "camA.mp4", folder / "camA.mp4")
    with pytest.raises(start.StartError, match="footage at its top level. Original footage is never moved"):
        start.run(folder)
    assert sorted(p.name for p in folder.iterdir()) == ["camA.mp4"]      # nothing written, nothing moved
    assert main(["start", str(folder)]) == 1
    assert "Stopped at ingest" in capsys.readouterr().out
