"""PROGRESS.md protocol: idempotent checklist, human text preserved, log routing, automatic trail, resume briefing."""

import json

import pytest

from ave import cli, progress, state
from ave.episode import Episode, write_json


def run(capsys, *argv):
    rc = cli.main([str(a) for a in argv])
    out = capsys.readouterr().out
    return rc, out


def jrun(capsys, *argv):
    rc, out = run(capsys, *argv)
    assert rc == 0
    return json.loads(out)


@pytest.fixture
def proj(tmp_path, capsys):
    d = tmp_path / "proj"
    jrun(capsys, "init-episode", d, "--title", "The Long Table 00")
    return Episode(d)


def section(text, heading):
    lines = text.split("\n")
    i = lines.index(heading)
    j = next((k for k in range(i + 1, len(lines)) if lines[k].startswith("## ")), len(lines))
    return "\n".join(lines[i + 1:j])


def test_init_writes_project_files_only_if_missing_and_no_symlinks(tmp_path, capsys):
    d = tmp_path / "p"
    d.mkdir()
    (d / "AGENTS.md").write_text("mine\n")
    r = jrun(capsys, "init-episode", d)
    assert r["project_files"]["created"] == ["PROGRESS.md", "CLAUDE.md"] and r["project_files"]["kept"] == ["AGENTS.md"]
    assert (d / "AGENTS.md").read_text() == "mine\n"
    assert not (d / "CLAUDE.md").is_symlink() and "AGENTS.md" in (d / "CLAUDE.md").read_text()
    again = jrun(capsys, "init-episode", d)
    assert again["project_files"]["created"] == []


def test_project_agents_md_points_at_the_repo(tmp_path, capsys):
    from ave import config
    jrun(capsys, "init-episode", tmp_path / "q")
    text = (tmp_path / "q" / "AGENTS.md").read_text()
    assert str(config.REPO_ROOT) in text and "ave" in text and "resume" in text and str(tmp_path / "q") in text


def test_progress_has_all_sections_and_header(proj):
    t = progress.path(proj).read_text()
    for h in (progress.H_CHECK, progress.H_DEC, progress.H_Q, progress.H_NOTES, progress.H_LOG):
        assert h in t
    assert "The Long Table 00" in t and str(proj.root) in t and "Resume " in t
    assert progress.START in t and progress.END in t


def test_checklist_regeneration_is_idempotent_and_reflects_disk(proj, capsys):
    p = progress.path(proj)
    jrun(capsys, "progress", proj.root)
    first = p.read_text()
    r = jrun(capsys, "progress", proj.root)
    assert not r["checklist_changed"] and p.read_text() == first
    assert "- [ ] Probe footage (`ingest`)" in first and "**Next:** Probe footage" in first
    proj.work.mkdir(parents=True, exist_ok=True)
    write_json(proj.work / "media.json", {"files": []})
    r = jrun(capsys, "progress", proj.root)
    assert r["checklist_changed"] and "ingest" in r["done"]
    assert "- [x] Probe footage (`ingest`)" in p.read_text()


def test_human_text_outside_markers_and_inside_sections_survives(proj, capsys):
    p = progress.path(proj)
    t = p.read_text()
    t = t.replace(progress.H_DEC + "\n", progress.H_DEC + "\n\n- HUMAN: we shoot in 4K, keep it.\n", 1)
    t = "Intro paragraph written by a human.\n\n" + t + "\n## My own section\n\nhands off\n"
    p.write_text(t)
    jrun(capsys, "log", proj.root, "chose podcast style because it is quiet", "--kind", "decision")
    jrun(capsys, "log", proj.root, "which intro take?", "--kind", "question")
    write_json(proj.work / "media.json", {"files": []})
    jrun(capsys, "progress", proj.root)
    out = p.read_text()
    assert out.startswith("Intro paragraph written by a human.")
    assert "- HUMAN: we shoot in 4K, keep it." in out and "## My own section\n\nhands off" in out
    assert "chose podcast style" in section(out, progress.H_DEC)
    assert out.index("HUMAN: we shoot") < out.index("chose podcast style")        # appended after the human's line


def test_removed_markers_are_restored_under_the_heading(proj, capsys):
    p = progress.path(proj)
    p.write_text(p.read_text().replace(progress.START, "").replace(progress.END, ""))
    jrun(capsys, "progress", proj.root)
    t = p.read_text()
    assert progress.START in section(t, progress.H_CHECK) and progress.END in section(t, progress.H_CHECK)


