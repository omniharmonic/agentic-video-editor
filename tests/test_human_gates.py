"""Human-only CLI acts go through ave.humanpresence.require_human; there is no unattended switch."""

import json
import sys

import pytest

from ave import humanpresence, learning
from ave.cli import main


class _Tty:
    def isatty(self):
        return True


def test_requires_stdin_and_stdout_tty(monkeypatch):
    for k in humanpresence.AGENT_MARKERS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr(sys, "stdout", _Tty())
    assert humanpresence.is_human_terminal()
    monkeypatch.setattr(sys, "stdout", object())          # no isatty at all
    assert not humanpresence.is_human_terminal()


@pytest.mark.parametrize("marker", humanpresence.AGENT_MARKERS)
def test_agent_markers_refuse_even_with_a_tty(monkeypatch, marker):
    for k in humanpresence.AGENT_MARKERS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr(sys, "stdout", _Tty())
    monkeypatch.setenv(marker, "1")
    assert not humanpresence.is_human_terminal()
    with pytest.raises(SystemExit, match=r"ave something now"):
        humanpresence.require_human("a thing", "ave something now")


@pytest.mark.parametrize("argv,line", [
    (["sequence", "approve", "EP"], "ave sequence approve EP"),
    (["trim", "set", "EP", "s1", "--in", "5", "--out", "99"], "ave trim set EP s1 --in 5.0 --out 99.0"),
    (["learn", "taste", "apply", "EP"], "ave learn taste apply EP"),
    (["publish", "approve", "EP", "item1"], "ave publish approve EP item1"),
    (["content", "canva-approve", "EP", "job1"], "ave content canva-approve EP job1"),
])
def test_human_only_commands_refuse_and_print_the_exact_line(argv, line):
    with pytest.raises(SystemExit) as e:
        main(argv)
    assert line in str(e.value) and "human act" in str(e.value)


def test_trim_set_writes_only_for_a_human(synth_episode, monkeypatch, capsys):
    ep = synth_episode
    with pytest.raises(SystemExit, match="human act"):
        main(["trim", "set", str(ep.root), "main", "--in", "1", "--out", "25"])
    assert not (json.loads(ep.meta_path.read_text()).get("trim"))
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: True)
    assert main(["trim", "set", str(ep.root), "main", "--in", "1", "--out", "25"]) == 0
    assert json.loads(ep.meta_path.read_text())["trim"]


def test_sequence_approve_is_gated(synth_episode):
    ep = synth_episode
    with pytest.raises(SystemExit, match="human act"):
        main(["sequence", "approve", str(ep.root)])
    assert not (ep.work / "sequence.approval.json").exists()


def test_open_decisions_record_provenance(synth_episode, monkeypatch):
    ep = synth_episode
    assert learning.record_decision(ep, "c1", "keep")["via"] == "notty"
    monkeypatch.setattr(humanpresence, "is_human_terminal", lambda: True)
    assert learning.record_decision(ep, "c2", "keep")["via"] == "tty"
    assert learning.record_decision(ep, "c3", "keep", source="ui")["via"] == "ui"
    lines = [json.loads(x) for x in learning.ledger_path(ep).read_text().splitlines()]
    assert [x["via"] for x in lines] == ["notty", "tty", "ui"]


def test_the_unattended_switch_is_gone_from_source():
    from pathlib import Path
    root = Path(__file__).parents[1]
    for p in list((root / "src").rglob("*.py")) + list((root / "skills").rglob("*.md")):
        assert "AVE_ALLOW_UNATTENDED_APPROVAL" not in p.read_text(), p
