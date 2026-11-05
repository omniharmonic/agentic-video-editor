"""Start and supervise one Syncthing process per user.

Syncthing's home is `~/Library/Application Support/AVE/syncthing` (from HOME, so tests get their own). It always runs with
`--home` there, `HOME` set to that same folder (never the person's real home, so it can never find or touch a Syncthing they
run themselves), an explicit minimal environment, `--no-browser`, `--no-restart` (this class does the restarting) and
`--no-upgrade`.

The GUI is a Unix socket, `<home>/gui.sock` (0600, home 0700), with a random API key and a random GUI user password (bcrypt,
set by `syncthing generate`) on every start, so another local user cannot reach it and nothing without the key gets in.

One owner per home: an exclusive `flock` on `<home>/ave.lock` is held while this process owns Syncthing, and config.xml is
only rewritten under it. `<home>/syncthing.pid` records the child. A Syncthing left running by an app that crashed is shut
down through its API (with the key read from config.xml before anything is patched), and killed only after its command line
shows it is syncthing `serve` on this exact home.

A crash restarts it with exponential backoff (0.5 s doubling to 30 s, reset after a minute of stable running); any failure
while restarting is recorded in `error` and retried. `stop()` is prompt even mid-start.
"""

from __future__ import annotations

import fcntl
import os
import pwd
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config as C
from .api import Api, ApiError

SOCKET_MAX = 100                   # macOS sockaddr_un holds 104 bytes


class SyncError(RuntimeError):
    pass


def default_home() -> Path:
    return Path.home() / "Library" / "Application Support" / "AVE" / "syncthing"


