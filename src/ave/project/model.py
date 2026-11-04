"""Project identity on disk: `.ave/project.json`, this computer's device id, and the folders beside them.

<project>/.ave/
  project.json         settings zone: permanent project_id, title, kind ("episode" | "studio"), layout, members, publisher
                       device, and each member device's Syncthing id and Tailscale address (see ave.syncthing.members).
                       Never any paths.
  log/<device>.jsonl   shared log zone, one writer per file (see log.py)
  local/               local-only zone (index.sqlite, ...)

The device id is a stable random id kept in `~/Library/Application Support/AVE/device.json`. Log file names, HLC stamps, the
`seen` maps and all ordering use this full id. The 4-character `short` tag derived from it is for display (and version file
names) only. Log files are never renamed: when Syncthing arrives, its device id is mapped to this one in project.json.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .. import atomic
from ..episode import detect_layout
from . import hlc as _hlc
from .log import DEVICE_ID_RE, Log, LogError, LogRead, ReadOnlyError, file_collisions, log_filename, log_files, read_logs
from .reducer import State, reduce

PROJECT_VERSION = 1
KINDS = ("episode", "studio")
UPDATE_NEEDED = "Update needed"


class ProjectError(Exception):
    pass


class ReadOnlyProject(ReadOnlyError, ProjectError):
    """A write refused because a newer AVE saved this project. Also a LogError and a ValueError, so every caller that
    reports those reports this, with its words."""


def _app_version() -> str:
    from .. import __version__
    return __version__


def newer_words(what: str) -> str:
    return f"This project was saved by a newer AVE ({what}). Update to edit it."


# Newest "app" stamp per log file: {path: (inode, size, mtime_ns, read up to, newest, the 64 bytes before that)}. A file
# that only grew is read from where the last read stopped (checked against those bytes), so a check costs a stat per file.
_STAMPS: dict[str, tuple] = {}
_STAMPS_LOCK = threading.Lock()


def _newer(a: str | None, b: str | None) -> str | None:
    from ..updates import compare_versions
    if not a:
        return b
    if not b:
        return a
    return a if compare_versions(a, b) >= 0 else b


def newest_writer(log_dir: Path) -> str | None:
    """The newest AVE version stamped on any event in the project's logs (`"app"`). Unstamped events count as no version."""
    best = None
    for path in log_files(log_dir):
        try:
            st = path.stat()
        except OSError:
            continue
        key = str(path)
        with _STAMPS_LOCK:
            hit = _STAMPS.get(key)
        if hit and hit[:3] == (st.st_ino, st.st_size, st.st_mtime_ns):
            best = _newer(best, hit[4])
            continue
        start, newest, tail = (hit[3], hit[4], hit[5]) if hit and hit[0] == st.st_ino and st.st_size >= hit[3] else (0, None, b"")
        try:
            with open(path, "rb") as f:
                f.seek(start - len(tail))
                chunk = f.read()
        except OSError:
            continue
        if not chunk.startswith(tail):         # rewritten rather than appended to: read it all again
            start, newest, tail = 0, None, b""
            try:
                chunk = path.read_bytes()
            except OSError:
                continue
        chunk = chunk[len(tail):]
        end = chunk.rfind(b"\n") + 1            # a line still being written is read next time, whole
        for line in chunk[:end].splitlines():
            if b'"app"' not in line:
                continue
            try:
                app = json.loads(line).get("app")
            except (ValueError, AttributeError):
                continue
            if isinstance(app, str) and app:
                newest = _newer(newest, app)
        done = start + end
        with _STAMPS_LOCK:
            _STAMPS[key] = (st.st_ino, st.st_size, st.st_mtime_ns, done, newest, (tail + chunk[:end])[-64:])
        best = _newer(best, newest)
    return best


def update_needed(meta: dict, app_version: str | None = None, newest: str | None = None) -> str | None:
    """Why this app must not write the project, or None. A project's version is the newest AVE that wrote to it: the
    `"app"` stamp on its log events (`newest`), at least the `app_version` project.json recorded at creation, plus the
    format number `v`. When either is newer than this app, the project is read-only here."""
    from ..updates import compare_versions
    app_version = app_version or _app_version()
    v = meta.get("v")
    if isinstance(v, int) and not isinstance(v, bool) and v > PROJECT_VERSION:
        return newer_words(f"project format {v}")
    created = meta.get("app_version") if isinstance(meta.get("app_version"), str) else None
    top = _newer(created, newest)
    if top and compare_versions(top, app_version) > 0:
        return newer_words(top)
    return None


def _project_root(path: Path, depth: int = 16) -> Path | None:
    for parent in list(Path(path).parents)[:depth]:
        if (parent / ".ave" / "project.json").is_file():
            return parent
    return None


