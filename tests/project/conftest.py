"""Fixtures for tests/project: a private HOME (so device.json is never the real one) and a fresh project."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def private_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def project(tmp_path):
    from ave.project import Project
    return Project.create(tmp_path / "proj", "The Long Table", person="maren")