def find_binary() -> Path:
    """The bundled syncthing. Frozen app: the verified runtime component only. Otherwise AVE_SYNCTHING, then the local
    release build at ~/ave-release-build/sync/syncthing (the real user's home, not HOME). Never PATH."""
    if getattr(sys, "frozen", False):
        from .. import desktop_runtime
        return desktop_runtime.component("sync") / "syncthing"
    env = os.environ.get("AVE_SYNCTHING")
    if env:
        p = Path(env)
        if not (p.is_file() and os.access(p, os.X_OK)):
            raise SyncError(f"AVE_SYNCTHING={env} is not an executable file")
        return p
    dev = Path(pwd.getpwuid(os.getuid()).pw_dir) / "ave-release-build" / "sync" / "syncthing"
    if dev.is_file() and os.access(dev, os.X_OK):
        return dev
    raise SyncError("no syncthing binary: set AVE_SYNCTHING or build the release runtime")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Supervisor:
    BACKOFF_START, BACKOFF_MAX, STABLE_S = 0.5, 30.0, 60.0

    def __init__(self, binary: Path, home: Path, listen: str, *, ready_timeout: float = 30.0, allow_loopback: bool = False):
        self.binary, self.home = Path(binary), C.real(home)
        self.allow_loopback = allow_loopback
        self.listen = C.listen_url(listen, allow_loopback=allow_loopback)
        self.ready_timeout = ready_timeout
        self.socket_path = self.home / "gui.sock"
        if len(str(self.socket_path)) > SOCKET_MAX:
            raise SyncError(f"Syncthing home path is too long for its control socket: {self.socket_path}")
        self.proc: subprocess.Popen | None = None
        self.api: Api | None = None
        self.restarts = 0
        self.last_exit: int | None = None
        self.error = ""
        self.state = "stopped"             # stopped | starting | running | restarting
        self._want = False
        self._lock = threading.Lock()
        self._lockfile = None
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()

    # ------------------------------------------------------------------ helpers
    @property
    def pidfile(self) -> Path:
        return self.home / "syncthing.pid"

    def env(self) -> dict[str, str]:
        return {"HOME": str(self.home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
                "LANG": "en_US.UTF-8", "STNODEFAULTFOLDER": "1", "STNOUPGRADE": "1", "STNORESTART": "1",
                "STMONITORED": "1"}                  # run as one process: no monitor child of its own

    def _run(self, *args: str, timeout: float = 60, stdin: str | None = None) -> str:
        r = subprocess.run([str(self.binary), *args, f"--home={self.home}"], env=self.env(), capture_output=True, text=True,
                           timeout=timeout, cwd=str(self.home), input=stdin)
        if r.returncode != 0:
            raise SyncError(f"syncthing {args[0]} failed: {(r.stderr or r.stdout).strip()[-500:]}")
        return r.stdout

    def _mkhome(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        os.chmod(self.home, 0o700)

    def device_id(self) -> str:
        self._mkhome()
        if not (self.home / "cert.pem").is_file():
            with self._owned():
                self._generate()
        out = self._run("device-id").strip().splitlines()[-1].strip()
        if not C.DEVICE_ID_RE.match(out):
            raise SyncError(f"unexpected device id {out!r}")
        return out

    # ------------------------------------------------------------------ ownership of the home
    def _acquire(self) -> None:
        self._mkhome()
        f = open(self.home / "ave.lock", "a+")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.close()
            raise SyncError("another AVE is already running sync on this computer") from None
        self._lockfile = f

    def _release(self) -> None:
        if self._lockfile:
            fcntl.flock(self._lockfile, fcntl.LOCK_UN)
            self._lockfile.close()
            self._lockfile = None

    class _Owned:
        def __init__(self, sup):
            self.sup, self.mine = sup, False

        def __enter__(self):
            if not self.sup._lockfile:
                self.sup._acquire()
                self.mine = True

        def __exit__(self, *exc):
            if self.mine:
                self.sup._release()

    def _owned(self):
        return Supervisor._Owned(self)

    def _ours_cmd(self, cmd: str) -> bool:
        parts = cmd.strip().split(" serve ", 1)
        return len(parts) == 2 and Path(parts[0]).name == "syncthing" and f"--home={self.home} " in parts[1] + " "

    @staticmethod
    def _commands(pid: int | None = None) -> dict[int, str] | None:
        """pid -> command line, or None when ps itself failed."""
        argv = ["/bin/ps", "-ww", "-o", "pid=,command="] + (["-p", str(pid)] if pid else ["-ax"])
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode not in (0, 1) or (not pid and not r.stdout.strip()):
            return None
        out = r.stdout
        res = {}
        for line in out.splitlines():
            head, _, cmd = line.strip().partition(" ")
            if head.isdigit():
                res[int(head)] = cmd
        return res

    def is_ours(self, pid: int) -> bool:
        """True when `pid` is syncthing `serve` on this exact home."""
        return self._ours_cmd((self._commands(pid) or {}).get(pid, ""))

    def _pidfile_pid(self) -> int | None:
        try:
            return int(self.pidfile.read_text().strip())
        except (OSError, ValueError):
            return None

    def _leftovers(self) -> list[int]:
        """Syncthing processes serving this home, found by command line. When ps fails, the pidfile's live pid is used
        instead (it is then only asked to shut down through its API, never killed, since it cannot be verified)."""
        cmds = self._commands()
        if cmds is None:
            pid = self._pidfile_pid()
            return [pid] if pid and pid != os.getpid() and _alive(pid) else []
        return [p for p, cmd in cmds.items() if p != os.getpid() and self._ours_cmd(cmd)]

    def _reap_leftovers(self) -> None:
        """Shut down a Syncthing an earlier (crashed) app left on this home: politely via its own API key first."""
        pids = self._leftovers()
        if not pids:
            return
        address, key = C.read_gui(self.home / "config.xml") if (self.home / "config.xml").is_file() else ("", "")
        if address.startswith("unix://") and key:
            try:
                Api(address[len("unix://"):], key, timeout=5).post("/rest/system/shutdown")
            except ApiError:
                pass
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and any(_alive(p) for p in pids):
            time.sleep(0.1)
        for p in pids:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                if _alive(p) and self.is_ours(p):
                    os.kill(p, sig)
                    end = time.monotonic() + 5
                    while time.monotonic() < end and _alive(p):
                        time.sleep(0.1)
        if any(_alive(p) for p in pids):
            raise SyncError("an earlier Syncthing on this home would not stop")

    # ------------------------------------------------------------------ process
    def _generate(self) -> None:
        """Create keys and config once; on every call set a fresh random GUI password (stored bcrypt-hashed)."""
        self._mkhome()
        self._run("generate", "--no-port-probing", f"--gui-user={C.GUI_USER}", "--gui-password=-",
                  stdin=secrets.token_urlsafe(32) + "\n")
        for stray in self.home.glob(".syncthing.tmp.*"):
            stray.unlink(missing_ok=True)

    def _spawn(self) -> None:
        """Start one Syncthing. Caller holds the home lock. Gives up early when stop() is called."""
        self._reap_leftovers()
        self._generate()
        key = secrets.token_urlsafe(32)
        self.socket_path.unlink(missing_ok=True)
        C.patch_config_xml(self.home / "config.xml", self.listen, self.socket_path, key, allow_loopback=self.allow_loopback)
        log = open(self.home / "syncthing.log", "ab")
        try:
            proc = subprocess.Popen(
                [str(self.binary), "serve", f"--home={self.home}", "--no-browser", "--no-restart", "--no-upgrade",
                 "--log-file=-"],
                env={**self.env(), "STGUIADDRESS": f"unix://{self.socket_path}", "STGUIAPIKEY": key},   # the key stays off argv
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, cwd=str(self.home), start_new_session=True)
        finally:
            log.close()
        self.pidfile.write_text(f"{proc.pid}\n")
        api = Api(self.socket_path, key)
        with self._lock:
            self.proc, self.api = proc, api
        deadline = time.monotonic() + self.ready_timeout
        while time.monotonic() < deadline:
            if not self._want:
                self._kill(proc)
                raise SyncError("stopped while starting")
            if proc.poll() is not None:
                raise SyncError(f"syncthing exited with {proc.returncode} on start; see {self.home / 'syncthing.log'}")
            if api.ping():
                return
            time.sleep(0.1)
        self._kill(proc)
        raise SyncError("syncthing did not answer on its API in time")

    @staticmethod
    def _kill(p: subprocess.Popen | None) -> None:
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(5)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        with self._lock:
            if self._want:
                return                              # already running, starting or in a restart backoff
            self._want = True
            self.state, self.error = "starting", ""
        try:
            self._acquire()
            self._spawn()
        except BaseException as exc:
            with self._lock:
                self._want = False
                self.state, self.error = "stopped", str(exc)
            self._kill(self.proc)
            self._release()
            raise
        with self._lock:
            self.state = "running"
            self._wake.clear()
            self._thread = threading.Thread(target=self._watch, name="ave-syncthing", daemon=True)
            self._thread.start()

    def _watch(self) -> None:
        backoff, started, seen = self.BACKOFF_START, time.monotonic(), None
        while True:
            p = self.proc
            if p is not None and p is not seen:     # a process already reported keeps its "restart failed" reason
                seen = p
                code = p.wait()
                with self._lock:
                    if not self._want:
                        return
                    self.last_exit, self.state = code, "restarting"
                    if not self.error.startswith("restart failed"):
                        self.error = f"syncthing exited with {code}"
            if time.monotonic() - started > self.STABLE_S:
                backoff = self.BACKOFF_START
            if self._wake.wait(backoff):
                return                              # stop() during the backoff
            if not self._want:
                return
            try:
                self._spawn()
                with self._lock:
                    self.restarts += 1
                    self.state, self.error = "running", ""
                started = time.monotonic()
            except Exception as exc:                # noqa: BLE001  any failure is shown and retried
                with self._lock:
                    self.error = f"restart failed: {exc}"
                    seen = self.proc                # its exit is part of this failure, not a new crash
                if not self._want:
                    return
            backoff = min(backoff * 2, self.BACKOFF_MAX)

    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    @property
    def wanted(self) -> bool:
        return self._want

    def wait_ready(self, timeout: float = 30.0) -> Api:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            api = self.api
            if self.running() and api and api.ping():
                return api
            if not self._want:
                break
            time.sleep(0.1)
        raise SyncError("syncthing is not running" + (f": {self.error}" if self.error else ""))

    def stop(self) -> None:
        with self._lock:
            if not self._want and self._lockfile is None and self.proc is None:
                return                              # never started, or already stopped: touch nothing
            self._want = False
            self._wake.set()
            p, api = self.proc, self.api
        if p and p.poll() is None:
            try:
                if api:
                    api.post("/rest/system/shutdown")
            except ApiError:
                pass
            try:
                p.wait(15)
            except subprocess.TimeoutExpired:
                self._kill(p)
        if self._thread:
            self._thread.join(10)
        self._kill(self.proc)                       # a restart that raced the stop
        self.pidfile.unlink(missing_ok=True)
        self._release()
        with self._lock:
            self.proc, self.state = None, "stopped"
