"""`uv build` (sdist, then a wheel built FROM that sdist) must work and carry the runtime data.

Regression: the sdist once omitted skills/ (hatch's walker met the .agents/skills symlink first), so plain `uv build` and
`pip install x.tar.gz` died with "Forced include not found".
"""

import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("uv") is None or not (ROOT / "pyproject.toml").is_file(),
                                reason="needs uv and a source checkout")

NEEDED = ("config/", "skills/", "motion-kit/", "content/")


@pytest.fixture(scope="module")
def dist(tmp_path_factory):
    out = tmp_path_factory.mktemp("dist")
    r = subprocess.run(["uv", "build", "--out-dir", str(out), str(ROOT)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    return out


def test_sdist_carries_forced_includes(dist):
    sdist = next(dist.glob("*.tar.gz"))
    with tarfile.open(sdist) as t:
        names = [n.split("/", 1)[1] for n in t.getnames() if "/" in n]
    for d in NEEDED:
        assert any(n.startswith(d) for n in names), f"sdist is missing {d}"
    assert sum(n.startswith("skills/") and n.endswith("SKILL.md") for n in names) >= 20
    assert not any(n.startswith((".agents/", ".claude/")) for n in names)
    assert "AGENTS.md" in names and "pyproject.toml" in names


def test_wheel_from_sdist_has_data(dist):
    whl = next(dist.glob("*.whl"))
    with zipfile.ZipFile(whl) as z:
        names = z.namelist()
    for d in NEEDED:
        assert any(n.startswith(f"ave/_data/{d}") for n in names), f"wheel is missing ave/_data/{d}"
    assert "ave/_data/AGENTS.md" in names
    assert any(n.endswith("ave/_data/skills/ave-pipeline/SKILL.md") for n in names)
