"""One real Syncthing under the supervisor: private home, GUI on a 0600 socket, nothing without the key, public paths off,
one owner per home, a leftover from a crashed app is replaced, restart after a crash, clean stop, waiting for Tailscale."""

from __future__ import annotations

import http.client
import os
import signal
import stat
import subprocess
import sys
import textwrap
import xml.etree.ElementTree as ET

import pytest

from ave.syncthing import SyncError, SyncManager, Supervisor
from ave.syncthing import config as C
from ave.syncthing.api import _UnixConnection

from . import harness as H


def _listening_tcp(pid: int) -> list[str]:
    """Local addresses this process listens on over TCP (netstat names the owning process as name:pid)."""
    out = subprocess.run(["/usr/sbin/netstat", "-anv", "-p", "tcp"], capture_output=True, text=True).stdout
    return sorted(line.split()[3] for line in out.splitlines() if "LISTEN" in line and f":{pid} " in line + " ")


def test_runs_private_with_public_paths_off(sync_home, short_home):
    m = sync_home.manager
    sup = m.supervisor
    assert sup.home == short_home / "Library" / "Application Support" / "AVE" / "syncthing" and sup.env()["HOME"] == str(sup.home)
    assert stat.S_IMODE(os.stat(sup.home).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(sup.socket_path).st_mode) == 0o600 and stat.S_ISSOCK(os.stat(sup.socket_path).st_mode)
    o = m.api.get("/rest/config/options")
    assert {k: o[k] for k in C.SYNC_OPTIONS} == C.SYNC_OPTIONS
    assert o["listenAddresses"] == [f"tcp://127.0.0.1:{sync_home.port}"] and not o["crashReportingEnabled"]
    H.wait_until(lambda: _listening_tcp(sup.proc.pid), timeout=10, what="listener")
    assert _listening_tcp(sup.proc.pid) == [f"127.0.0.1.{sync_home.port}"]       # only the sync port: no TCP GUI at all
    assert m.api.get("/rest/config/folders") == []                    # no default ~/Sync folder
    xml = ET.parse(sup.home / "config.xml").getroot()
    assert xml.findtext("options/relaysEnabled") == "false" and xml.findtext("options/globalAnnounceEnabled") == "false"
    assert xml.findtext("gui/user") == "ave" and xml.findtext("gui/password").startswith("$2")
    assert C.DEVICE_ID_RE.match(m.device_id())
    assert str(sup.api.key) not in subprocess.run(["/bin/ps", "-ww", "-o", "command=", "-p", str(sup.proc.pid)],
                                                  capture_output=True, text=True).stdout


def _raw(sock: str, path: str, headers: dict) -> tuple[int, list]:
    c = _UnixConnection(sock, 5)
    c.request("GET", path, headers=headers)
    r = c.getresponse()
    r.read()
    return r.status, r.getheaders()


def test_nothing_gets_in_without_the_key(sync_home):
    sock = str(sync_home.manager.supervisor.socket_path)
    assert _raw(sock, "/rest/system/status", {})[0] == 403
    status, headers = _raw(sock, "/", {})
    cookies = [v.split(";")[0] for k, v in headers if k.lower() == "set-cookie"]
    csrf = [c for c in cookies if c.startswith("CSRF-Token")]
    hdr = {"Cookie": "; ".join(cookies)}
    if csrf:
        name, value = csrf[0].split("=", 1)
        hdr["X-" + name] = value
    assert _raw(sock, "/rest/config/devices", hdr)[0] in (401, 403)
    assert _raw(sock, "/rest/system/status", {"X-API-Key": "wrong"})[0] in (401, 403)
    assert sync_home.manager.api.ping()


def test_supervisor_restarts_after_a_crash(sync_home):
    m = sync_home.manager
    me, sup = m.device_id(), m.supervisor
    first = sup.proc.pid
    os.kill(first, signal.SIGKILL)
    H.wait_until(lambda: sup.restarts == 1 and sup.running() and sup.api.ping(), timeout=30, what="restart")
    assert sup.proc.pid != first and sup.last_exit == -signal.SIGKILL and sup.state == "running"
    assert m.device_id() == me
    assert m.api.get("/rest/config/options")["listenAddresses"] == [f"tcp://127.0.0.1:{sync_home.port}"]


def test_start_during_the_restart_backoff_keeps_one_supervisor(sync_home):
    m = sync_home.manager
    sup = m.supervisor
    sup.BACKOFF_START = 2.0
    os.kill(sup.proc.pid, signal.SIGKILL)
    H.wait_until(lambda: sup.state == "restarting", timeout=10, what="backoff")
    m.start()
    assert m.supervisor is sup
    H.wait_until(lambda: sup.restarts == 1 and sup.running(), timeout=30, what="restart")
    assert len(sup._leftovers()) == 1


def test_a_second_owner_of_the_home_is_refused_and_touches_nothing(sync_home):
    sup = sync_home.manager.supervisor
    before = (sup.home / "config.xml").read_bytes()
    other = Supervisor(sup.binary, sup.home, f"tcp://127.0.0.1:{H.free_port()}", allow_loopback=True, ready_timeout=5)
    with pytest.raises(SyncError, match="already running"):
        other.start()
    assert (sup.home / "config.xml").read_bytes() == before and sup.api.ping()


