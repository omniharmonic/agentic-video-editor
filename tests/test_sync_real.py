"""Real-footage validation (field notes N2/N3): skipped unless the 16 kHz analysis audio of the first real episode is present.

Measured truth (offset = where the camera's t=0 sits on the master timeline; drift = camera runs slow vs the master):
s1A 4.271 s +50 ppm | s1B 4.156 s +24 ppm | s2A 7119.408 s +85 ppm | s2B 7118.827 s +46 ppm.
Session 2 is 2 hours into the master, i.e. past half the envelope FFT, so this also guards the lag-wraparound fix.
"""
import os
from pathlib import Path

import pytest

from ave import sync

_NONE = Path("/nonexistent/ave-real-footage-not-configured")


def _real(var: str) -> Path:
    """Optional real-footage inputs come from an env var; unset means the dependent tests skip (nothing is ever probed on a hard-coded drive)."""
    v = os.environ.get(var)
    return Path(v).expanduser() if v else _NONE

# AVE_TEST_REAL_SYNC_DIR: a folder with master.wav, s1A.wav, s1B.wav, s2A.wav, s2B.wav (analysis audio of one real episode)
D = _real("AVE_TEST_REAL_SYNC_DIR")
TRUTH = {"s1A": (4.271, 50.0), "s1B": (4.156, 24.0), "s2A": (7119.408, 85.0), "s2B": (7118.827, 46.0)}
pytestmark = pytest.mark.skipif(not all((D / f"{n}.wav").exists() for n in ["master", *TRUTH]), reason="real episode analysis audio not present")


@pytest.fixture(scope="module")
def master_env():
    return sync.envelope(D / "master.wav")


@pytest.mark.parametrize("cam", sorted(TRUTH))
def test_real_cameras_reproduce_measured_offset_and_drift(cam, master_env):
    r = sync.sync_pair(D / "master.wav", D / f"{cam}.wav", ref_env=master_env)
    off, ppm = TRUTH[cam]
    assert abs(r["start_offset"] - off) < 0.030, r
    assert abs(r["drift_ppm"] - ppm) < 10.0, r
    assert r["psr_min"] > 40 and len(r["points"]) == 5
