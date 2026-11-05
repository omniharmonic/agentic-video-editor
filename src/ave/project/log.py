"""Per-device append-only logs: `.ave/log/<full device id>.jsonl`, one writer per file.

Writing: one JSON object per line, appended with O_APPEND under an exclusive lock (the app and the MCP server on one computer
share the file), then made durable with F_FULLFSYNC on macOS (fsync elsewhere); a newly created log file also syncs its
directory. The lock is flock; where a volume does not support it (some exFAT or network volumes) an exclusive lock file in the
local zone is used, with stale-lock recovery. A file that does not end in a newline (a crash mid-write) gets a newline first,
so a torn fragment can never swallow the next event. Nothing is ever rewritten.

Reading never raises for bad content: a torn final line, malformed lines and invalid events are skipped and described in
`.warnings`.

File names are the full device id. Two different ids that would land on one file (they differ only in letter case, which
APFS and exFAT treat as the same name) are refused rather than merged into one log.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import socket
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator

from .. import atomic
from . import hlc as _hlc
from .reducer import ACTORS, OPS, validate_event

DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
LOCK_STALE_S = 30.0
LOCK_TIMEOUT_S = 60.0
_NO_FLOCK = (errno.ENOTSUP, errno.EINVAL, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP))
_TAIL_HLC_RE = re.compile(rb'"hlc"\s*:\s*"([^"]{1,128})"')


class LogError(Exception):
    pass


def release_version() -> str | None:
    """The version a packaged release app stamps on what it writes, or None. Development and source runs stamp nothing,
    so a checkout ahead of the shipped app can never make teammates on the release read-only."""
    import sys
    if not getattr(sys, "frozen", False):          # the same test desktop_runtime.frozen() uses for the packaged app
        return None
    from .. import __version__
    return __version__


class ReadOnlyError(LogError, ValueError):
    """A write to a project saved by a newer AVE (model.update_needed): the project is read-only on this computer."""


class LogRead(list):
    """The events read from one or more log files, plus `.warnings` about anything skipped."""

    def __init__(self, events: Iterable[dict] = (), warnings: Iterable[str] = ()):
        super().__init__(events)
        self.warnings: list[str] = list(warnings)


def log_filename(device_id: str) -> str:
    if not isinstance(device_id, str) or not DEVICE_ID_RE.match(device_id):
        raise LogError(f"device id {device_id!r} cannot name a log file (letters, digits, _ and - only, at most 64)")
    return device_id + ".jsonl"


def file_collisions(device_ids: Iterable[str]) -> list[tuple[str, str]]:
    """Pairs of different device ids that would share one log file on a case-insensitive volume."""
    seen: dict[str, str] = {}
    out = []
    for d in device_ids:
        k = d.casefold()
        if k in seen and seen[k] != d:
            out.append((seen[k], d))
        seen.setdefault(k, d)
    return out


def _parse_lines(raw_lines: list[bytes], name: str, first_line: int, out: LogRead) -> None:
    for n, raw in enumerate(raw_lines, first_line):
        if not raw.strip():
            continue
        try:
            e = json.loads(raw)
        except (ValueError, RecursionError):
            out.warnings.append(f"{name} line {n}: not valid JSON, skipped")
            continue
        try:
            why = validate_event(e)
        except RecursionError:
            why = "nested too deep"
        if why:
            out.warnings.append(f"{name} line {n}: skipped, {why}")
            continue
        if e["device"] != name.removesuffix(".jsonl"):
            out.warnings.append(f"{name} line {n}: event from device {e['device']!r} in another device's log")
        out.append(e)


def read_log(path: Path) -> LogRead:
    path = Path(path)
    out = LogRead()
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return out
    except OSError as exc:
        out.warnings.append(f"{path.name}: could not be read ({exc.strerror or exc})")
        return out
    lines = data.split(b"\n")
    tail = lines.pop()  # b"" when the file ends in a newline
    if tail.strip():
        out.warnings.append(f"{path.name}: torn final line skipped (an unfinished write; it is kept on disk untouched)")
    _parse_lines(lines, path.name, 1, out)
    return out


def log_files(log_dir: Path) -> list[Path]:
    d = Path(log_dir)
    return sorted(p for p in d.glob("*.jsonl") if p.is_file() and not p.name.startswith(".")) if d.is_dir() else []


def read_logs(log_dir: Path) -> LogRead:
    out = LogRead()
    for p in log_files(log_dir):
        r = read_log(p)
        out.extend(r)
        out.warnings.extend(r.warnings)
    return out


@dataclass
class _Cursor:
    """How far one file has been read, and the newest stamp per device in what was read."""
    ino: int
    offset: int = 0
    lines: int = 0
    newest: dict[str, _hlc.Stamp] = field(default_factory=dict)


def _sync(fd: int) -> None:
    """Durable on macOS too: plain fsync there only reaches the drive's cache."""
    if hasattr(fcntl, "F_FULLFSYNC"):
        try:
            fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
            return
        except OSError:
            pass
    os.fsync(fd)


