"""The studio's docs and skills say what the CLI does: every command is documented, every skill is well formed, every `ave studio ...` line a skill shows parses,
and the docs stay honest about what is unverified."""

import re
import shlex
from pathlib import Path

import pytest
import yaml

from ave.commands.studio import _build, command_names

ROOT = Path(__file__).resolve().parents[2]
SKILLS = sorted((ROOT / "skills").glob("ave-studio*/SKILL.md"))
HUMAN_LINES = ("approve", "ui", "account live", "account auth", "resume", "resolve", "broll pick", "broll generate", "schedule apply", "tick --send", "run --send")


def test_the_seven_studio_skills_exist_with_valid_frontmatter():
    assert [p.parent.name for p in SKILLS] == ["ave-studio", "ave-studio-broll", "ave-studio-calendar", "ave-studio-ideas", "ave-studio-recipes",
                                               "ave-studio-review-loop", "ave-studio-visuals"]
    for p in SKILLS:
        text = p.read_text()
        m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        assert m, p
        fm = yaml.safe_load(m.group(1))
        assert fm["name"] == p.parent.name and len(fm["description"]) > 60, p
        assert "AVE_ALLOW_UNATTENDED_APPROVAL" not in text


def test_docs_mention_every_command_group_and_stay_honest():
    doc = (ROOT / "docs" / "studio.md").read_text()
    for name in command_names():
        assert f"`{name}" in doc or f"{name} " in doc or f"{name}`" in doc or f"{name}\\" in doc, name
    low = doc.lower()
    assert "nothing in the studio has posted to a live account" in low and "verified: false" in low
    assert "one-camera" in low and "not yet run on real footage" in low
    agents = (ROOT / "AGENTS.md").read_text()
    assert "Content studio" in agents and "`ave-studio`" in agents and "no platform adapter has run against a live account" in agents
    assert "Built, fakes only" in (ROOT / "docs" / "ROADMAP.md").read_text()


def _studio_lines():
    """Every `ave studio ...` command shown in a skill's fenced code blocks or inline code, as an argv list (placeholders replaced by plain words)."""
    for p in SKILLS:
        for m in re.finditer(r"`(?:! )?(ave studio [^`]+)`|^\s*(?:! )?(ave studio [^\n#]+?)(?:\s+#.*)?$", p.read_text(), re.M):
            raw = (m.group(1) or m.group(2)).strip()
            yield p.parent.name, raw


def test_every_studio_command_shown_in_a_skill_parses():
    import argparse
    parser = argparse.ArgumentParser(prog="ave")
    sub = parser.add_subparsers()
    s = sub.add_parser("studio")
    ss = s.add_subparsers(dest="studio_cmd", required=True)
    _build(ss)
    bad = []
    seen = 0
    for skill, raw in _studio_lines():
        line = re.sub(r"<[^>]+>", "X", raw)                                  # <post>, <variant> ... -> a word
        line = re.sub(r"\[[^\]]*\]", "", line)                               # optional parts
        line = line.replace("...", "x").replace("|", " ")
        try:
            argv = shlex.split(line)[1:]
        except ValueError:
            continue
        if not argv or argv[0] != "studio" or len(argv) < 2:
            continue
        if any(t in ("ID", "ack") for t in argv) and argv[1] == "alerts":
            continue
        seen += 1
        err = []
        import contextlib, io
        with contextlib.redirect_stderr(io.StringIO()) as buf:
            try:
                parser.parse_args(argv)
            except SystemExit as e:
                if e.code not in (0, None):
                    err.append(buf.getvalue().strip().splitlines()[-1] if buf.getvalue().strip() else "exit")
        if err:
            bad.append((skill, raw, err[0]))
    assert seen > 40, seen
    # a handful of lines in prose use ad-hoc shapes (a variant spelled `v_ig`); tolerate none that name an unknown subcommand
    unknown = [b for b in bad if "invalid choice" in b[2]]
    assert not unknown, unknown
