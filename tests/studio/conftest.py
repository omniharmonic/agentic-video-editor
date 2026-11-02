"""Fixtures for tests/studio: the offline guard (spec 10.1), in-memory keyring and HOME isolation, and the builders."""

from __future__ import annotations

import socket

import pytest
from publish_helpers import isolated_secrets  # noqa: F401  (autouse: tmp HOME, MemKeyring, no AVE_* env)

from studio_helpers import FakeClock, build_studio


@pytest.fixture(autouse=True)
def offline_guard(monkeypatch):
    """Only loopback and unix sockets may connect. Anything else fails the test (no real network, ever)."""
    real = socket.socket.connect

    def guarded(self, address, *a, **kw):
        host = address[0] if isinstance(address, tuple) else address
        if self.family == getattr(socket, "AF_UNIX", None) or host in ("127.0.0.1", "::1", "localhost"):
            return real(self, address, *a, **kw)
        raise AssertionError(f"network access attempted in a studio test: {address!r}")

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def bare(tmp_path):
    """An empty, valid studio (no accounts, no posts)."""
    from ave.studio.project import StudioProject
    p = StudioProject(tmp_path / "bare")
    p.create(title="Bare", tz="UTC")
    return p


@pytest.fixture
def built(tmp_path, clock):
    return build_studio(tmp_path, clock=clock)


@pytest.fixture
def sp(built):
    return built.sp


@pytest.fixture
def studio(built):
    return built.studio


@pytest.fixture
def synthetic_final_asr(monkeypatch,tmp_path):
    """Explicit synthetic ASR boundary, injected only by test process/worker."""
    import sys
    from ave.studio import episode_workshop as E,workbench
    config={'engine':'synthetic-test-only','binary_sha256':'a'*64,'model_sha256':'b'*64,'model_dir':'fixture','language':None,'diarize':False}
    words=[{'w':'Hello','start':.2,'end':.5},{'w':'world','start':1.1,'end':1.4}]
    monkeypatch.setattr(E,'final_asr_configuration',lambda:config)
    monkeypatch.setattr(E,'final_asr',lambda ep,media_path,work_dir,configuration:{'engine':{'asr':'synthetic-fixture'},'words':words,'warnings':['Synthetic ASR fixture, not a real transcription']})
    script=tmp_path/'synthetic_final_worker.py'
    script.write_text("from ave.studio import episode_workshop as E, workbench_worker as W\n"
                      "E.final_asr_configuration=lambda:"+repr(config)+"\n"
                      "E.final_asr=lambda ep,media_path,work_dir,configuration:{'engine':{'asr':'synthetic-fixture'},'words':"+repr(words)+",'warnings':['Synthetic ASR fixture, not a real transcription']}\n"
                      "raise SystemExit(W.main())\n")
    monkeypatch.setattr(workbench,'worker_argv',lambda root,jid:[sys.executable,str(script),str(root),jid])
    return config