def _sync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        _sync(fd)
    except OSError:
        pass  # some volumes cannot sync a directory; the file itself is already durable
    finally:
        os.close(fd)


def _lock_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _is_stale(path: Path, raw: bytes, stale_s: float) -> bool:
    """Staleness comes from the time the holder wrote into the lock, not the volume's mtime (a server's clock on network
    volumes). A lock with no readable time (a holder that crashed between creating and writing it) falls back to mtime."""
    try:
        t = json.loads(raw).get("t")
        if isinstance(t, (int, float)):
            return time.time() - t > stale_s
    except (ValueError, AttributeError, RecursionError):
        pass
    try:
        return time.time() - path.stat().st_mtime > stale_s
    except FileNotFoundError:
        return False


def _remove_if(path: Path, expect: bytes) -> bool:
    """Remove `path` only if it still holds `expect`. It is first renamed to a unique name, so exactly one caller can move a
    given lock, and a lock that turns out to be someone else's is put back without replacing anything."""
    gone = path.with_name(f"{path.name}.{uuid.uuid4().hex}.gone")
    try:
        os.rename(path, gone)
    except FileNotFoundError:
        return False
    try:
        if gone.read_bytes() == expect:
            return True
        try:
            atomic.publish(gone, path)  # not ours: restore it, never over a lock created meanwhile
        except FileExistsError:
            pass
        return False
    finally:
        try:
            os.unlink(gone)
        except FileNotFoundError:
            pass


def _take_over(path: Path, raw: bytes, stale_s: float) -> None:
    """Remove a stale lock. A `.takeover` guard lets one waiter at a time do it, and it re-checks under the guard."""
    guard = path.with_name(path.name + ".takeover")
    mine = json.dumps({"token": uuid.uuid4().hex, "t": time.time()}).encode()
    try:
        fd = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        graw = _lock_bytes(guard)
        if graw is not None and _is_stale(guard, graw, stale_s):
            _remove_if(guard, graw)  # a waiter crashed mid-takeover
        return
    try:
        os.write(fd, mine)
    finally:
        os.close(fd)
    try:
        if _lock_bytes(path) == raw and _is_stale(path, raw, stale_s):
            _remove_if(path, raw)
    finally:
        _remove_if(guard, mine)


@contextmanager
def lock_file(path: Path, stale_s: float = LOCK_STALE_S, timeout_s: float = LOCK_TIMEOUT_S) -> Iterator[None]:
    """An exclusive O_EXCL lock file holding a unique token and the time it was taken. A lock older than `stale_s` (a
    crashed holder) is taken over; releasing removes the lock only if it still holds our token."""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_s
    mine = json.dumps({"token": uuid.uuid4().hex, "host": socket.gethostname(), "pid": os.getpid(),
                       "t": time.time()}).encode()
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raw = _lock_bytes(path)
            if raw is None:
                continue
            if _is_stale(path, raw, stale_s):
                _take_over(path, raw, stale_s)
                continue
            if time.monotonic() > deadline:
                raise TimeoutError(f"{path} is held by another writer")
            time.sleep(0.002)
            continue
        try:
            os.write(fd, mine)
        finally:
            os.close(fd)
        break
    try:
        yield
    finally:
        if _lock_bytes(path) == mine:  # a holder that was taken over leaves its successor's lock alone
            _remove_if(path, mine)


