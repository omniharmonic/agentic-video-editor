"""A missing libass must be reported clearly (and by `ave doctor`), not as a filtergraph parse error."""
import pytest

from ave import doctor, ffutil


def test_require_libass_gives_an_actionable_error(monkeypatch):
    monkeypatch.setattr(ffutil, "has_filter", lambda name: False)
    with pytest.raises(ffutil.FFmpegError, match="without libass") as e:
        ffutil.require_libass()
    assert "ffmpeg-full" in str(e.value) or "apt-get" in str(e.value) or "ffmpeg.org" in str(e.value)


def test_require_libass_passes_when_the_filter_exists(monkeypatch):
    monkeypatch.setattr(ffutil, "has_filter", lambda name: True)
    ffutil.require_libass()


def test_doctor_flags_a_missing_libass(monkeypatch):
    monkeypatch.setattr(ffutil, "has_filter", lambda name: False)
    monkeypatch.setattr(doctor.config, "binary", lambda name: "/usr/bin/" + name)
    checks = [c for c in doctor._checks() if "libass" in c.name]
    assert checks and checks[0].status == doctor.FAIL and checks[0].fix
    assert checks[0].group == doctor.CAPTIONS
    assert "ffmpeg-full" in checks[0].fix or "apt-get" in checks[0].fix or "ffmpeg.org" in checks[0].fix


def test_missing_libass_never_reads_ready(monkeypatch, capsys):
    monkeypatch.setattr(ffutil, "has_filter", lambda name: False)
    monkeypatch.setattr(doctor.config, "binary", lambda name: "/usr/bin/" + name)
    checks = doctor._checks()
    line, code = doctor.verdict(checks)
    assert code == 0  # the basic flow (demo, assembly, rough cut) still works
    assert line.startswith("PARTLY READY") and "READY for real footage" not in line and "libass" in line
    assert doctor.run() == 0
    out = capsys.readouterr().out
    assert "Required for captions & shorts" in out and "READY for real footage" not in out


def test_libass_present_is_ok_and_group_is_shown(monkeypatch):
    monkeypatch.setattr(ffutil, "has_filter", lambda name: True)
    monkeypatch.setattr(doctor.config, "binary", lambda name: "/usr/bin/" + name)
    c = [c for c in doctor._checks() if "libass" in c.name][0]
    assert c.status == doctor.OK and c.group == doctor.CAPTIONS
    assert not doctor.verdict(doctor._checks())[0].startswith("PARTLY")
