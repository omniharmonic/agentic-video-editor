"""Fixtures for tests/sync: a private HOME, a built config, one Syncthing home and a pair of homes on loopback."""

from __future__ import annotations

import shutil

import pytest

from ave.syncthing import Peer
from ave.syncthing import config as C

from . import harness as H

SID = {k: "-".join([k * 7] * 8) for k in "ABCD"}


@pytest.fixture(autouse=True)
def private_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def two_member_project(tmp_path):
    root = tmp_path / "proj"
    H.write_project_json(root, "prj_TEST", "The Long Table", "flat",
                         [{"person": "maren", "devices": ["dev_A"], "media": "full"},
                          {"person": "tomas", "devices": ["dev_B"], "media": "proxies"}], "dev_A")
    return root


@pytest.fixture
def cfg(two_member_project):
    import json
    meta = json.loads((two_member_project / ".ave" / "project.json").read_text())
    peers = [Peer("dev_B", SID["B"], "100.64.0.2", "tomas"), Peer("dev_C", SID["C"], "100.64.0.3", "stranger")]
    return C.build_config(SID["A"], "100.64.0.1", [(two_member_project, meta)], peers, local_name="maren")


@pytest.fixture
def short(monkeypatch):
    d = H.short_dir()
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def short_home(monkeypatch, short):
    """HOME in a short folder, so the app's default Syncthing home is used and its socket path fits."""
    monkeypatch.setenv("HOME", str(short))
    return short


@pytest.fixture
def sync_home(short_home):
    h = H.make_home(None, "solo", "dev_SOLO")
    h.manager.start()
    yield h
    h.manager.stop()


@pytest.fixture
def sync_pair(short):
    a = H.make_home(short, "maren", "dev_A")
    b = H.make_home(short, "tomas", "dev_B")
    a.manager.start()
    b.manager.start()
    yield a, b
    a.manager.stop()
    b.manager.stop()


def pytest_collection_modifyitems(items):
    """Every sync test has a bound: a Syncthing wait that never ends fails the test instead of stalling the run."""
    for item in items:
        if "/tests/sync/" in str(item.fspath) and not item.get_closest_marker("timeout"):
            item.add_marker(pytest.mark.timeout(300))
