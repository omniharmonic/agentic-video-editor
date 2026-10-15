"""`ave demo`, the resource locator and the friendlier `ave doctor`."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ave import cli, doctor, resources
from ave.commands import demo
from conftest import needs_ffmpeg


@needs_ffmpeg
def test_demo_runs_the_whole_pipeline(tmp_path, capsys):
    root = tmp_path / "demo"
    assert cli.main(["demo", str(root)]) == 0
    out = capsys.readouterr().out
    for n in range(1, 12):
        assert f"[{n}/11]" in out
    assert "OK" in out and "camB offset found = 1.000" in out
    assert (root / "edits" / "assembly.v1.json").exists()
    assert (root / "edits" / "rough.v1.json").exists()
    mp4 = list((root / "exports").glob("rough.v1.proxy.mp4"))
    assert mp4 and mp4[0].stat().st_size > 10_000
    # raw footage was only written by the generator, and the transcript says what it is
    assert json.loads((root / "work" / "transcript.json").read_text())["engine"]["asr"] == "synthetic"
    # re-running on the same folder works, and --force rebuilds from scratch
    assert cli.main(["demo", str(root), "--force"]) == 0


def test_demo_refuses_a_folder_that_is_not_an_episode(tmp_path, capsys):
    (tmp_path / "precious.txt").write_text("keep me")
    assert cli.main(["demo", str(tmp_path)]) == 1
    assert "not an ave episode" in capsys.readouterr().err
    assert (tmp_path / "precious.txt").read_text() == "keep me"


def test_demo_without_ffmpeg_says_how_to_install_it(tmp_path, monkeypatch, capsys):
    from ave import config
    monkeypatch.setattr(config, "binary", lambda name: None)
    assert cli.main(["demo", str(tmp_path / "d")]) == 1
    err = capsys.readouterr().err
    assert "ffmpeg" in err and "install" in err and not (tmp_path / "d").exists()


def test_demo_transcript_note_is_honest_without_asr(monkeypatch):
    from ave import config
    monkeypatch.setattr(config, "fluidaudio_cli", lambda: None)
    monkeypatch.setattr(config, "whisper_cli", lambda: None)
    note = demo._transcript_note()
    assert "synthetic" in note and "no ASR engine installed" in note


def _py(code: str, **env) -> str:
    e = {**os.environ, **env}
    e.pop("AVE_HOME", None) if "AVE_HOME" not in env else None
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=e).stdout.strip()


def test_resources_find_the_checkout():
    assert (resources.home() / "config" / "cleanup.default.yaml").exists()
    assert (resources.home() / "skills").is_dir() and (resources.home() / "motion-kit" / "brand" / "tokens.json").exists()


def test_ave_home_override(tmp_path):
    (tmp_path / "config").mkdir()
    out = _py("from ave import resources; print(resources.home())", AVE_HOME=str(tmp_path))
    assert out == str(tmp_path.resolve())
    bad = subprocess.run([sys.executable, "-c", "from ave import resources; resources.home()"], capture_output=True, text=True,
                         env={**os.environ, "AVE_HOME": str(tmp_path / "nope")})
    assert bad.returncode != 0 and "AVE_HOME" in bad.stderr


def test_packaged_data_is_used_when_there_is_no_checkout(tmp_path, monkeypatch):
    """Simulate an installed wheel: ave/_data holds the data and the package is not inside a checkout."""
    data = tmp_path / "pkg" / "_data"
    (data / "config").mkdir(parents=True)
    monkeypatch.delenv("AVE_HOME", raising=False)
    monkeypatch.setattr(resources, "_PKG", tmp_path / "pkg")
    resources._home.cache_clear()
    try:
        assert resources.checkout_root() is None
        assert resources.home() == data
        assert not resources.is_checkout()
        assert resources.source().startswith("packaged data")
    finally:
        resources._home.cache_clear()


def test_doctor_is_grouped_and_has_a_verdict(capsys):
    code = doctor.run()
    out = capsys.readouterr().out
    assert "Required" in out and "For real footage" in out and "Optional" in out
    assert out.strip().splitlines()[-1].startswith(("READY", "NOT READY"))
    assert code == (0 if shutil.which("ffmpeg") else 1)


def test_doctor_optional_gaps_never_fail_and_every_gap_has_a_fix():
    checks = doctor._checks()
    for c in checks:
        if c.status in (doctor.WARN, doctor.FAIL):
            assert c.fix, f"{c.name} has no fix command"
        if c.group != doctor.REQUIRED:
            assert c.status != doctor.FAIL


def test_doctor_verdict_levels():
    ok = doctor.Check(doctor.OK, "x", "", group=doctor.REQUIRED)
    bad = doctor.Check(doctor.FAIL, "ffmpeg", "missing", "fix", group=doctor.REQUIRED)
    asr = doctor.Check(doctor.WARN, "A transcription engine", "none", "fix", False, doctor.FOOTAGE)
    assert doctor.verdict([bad])[1] == 1 and doctor.verdict([bad])[0].startswith("NOT READY")
    line, code = doctor.verdict([ok, asr])
    assert code == 0 and "transcription engine" in line
    assert doctor.verdict([ok])[0].startswith("READY")