def test_log_kinds_route_to_the_right_sections(proj, capsys):
    p = progress.path(proj)
    jrun(capsys, "log", proj.root, "plain note")
    jrun(capsys, "log", proj.root, "use cam A as wide", "--kind", "decision", "--step", "map")
    q = jrun(capsys, "log", proj.root, "who is the guest?", "--kind", "question")
    jrun(capsys, "log", proj.root, "drive unmounted", "--kind", "blocker")
    t = p.read_text()
    log, dec, qs = section(t, progress.H_LOG), section(t, progress.H_DEC), section(t, progress.H_Q)
    assert "use cam A as wide" in dec and "use cam A as wide" in log and "[decision] [map]" in log
    assert f"{q['question']}. [ ] who is the guest?" in qs and "who is the guest?" in log
    assert "plain note" in log and "plain note" not in dec + qs
    assert "[blocker] drive unmounted" in log and "drive unmounted" not in dec + qs
    assert q["question"] == 1


def test_done_refreshes_the_checklist(proj, capsys):
    write_json(proj.work / "media.json", {"files": []})
    jrun(capsys, "log", proj.root, "footage probed", "--kind", "done", "--step", "ingest")
    t = progress.path(proj).read_text()
    assert "- [x] Probe footage (`ingest`)" in t and "[done] [ingest] footage probed" in t


def test_questions_are_numbered_and_resolvable(proj, capsys):
    jrun(capsys, "log", proj.root, "first?", "--kind", "question")
    jrun(capsys, "log", proj.root, "second?", "--kind", "question")
    jrun(capsys, "log", proj.root, "yes, A", "--resolve", "1")
    t = progress.path(proj).read_text()
    qs = section(t, progress.H_Q)
    assert "1. [x] first?" in qs and "2. [ ] second?" in qs
    assert progress.open_questions(proj) == [l for l in progress.open_questions(proj) if l.startswith("2. [ ] second?")]
    assert "Q1: yes, A" in section(t, progress.H_LOG)
    with pytest.raises(SystemExit, match="no open question 1"):
        progress.log(proj, "again", resolve=1)


def test_mark_stage_appends_exactly_one_log_line(proj):
    before = progress.recent_log(proj, 100)
    proj.mark_stage("ingest", files=3)
    after = progress.recent_log(proj, 100)
    assert len(after) == len(before) + 1
    assert "stage ingest done (files=3)" in after[-1] and "[done] [ingest]" in after[-1]


def test_set_review_logs_and_legacy_episodes_without_progress_are_untouched(tmp_path, proj):
    state.set_review(proj, "assembly ready")
    assert "waiting on the human: assembly ready" in progress.recent_log(proj, 1)[0]
    legacy = Episode(tmp_path / "legacy")
    legacy.create()
    (legacy.root / "PROGRESS.md").unlink(missing_ok=True)
    legacy.mark_stage("ingest")
    state.set_review(legacy, "x")
    assert not (legacy.root / "PROGRESS.md").exists()


def test_learn_records_proposed_status_and_can_be_applied(proj, capsys):
    r = jrun(capsys, "learn", proj.root, "ASR drops names", "--skill", "ave-captions", "--change", "add a glossary step")
    assert r["status"] == "proposed" and r["id"] == "F1"
    jrun(capsys, "learn", proj.root, "second insight")
    t = progress.path(proj).read_text()
    notes = section(t, progress.H_NOTES)
    assert "F1 [proposed]" in notes and "skill: `ave-captions`" in notes and "proposed change: add a glossary step" in notes
    assert "F2 [proposed]" in notes and "learned F1" in section(t, progress.H_LOG)
    assert len(jrun(capsys, "learn", proj.root, "--list")["proposed"]) == 2
    jrun(capsys, "learn", proj.root, "--apply", "F1", "--commit", "abc123")
    notes = section(progress.path(proj).read_text(), progress.H_NOTES)
    assert "F1 [applied abc123]" in notes and "F2 [proposed]" in notes


def test_resume_shows_open_questions_and_last_five_log_lines(proj, capsys):
    jrun(capsys, "log", proj.root, "need the guest name", "--kind", "question")
    for i in range(7):
        jrun(capsys, "log", proj.root, f"step number {i}")
    rc, out = run(capsys, "resume", proj.root)
    assert rc == 0 and "Open questions for the human:" in out and "1. [ ] need the guest name" in out
    assert "step number 6" in out and "step number 2" in out and "step number 1" not in out
    assert "PROGRESS.md" in out


def test_resume_without_progress_file_says_how_to_create_it(tmp_path, capsys):
    e = Episode(tmp_path / "old")
    e.create()
    rc, out = run(capsys, "resume", e.root)
    assert "No PROGRESS.md yet" in out
    jrun(capsys, "progress", e.root)
    assert (e.root / "PROGRESS.md").exists()


def test_adopt_command_on_an_existing_numbered_folder(tmp_path, capsys):
    d = tmp_path / "00"
    (d / "02_FOOTAGE").mkdir(parents=True)
    r = jrun(capsys, "adopt", d)
    assert r["layout"] == "numbered" and (d / "PROGRESS.md").exists()
    assert (d / "01_PROJECT" / "work").is_dir()
