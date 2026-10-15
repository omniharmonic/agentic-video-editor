"""Shared fixtures/fakes for the publish tests. No network, no real keychain, no real HOME."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend

from ave.episode import Episode
from ave.publish import safety


class MemKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        self.d = {}

    def get_password(self, service, username):
        return self.d.get((service, username))

    def set_password(self, service, username, password):
        self.d[(service, username)] = password

    def delete_password(self, service, username):
        self.d.pop((service, username), None)


@pytest.fixture(autouse=True)
def isolated_secrets(monkeypatch, tmp_path):
    for k in list(__import__("os").environ):
        if k.startswith("AVE_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    old = keyring.get_keyring()
    mem = MemKeyring()
    keyring.set_keyring(mem)
    safety._KNOWN_SECRETS.clear()
    yield mem
    keyring.set_keyring(old)
    safety._KNOWN_SECRETS.clear()


def _mk(path: Path, size: str, secs: int, color: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s={size}:r=10:d={secs}",
                    "-f", "lavfi", "-i", f"sine=f=300:d={secs}", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(path)], check=True)


@pytest.fixture(scope="session")
def video_pool(tmp_path_factory):
    d = tmp_path_factory.mktemp("pubvideos")
    _mk(d / "vertical.mp4", "360x640", 4, "blue")
    _mk(d / "wide.mp4", "640x360", 4, "red")
    _mk(d / "wide_long.mp4", "640x360", 200, "green")
    return d


@pytest.fixture
def ep(tmp_path):
    e = Episode(tmp_path / "ep")
    e.create("Test Episode")
    return e


@pytest.fixture
def vertical(video_pool, ep):
    dst = ep.exports / "shorts" / "clip1.mp4"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(video_pool / "vertical.mp4", dst)
    return dst


@pytest.fixture
def wide(video_pool, ep):
    dst = ep.exports / "final.mp4"
    shutil.copy(video_pool / "wide.mp4", dst)
    return dst


# ---- fakes
class FakeResp:
    def __init__(self, data=None, status=200):
        self._d = data if data is not None else {}
        self.status_code = status

    def json(self):
        return self._d


class FakeSession:
    """handler(method, url, kwargs) -> FakeResp | raises. Records every call."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def _do(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.handler(method, url, kw)

    def post(self, url, **kw):
        return self._do("POST", url, **kw)

    def get(self, url, **kw):
        return self._do("GET", url, **kw)

    def request(self, method, url, **kw):
        return self._do(method, url, **kw)


class RecordingPublisher:
    """A Publisher stand-in for queue tests."""

    def __init__(self, name="youtube", behavior="ok", recover=None, problems=None):
        self.name = name
        self.behavior = behavior
        self._recover = recover
        self.problems = problems or []
        self.publish_calls = 0

    def preflight(self, item, video):
        return list(self.problems)

    def ready_problems(self):
        return []

    def publish(self, item, video, progress):
        from ave.publish.adapters.base import PublishError
        self.publish_calls += 1
        progress({"upload_id": "u1"})
        if self.behavior == "ok":
            return {"id": f"{self.name}-1", "url": f"https://x/{self.name}"}
        if self.behavior == "refuse":
            raise PublishError("quota exceeded")
        if self.behavior == "crash":
            raise KeyboardInterrupt()
        if self.behavior == "network":
            raise ConnectionError("reset by peer")

    def recover(self, item, record):
        return self._recover

    def status(self):
        return {"platform": self.name, "ready": True}


def queue_item(ep, video, platforms=("youtube",), **kw):
    from ave.publish import queue
    kw.setdefault("ai_disclosure", False)
    return queue.create(ep, video=video, platforms=list(platforms), title=kw.pop("title", "Great Episode"),
                        publishers={p: RecordingPublisher(p) for p in platforms}, **kw)


def approve_item(ep, item_id, publishers=None):
    from ave.publish import queue
    it = queue.load(ep, item_id)
    return queue.approve(ep, item_id, "ben", it["sha256"][:12], publishers=publishers)
