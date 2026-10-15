"""Every relative link in the repository's Markdown files must point at a file or folder that exists."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".venv", "venv", "node_modules", ".pytest_cache", "vendor", ".claude", ".agents", "__pycache__", ".git", "work", "episodes"}

LINK = re.compile(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
FENCE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE = re.compile(r"`[^`\n]*`")
REF_DEF = re.compile(r"^\s{0,3}\[[^\]]+\]:\s+(\S+)", re.M)


def markdown_files() -> list[Path]:
    return [p for p in sorted(ROOT.rglob("*.md")) if not SKIP_DIRS.intersection(p.relative_to(ROOT).parts)]


def links_in(text: str) -> list[str]:
    """Link targets (inline and reference-style) outside fenced code blocks and inline code."""
    body, in_fence = [], False
    for line in text.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            body.append(INLINE_CODE.sub("", line))
    joined = "\n".join(body)
    return LINK.findall(joined) + REF_DEF.findall(joined)


def is_local(target: str) -> bool:
    return not (re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith("#") or target.startswith("//"))


def broken_links(path: Path, root: Path = ROOT) -> list[str]:
    bad = []
    for target in links_in(path.read_text(encoding="utf-8")):
        if not is_local(target):
            continue
        rel = unquote(target.split("#", 1)[0].split("?", 1)[0])
        if not rel:
            continue
        dest = (root / rel.lstrip("/")) if rel.startswith("/") else (path.parent / rel)
        if not dest.exists():
            bad.append(target)
    return bad


def test_there_are_markdown_files_to_check():
    names = {p.name for p in markdown_files()}
    assert {"AGENTS.md", "ROADMAP.md"} <= names


@pytest.mark.parametrize("path", markdown_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_resolve(path):
    assert broken_links(path) == [], f"{path.relative_to(ROOT)} has links to missing files"


def test_the_checker_itself_finds_a_broken_link(tmp_path):
    f = tmp_path / "a.md"
    f.write_text("[ok](a.md) [bad](nope.md#x) `[code](missing1.md)` [web](https://example.org/x) [anchor](#top)\n"
                 "```\n[fenced](missing2.md)\n```\n[ref]: missing3.md\n")
    assert broken_links(f, tmp_path) == ["nope.md#x", "missing3.md"]