def read_only_reason(root: Path) -> str | None:
    """update_needed() for the project at `root` ("This project was saved by a newer AVE (x.y)..."), None when it is
    writable here or is not a project."""
    pj = Path(root) / ".ave" / "project.json"
    try:
        meta = json.loads(pj.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    return update_needed(meta, newest=newest_writer(pj.parent / "log"))


def require_writable(root: Path) -> None:
    """Refuse (ReadOnlyProject) to write to a project a newer AVE saved. Nothing for a folder that is not a project."""
    reason = read_only_reason(root)
    if reason:
        raise ReadOnlyProject(reason)


def guard_write(path: Path) -> None:
    """The check every shared-file writer makes: inside a read-only project, a file that syncs (settings or permanent zone,
    project.json included) is refused. Local-only files (caches, work files) are still written."""
    path = Path(path)
    root = _project_root(path)
    if root is None:
        return
    try:
        require_writable(root)
    except ReadOnlyProject:
        from . import zones
        try:
            zone = zones.zone_of(path.absolute().relative_to(root.absolute()).as_posix(), zones.project_rules(root))
        except (ValueError, OSError, SystemExit):
            zone = "settings"
        if zone != "local":
            raise


def device_file() -> Path:
    return Path.home() / "Library" / "Application Support" / "AVE" / "device.json"


def short_id(device_id: str) -> str:
    digest = int.from_bytes(hashlib.sha256(device_id.encode()).digest()[:4], "big")
    return "".join(_hlc.CROCKFORD[(digest >> (5 * i)) & 31] for i in range(4))


@dataclass(frozen=True)
class Device:
    id: str
    short: str
    person: str | None = None

    @classmethod
    def load(cls) -> "Device":
        """This computer's device, created on first use. Two processes racing to create it agree on one id."""
        path = device_file()
        for _ in range(2):
            try:
                meta = json.loads(path.read_text())
                if isinstance(meta, dict) and isinstance(meta.get("device_id"), str) and meta["device_id"]:
                    return cls(meta["device_id"], short_id(meta["device_id"]), meta.get("person"))
                raise ProjectError(f"{path} has no device_id; move it aside and AVE will make a new one")
            except FileNotFoundError:
                pass
            except ValueError as exc:
                raise ProjectError(f"{path} is not valid JSON ({exc}); move it aside and AVE will make a new one") from exc
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".device.{os.getpid()}.tmp")
            tmp.write_text(json.dumps({"v": 1, "device_id": "dev_" + _hlc.ulid(),
                                       "created": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, indent=2) + "\n")
            try:
                _fsync(tmp)
                atomic.publish(tmp, path)
            except FileExistsError:
                pass
            finally:
                tmp.unlink(missing_ok=True)
        raise ProjectError(f"could not create {path}")


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Project:
    def __init__(self, root: Path, meta: dict, device: Device, wall: Callable[[], int] = _hlc.wall_ms):
        self.root = Path(root)
        self.meta = meta
        self.device = device
        self.wall = wall
        self.warnings: list[str] = []
        self._log: Log | None = None

    @property
    def update_needed(self) -> str | None:
        """"This project was saved by a newer AVE (x.y). Update to edit it." when a newer AVE wrote it, else None."""
        return update_needed(self.meta, newest=newest_writer(self.log_dir)) if isinstance(self.meta, dict) else None

    @property
    def read_only(self) -> bool:
        """A project saved by a newer AVE: read it, never write it (see update_needed)."""
        return bool(self.update_needed)

    # ---------------------------------------------------------------- paths
    @staticmethod
    def ave_dir_of(root: Path) -> Path:
        return Path(root) / ".ave"

    @property
    def ave_dir(self) -> Path:
        return self.ave_dir_of(self.root)

    @property
    def project_json(self) -> Path:
        return self.ave_dir / "project.json"

    @property
    def log_dir(self) -> Path:
        return self.ave_dir / "log"

    @property
    def local_dir(self) -> Path:
        return self.ave_dir / "local"

    # ---------------------------------------------------------------- identity
    @property
    def project_id(self) -> str:
        return self.meta["project_id"]

    @property
    def title(self) -> str:
        return self.meta.get("title", "")

    @property
    def kind(self) -> str:
        """"studio" or "episode": from project.json, else from the folder (a project made before kind was recorded)."""
        k = self.meta.get("kind")
        return k if k in KINDS else ("studio" if (self.root / "studio.json").is_file() else "episode")

    @property
    def publisher_device(self) -> str | None:
        return self.meta.get("publisher_device")

    @property
    def layout(self) -> Any:
        return self.meta.get("layout")

    @property
    def members(self) -> list[dict]:
        return list(self.meta.get("members") or [])

    @property
    def person(self) -> str:
        for m in self.members:
            if self.device.id in (m.get("devices") or []):
                return m.get("person") or _default_person(self.device)
        return _default_person(self.device)

    # ---------------------------------------------------------------- lifecycle
    @classmethod
    def create(cls, root: str | Path, title: str, *, person: str | None = None, layout: Any = None, kind: str | None = None,
               wall: Callable[[], int] = _hlc.wall_ms) -> "Project":
        """Add `.ave/` to a folder. Moves and rewrites nothing that is already there; refuses if a project exists. `kind` is
        recorded in project.json (default: "studio" when the folder has a studio.json), so a member joining from an
        invitation gets the studio rules before studio.json itself arrives."""
        root = Path(root)
        kind = kind or ("studio" if (root / "studio.json").is_file() else "episode")
        if kind not in KINDS:
            raise ProjectError(f"kind must be one of {KINDS}, got {kind!r}")
        root.mkdir(parents=True, exist_ok=True)
        pj = cls.ave_dir_of(root) / "project.json"
        if pj.exists():
            raise ProjectError(f"{root} is already a project ({pj})")
        device = Device.load()
        meta = {
            "v": PROJECT_VERSION,
            "project_id": "prj_" + _hlc.ulid(),
            "title": title,
            "kind": kind,
            "layout": layout if layout is not None else _layout_of(root),
            "members": [{"person": person or _default_person(device), "devices": [device.id], "media": "full"}],
            "publisher_device": device.id,
            "owner_device": device.id,      # the computer whose member.added / member.removed events decide membership
        }
        from .log import release_version
        if release_version():                # only a release records the version that made it (log.release_version)
            meta["app_version"] = release_version()
        for d in ("log", "local"):
            (pj.parent / d).mkdir(parents=True, exist_ok=True)
        tmp = pj.with_name(f".project.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
        try:
            _fsync(tmp)
            atomic.publish(tmp, pj)  # never replaces: a project made at the same moment elsewhere wins
        except FileExistsError as exc:
            raise ProjectError(f"{root} is already a project ({pj})") from exc
        finally:
            tmp.unlink(missing_ok=True)
        return cls(root, meta, device, wall)

    @classmethod
    def open(cls, root: str | Path, *, wall: Callable[[], int] = _hlc.wall_ms) -> "Project":
        root = Path(root)
        pj = cls.ave_dir_of(root) / "project.json"
        try:
            meta = json.loads(pj.read_text())
        except FileNotFoundError as exc:
            raise ProjectError(f"{root} is not a project (no {pj.relative_to(root)})") from exc
        except ValueError as exc:
            raise ProjectError(f"{pj} is not valid JSON ({exc})") from exc
        if not isinstance(meta, dict) or not isinstance(meta.get("project_id"), str):
            raise ProjectError(f"{pj} has no project_id")
        project = cls(root, meta, Device.load(), wall)
        needed = project.update_needed
        if needed is None and meta.get("v") != PROJECT_VERSION:
            raise ProjectError(f"{pj} is version {meta.get('v')!r}; this AVE reads version {PROJECT_VERSION}")
        if needed:
            project.warnings.append(needed)
        project.check_devices()
        return project

    def check_devices(self) -> None:
        """Refuse a project where two different device ids would share one log file (they differ only in letter case)."""
        ids = [self.device.id] + [d for m in self.members for d in (m.get("devices") or []) if isinstance(d, str)]
        try:
            for d in ids:
                log_filename(d)
        except LogError as exc:
            raise ProjectError(str(exc)) from exc
        for p in log_files(self.log_dir):  # already skips AppleDouble ._* files
            if DEVICE_ID_RE.match(p.stem):
                ids.append(p.stem)
            else:  # e.g. a Syncthing *.sync-conflict-*.jsonl copy: still read, but it names no device
                self.warnings.append(f"log: {p.name} is not named after a device; it is read but never written")
        clashes = file_collisions(ids)
        if clashes:
            a, b = clashes[0]
            raise ProjectError(f"devices {a} and {b} would share one log file on this volume; refusing to open the project log")

    # ---------------------------------------------------------------- log and state
    @property
    def log(self) -> Log:
        if self._log is None:
            self._log = Log(self.log_dir, self.device.id, self.person, wall=self.wall, lock_dir=self.local_dir / "locks")
            self._log.refuse = lambda: self.update_needed      # asked on every append: a newer peer's event can arrive any time
        return self._log

    def read_events(self) -> LogRead:
        return read_logs(self.log_dir)

    def state(self) -> State:
        """reduce() of every log, plus this computer's reading and clock warnings (which are local, not shared)."""
        events = self.read_events()
        st = reduce(events)
        st.warnings = sorted(set(st.warnings) | set(events.warnings) | set(self.log.warnings) | set(self.warnings))
        return st

    def index(self):
        from .index import Index
        return Index(self)


def _default_person(device: Device) -> str:
    if device.person:
        return device.person
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no login name in some sandboxes
        return "me"


def _layout_of(root: Path) -> Any:
    """The episode's own layout when it has an episode.json, else what the folder looks like."""
    ep = root / "episode.json"
    if ep.is_file():
        try:
            spec = json.loads(ep.read_text()).get("layout")
        except (OSError, ValueError, AttributeError):
            spec = None
        if spec is not None:
            return spec
        return "flat"
    return detect_layout(root)
