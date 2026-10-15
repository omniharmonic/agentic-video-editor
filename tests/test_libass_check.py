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
    assert checks and checks[0].status == doctor.WARN and checks[0].fix
