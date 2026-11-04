"""The loopback file rules every route of the app's server inherits (studio/ui/http.py, moved from the retired `ave ui`): no
traversal, no dot-files, no symlink out of the project, never raw footage (whatever the case or the symlink), ranged reads, and a
bad Content-Length is refused without reading. Exercised through the Workspace's episode file route."""

from __future__ import annotations

import http.client
import socket
import threading

import pytest

from ave.episode import Episode
from ave.project import Project
from ave.studio.project import StudioProject
from ave.studio.service import open_studio
from ave.studio.ui.server import StudioServer


@pytest.fixture
def world(tmp_path):
    sp = StudioProject(tmp_path / "studio")
    sp.create(title="Files", tz="UTC")
    ep = Episode(sp.root / "ep12")
    ep.create("Episode 12")
    p = Project.create(ep.root, "Episode 12", person="Maren")
    ep.raw.mkdir(parents=True, exist_ok=True)
    (ep.raw / "camA.mp4").write_bytes(b"raw footage bytes")
    ep.exports.mkdir(parents=True, exist_ok=True)
    (ep.exports / "notes.txt").write_text("0123456789" * 10)
    (ep.exports / ".hidden").write_text("secret")
    srv = StudioServer(open_studio(sp.root), pin="424242")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield type("W", (), {"srv": srv, "ep": ep, "base": f"/api/workspace/file/{p.project_id}/", "raw": ep.rel(ep.raw)})
    srv.shutdown()
    t.join(5)
    srv.server_close()


def _get(w, path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", w.srv.port, timeout=10)
    c.request("GET", path, headers={"X-AVE-Token": w.srv.token, "Host": f"127.0.0.1:{w.srv.port}", **(headers or {})})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, dict(r.getheaders()), body


@pytest.mark.parametrize("rel", ["../episode.json", "%2e%2e/episode.json", "exports/../../episode.json", "exports/.hidden",
                                 "exports/%00", "exports"])
def test_traversal_and_dotfiles_are_refused(world, rel):
    s, _, body = _get(world, world.base + rel)
    assert s in (400, 403, 404), (rel, s)
    assert b"secret" not in body


def test_a_symlink_out_of_the_project_is_refused(world, tmp_path):
    out = tmp_path / "outside.txt"
    out.write_text("outside")
    (world.ep.exports / "link.txt").symlink_to(out)
    assert _get(world, world.base + "exports/link.txt")[0] == 404


def test_raw_footage_is_never_served_whatever_the_case_or_the_symlink(world):
    for rel in (f"{world.raw}/camA.mp4", f"{world.raw.upper()}/camA.mp4", f"{world.raw.title()}/CAMA.MP4"):
        s, _, body = _get(world, world.base + rel)
        assert s in (403, 404) and b"raw footage bytes" not in body, rel
    (world.ep.exports / "alias").symlink_to(world.ep.raw, target_is_directory=True)
    (world.ep.exports / "alias_file.mp4").symlink_to(world.ep.raw / "camA.mp4")
    for rel in ("exports/alias/camA.mp4", "exports/alias_file.mp4"):
        s, _, body = _get(world, world.base + rel)
        assert s in (403, 404) and b"raw footage bytes" not in body, rel


def test_file_range_requests(world):
    url = world.base + "exports/notes.txt"
    s, h, body = _get(world, url)
    assert s == 200 and len(body) == 100 and h["Accept-Ranges"] == "bytes"
    s, h, body = _get(world, url, {"Range": "bytes=10-19"})
    assert s == 206 and body == b"0123456789" and h["Content-Range"] == "bytes 10-19/100"
    assert len(_get(world, url, {"Range": "bytes=95-"})[2]) == 5 and len(_get(world, url, {"Range": "bytes=-4"})[2]) == 4
    assert _get(world, url, {"Range": "bytes=500-"})[0] == 416 and _get(world, url, {"Range": "bytes=abc"})[0] == 416


@pytest.mark.parametrize("value", ["-1", "-100000", "abc", str(10_000_000)])
def test_a_bad_content_length_is_refused_and_never_read(world, value):
    c = socket.create_connection(("127.0.0.1", world.srv.port), timeout=5)
    try:
        c.sendall((f"POST /api/workspace/content/reject HTTP/1.1\r\nHost: 127.0.0.1:{world.srv.port}\r\nX-AVE-Token: {world.srv.token}\r\n"
                   f"Content-Type: application/json\r\nContent-Length: {value}\r\nConnection: close\r\n\r\n").encode())
        data = b""
        while chunk := c.recv(65536):
            data += chunk
    finally:
        c.close()
    assert int(data.split(b" ", 2)[1]) in (400, 411, 413)


def test_a_file_replaced_while_it_is_served_is_sent_whole(world, monkeypatch):
    """A background writer swaps the file for a shorter one between the size check and the read (atomic replace): the
    response must describe the file it actually sends, or a kept-alive client waits forever for the missing bytes."""
    from pathlib import Path
    target = world.ep.exports / "notes.txt"
    real_open = Path.open

    def swap_then_open(self, *a, **k):
        if self == target:
            tmp = target.with_suffix(".new")
            tmp.write_text("short")
            tmp.replace(target)
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", swap_then_open)
    s, h, body = _get(world, world.base + "exports/notes.txt")
    assert s == 200 and body == b"short" and h["Content-Length"] == "5"


def test_a_file_cut_short_mid_read_closes_the_connection(world, monkeypatch):
    """Truncated in place after the headers went out: the bytes cannot be taken back, so the server closes the connection
    and the client gets a clear short read at once instead of hanging on a kept-alive socket."""
    import os
    from pathlib import Path
    target = world.ep.exports / "notes.txt"
    real_open = Path.open

    class Shrinking:
        def __init__(self, f):
            self.f = f
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            self.f.close()
        def seek(self, n):
            self.f.seek(n)
        def fileno(self):
            return self.f.fileno()
        def read(self, n):
            os.truncate(target, 10)
            return self.f.read(n)

    monkeypatch.setattr(Path, "open", lambda self, *a, **k: Shrinking(real_open(self, *a, **k)) if self == target else real_open(self, *a, **k))
    c = http.client.HTTPConnection("127.0.0.1", world.srv.port, timeout=5)
    c.request("GET", world.base + "exports/notes.txt", headers={"X-AVE-Token": world.srv.token, "Host": f"127.0.0.1:{world.srv.port}"})
    r = c.getresponse()
    with pytest.raises(http.client.IncompleteRead):
        r.read()
    c.close()


def test_responses_go_out_without_waiting_on_nagle(world):
    """Kept-alive responses are two writes (headers, then body). With Nagle's algorithm on, Linux holds the second until the
    client's delayed ACK (up to 40 ms) on every response: the route walk took minutes on Linux CI instead of seconds."""
    import socket as _socket
    seen = []
    from ave.studio.ui import http as http_mod
    real = http_mod.Handler.setup

    def setup(self):
        real(self)
        seen.append(self.connection.getsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY))

    http_mod.Handler.setup = setup
    try:
        _get(world, world.base + "exports/notes.txt")
    finally:
        http_mod.Handler.setup = real
    assert seen and all(seen), seen