class Log:
    """This computer's log in one project. Only ever writes `<log_dir>/<device id>.jsonl`."""

    def __init__(self, log_dir: Path, device: str, person: str | None, *, actor: str = "human",
                 wall: Callable[[], int] = _hlc.wall_ms, lock_dir: Path | None = None):
        self.log_dir = Path(log_dir)
        self.device = device
        self.filename = log_filename(device)
        self.person = person
        self.actor = actor
        self.clock = _hlc.Clock(device, wall=wall)
        self.lock_dir = Path(lock_dir) if lock_dir else self.log_dir.parent / "local" / "locks"
        self._cursors: dict[str, _Cursor] = {}
        # Why this project is read-only here (a newer AVE wrote it), or None: a string, or a callable asked on every append.
        self.refuse = None

    @property
    def path(self) -> Path:
        return self.log_dir / self.filename

    @property
    def warnings(self) -> list[str]:
        return list(self.clock.warnings)

    def check_files(self) -> None:
        """Refuse when another file in the log folder is this device's name in a different letter case."""
        for p in log_files(self.log_dir):
            if p.name != self.filename and p.name.casefold() == self.filename.casefold():
                raise LogError(f"{p.name} and {self.filename} are different devices that would share one log file on this "
                               f"volume; refusing to write. Give one of these computers a new device id.")

    # ---------------------------------------------------------------- reading the frontier, incrementally
    def _scan(self, p: Path) -> tuple[_Cursor | None, bytes]:
        try:
            st = p.stat()
        except FileNotFoundError:
            self._cursors.pop(p.name, None)
            return None, b""
        cur = self._cursors.get(p.name)
        if cur is None or cur.ino != st.st_ino or st.st_size < cur.offset:
            cur = self._cursors[p.name] = _Cursor(ino=st.st_ino)  # replaced by sync, or new: read from the start
        try:
            with open(p, "rb") as f:
                f.seek(cur.offset)
                chunk = f.read()
        except OSError:
            return cur, b""
        end = chunk.rfind(b"\n") + 1
        got = LogRead()
        lines = chunk[:end].split(b"\n")[:-1] if end else []
        _parse_lines(lines, p.name, cur.lines + 1, got)
        for e in got:
            s = _hlc.parse(e["hlc"])
            if e["device"] not in cur.newest or s > cur.newest[e["device"]]:
                cur.newest[e["device"]] = s
        cur.offset += end
        cur.lines += len(lines)
        return cur, chunk[end:]

    def _frontier(self) -> dict[str, _hlc.Stamp]:
        """{device: newest stamp read}, observing every one so the next tick follows all of them."""
        fr: dict[str, _hlc.Stamp] = {}
        for p in log_files(self.log_dir):
            cur, tail = self._scan(p)
            if cur is None:
                continue
            for d, s in cur.newest.items():
                if d not in fr or s > fr[d]:
                    fr[d] = s
            if p.name == self.filename and tail.strip():
                # our own torn last line: never stamp below it, even though nobody will ever read it as an event
                for m in _TAIL_HLC_RE.finditer(tail):
                    try:
                        self.clock.observe(m.group(1).decode())
                    except (ValueError, UnicodeDecodeError):
                        pass
        for s in fr.values():
            self.clock.observe(s)
        return fr

    @contextmanager
    def _exclusive(self, fd: int) -> Iterator[None]:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as exc:
            if exc.errno not in _NO_FLOCK:
                raise
            with lock_file(self.lock_dir / (self.filename + ".lock")):
                yield
            return
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)

    # ---------------------------------------------------------------- writing
    def append(self, op: str, artifact: str | None = None, data: dict | None = None, needs: Iterable[dict] = (), *,
               actor: str | None = None, person: str | None = None, seen: dict[str, str] | None = None) -> dict:
        reason = self.refuse() if callable(self.refuse) else self.refuse
        if reason:                              # a project saved by a newer AVE (model.update_needed): read-only here
            raise ReadOnlyError(reason)
        if op not in OPS:
            raise ValueError(f"unknown op {op!r}; one of {', '.join(OPS)}")
        actor = actor or self.actor
        if actor not in ACTORS:
            raise ValueError(f"actor must be one of {ACTORS}, got {actor!r}")
        if data is not None and not isinstance(data, dict):
            raise ValueError("data must be an object")
        needs = [dict(n) for n in needs]
        for n in needs:
            if not isinstance(n.get("path"), str):
                raise ValueError(f"each need must have a path: {n!r}")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.check_files()
        created = not self.path.exists()
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            with self._exclusive(fd):
                fr = self._frontier()  # under the lock: another process on this computer may have just appended
                if seen is None:
                    seen = {d: str(s) for d, s in sorted(fr.items())}
                else:
                    seen = self._check_seen(seen, fr)
                event: dict = {"id": _hlc.ulid(), "hlc": self.clock.tick(), "device": self.device,
                               "person": person or self.person, "actor": actor, "op": op, "artifact": artifact,
                               "data": data or {}, "needs": needs, "seen": seen}
                stamp = release_version()      # a release says which AVE wrote it: a project's version is its newest stamp
                if stamp:
                    event = {"id": event["id"], "app": stamp, **{k: v for k, v in event.items() if k != "id"}}
                why = validate_event(event)
                if why:
                    raise ValueError(f"this event would be unreadable: {why}")
                line = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                if _needs_newline(self.path):
                    line = b"\n" + line
                view = memoryview(line)
                while view:
                    view = view[os.write(fd, view):]
                _sync(fd)
        finally:
            os.close(fd)
        if created:
            _sync_dir(self.log_dir)
        return event

    @staticmethod
    def _check_seen(seen: dict, fr: dict[str, _hlc.Stamp]) -> dict[str, str]:
        out = {}
        for d, v in seen.items():
            try:
                s = _hlc.parse(v)
            except ValueError as exc:
                raise ValueError(f"seen[{d!r}] is not a stamp: {v!r}") from exc
            if d not in fr or s > fr[d]:
                raise ValueError(f"seen[{d!r}] = {v} is ahead of what this computer has read from that device")
            out[d] = v
        return dict(sorted(out.items()))


def _needs_newline(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            if not size:
                return False
            f.seek(size - 1)
            return f.read(1) != b"\n"
    except FileNotFoundError:
        return False