def test_a_syncthing_left_by_a_crashed_app_is_replaced(short, tmp_path):
    home = short / "st"
    port = H.free_port()
    script = textwrap.dedent(f"""
        import sys, time
        from pathlib import Path
        from ave.syncthing import Supervisor
        s = Supervisor(Path({str(H.binary_or_skip())!r}), Path({str(home)!r}), "tcp://127.0.0.1:{port}", allow_loopback=True)
        s.start(); print(s.proc.pid, flush=True); time.sleep(600)
    """)
    app = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True,
                           env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    old = int(app.stdout.readline())
    app.kill()                                                         # the app crashes; its Syncthing keeps running
    app.wait()
    os.kill(old, 0)                                                    # still alive
    sup = Supervisor(H.binary_or_skip(), home, f"tcp://127.0.0.1:{port}", allow_loopback=True)
    assert sup.is_ours(old)
    try:
        sup.start()
        assert sup.proc.pid != old and sup.api.ping()
        with pytest.raises(ProcessLookupError):
            os.kill(old, 0)
        assert (home / "syncthing.pid").read_text().strip() == str(sup.proc.pid)
        assert sup._leftovers() == [sup.proc.pid]
    finally:
        sup.stop()


def test_stop_is_clean_and_does_not_restart(sync_home):
    sup = sync_home.manager.supervisor
    proc = sup.proc
    sync_home.manager.stop()
    assert proc.poll() == 0 and sup.restarts == 0 and not sup.running() and sup.state == "stopped"
    assert not sup.pidfile.exists()


def test_waits_for_tailscale_then_starts(short):
    port, calls = H.free_port(), []

    def provider():
        calls.append(1)
        if len(calls) < 3:
            raise SyncError("Tailscale is not available")
        return f"tcp://127.0.0.1:{port}"

    m = SyncManager(short / "ts", binary=H.binary_or_skip(), ave_device="dev_T", allow_loopback=True,
                    listen_provider=provider, tailscale_poll_s=0.2)
    try:
        m.start()
        assert m.state == "waiting_for_tailscale" and "Tailscale" in m.waiting_reason
        H.wait_until(lambda: m.state == "running", timeout=30, what="start after Tailscale")
        assert m.api.get("/rest/config/options")["listenAddresses"] == [f"tcp://127.0.0.1:{port}"]
    finally:
        m.stop()
    assert m.state == "stopped"


def test_a_bad_tailscale_address_shows_as_an_error_and_keeps_retrying(short):
    from ave.syncthing import ConfigError

    def provider():
        raise ConfigError("refusing 8.8.8.8: not a Tailscale address")

    m = SyncManager(short / "ts", binary=H.binary_or_skip(), ave_device="dev_T", allow_loopback=True,
                    listen_provider=provider, tailscale_poll_s=0.1)
    try:
        m.start()
        H.wait_until(lambda: m.state == "error", timeout=10, what="error state")
        assert "Tailscale" in m.start_error
    finally:
        m.stop()


def test_a_failed_configure_stops_the_new_syncthing(short, monkeypatch):
    from ave.syncthing import ApiError
    from ave.syncthing.api import Api

    def boom(self, path, body):
        raise ApiError("PATCH refused")

    monkeypatch.setattr(Api, "patch", boom)
    m = SyncManager(short / "cf", f"tcp://127.0.0.1:{H.free_port()}", binary=H.binary_or_skip(), ave_device="dev_C",
                    allow_loopback=True)
    with pytest.raises(ApiError):
        m.start()
    assert not m.supervisor.running() and m.supervisor._leftovers() == [] and m.state == "stopped"


def test_the_restart_failure_reason_stays_visible(sync_home, monkeypatch):
    sup = sync_home.manager.supervisor
    sup.BACKOFF_START = 0.1

    def fail():
        raise SyncError("disk full")

    monkeypatch.setattr(sup, "_spawn", fail)
    os.kill(sup.proc.pid, signal.SIGKILL)
    H.wait_until(lambda: sup.error.startswith("restart failed"), timeout=10, what="failure")
    import time
    time.sleep(0.6)                                                    # further retries keep the reason
    assert sup.error == "restart failed: disk full" and sup.state == "restarting"


def test_the_pidfile_is_used_when_ps_fails(sync_home, monkeypatch):
    sup = sync_home.manager.supervisor
    monkeypatch.setattr(Supervisor, "_commands", staticmethod(lambda pid=None: None))
    assert sup._leftovers() == [sup.proc.pid] and not sup.is_ours(sup.proc.pid)      # found, but never killed unverified


def test_stop_while_the_waiting_loop_is_starting_leaves_nothing_running(short, monkeypatch):
    import threading
    port, calls = H.free_port(), []
    started, release = threading.Event(), threading.Event()
    real_start = Supervisor.start

    def slow_start(self):
        started.set()                                                  # the loop has made its supervisor; stop() races it
        release.wait(10)
        real_start(self)

    monkeypatch.setattr(Supervisor, "start", slow_start)

    def provider():
        calls.append(1)
        if len(calls) < 2:
            raise SyncError("Tailscale is not available")
        return f"tcp://127.0.0.1:{port}"

    m = SyncManager(short / "race", binary=H.binary_or_skip(), ave_device="dev_R", allow_loopback=True,
                    listen_provider=provider, tailscale_poll_s=0.1)
    m.start()
    assert started.wait(30)
    stopper = threading.Thread(target=m.stop)
    stopper.start()
    import time
    time.sleep(0.3)
    release.set()
    stopper.join(60)
    assert not stopper.is_alive() and m.state == "stopped"
    assert not m.supervisor.running() and m.supervisor._leftovers() == []
