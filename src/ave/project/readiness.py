"""Readiness: is every file a version needs on this computer, whole and unchanged?

The project log syncs ahead of large files, so a `version.added` event can arrive before the edit file, its captions or its
footage. Nothing may open such a version. Each version is one of:

  ready      every need exists with the recorded size and sha256
  arriving   a need is missing, still being written (Syncthing's `.syncthing.<name>.tmp` sits beside a missing or short
             file), its size is not the recorded one yet, or its hash is still being checked
  stuck      arriving with no change for STUCK_S (10 minutes), or Syncthing's temp file has not grown for that long. Reason
             `sync` (nothing is coming: check sync) or `resend` (a file of the wrong size sits there with nothing copying:
             ask the sender to resend)
  damaged    the size matches but the sha256 does not

What counts as a need:
  * the event's `needs` ({path, bytes, sha256}). Footage and proxies are recorded with their size and `sha256: None` (never
    hashed: Syncthing checks every block it moves), so they are ready once they exist at their recorded size.
  * an old event with no `needs`: its `file` only, ready once it exists.
  * a need the writer recorded as missing on its own computer (`hash: "pending"` with no size) never holds a version back:
    nothing is on its way. (A writer that was itself still receiving that file records it the same way, so a third computer
    does not wait for it either; that is rare.)
  * camera originals on a member who syncs proxies only never arrive, so they are skipped; the proxy each one plays from is
    checked in its place: the `proxy_of` need recorded at save time (by size), else, for a computer listed as a proxies member,
    the proxy path derived from the layout (it must exist). A member with full media skips `proxy_of` needs: it plays the originals.

Cost. Each directory a need lives in is stat'ed once per look; a need's own stat is cached until that directory's mtime
changes (Syncthing renames each file into place, which changes it), `invalidate()` names it (a sync event), or RECHECK_S has
passed. Hashes live in memory first and in `.ave/local/index.sqlite` (table `hashes`, keyed on path, size and mtime_ns), read and
written through one long-lived connection per thread. `readiness()` never hashes on the caller's thread unless asked
(`wait=True`): an unknown hash is queued for the one background hasher and reads "arriving" (checking) until it is known. When a
hash cannot be saved it is still kept in memory, and saving is retried with an exponential back-off (capped at BACKOFF_MAX_S),
so the hasher never loops. `on_hashed()` listeners hear only of hashes that are new.

An artifact's readiness is its current version's (the kept one, else the newest); an artifact with no versions is ready.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path, PurePosixPath
from typing import Callable, Literal

from .reducer import State


class _LRU(OrderedDict):
    """A dict that keeps at most `cap` entries, dropping the least recently used (callers hold their own lock)."""

    def __init__(self, cap: int):
        super().__init__()
        self.cap = cap

    def get(self, k, default=None):
        if k in self:
            self.move_to_end(k)
            return super().__getitem__(k)
        return default

    def __setitem__(self, k, v):
        super().__setitem__(k, v)
        self.move_to_end(k)
        while len(self) > self.cap:
            self.popitem(last=False)

Status = Literal["ready", "arriving", "stuck", "damaged"]
RANK = {"ready": 0, "arriving": 1, "stuck": 2, "damaged": 3}
TEMP_PATTERNS = (".syncthing.{}.tmp", "~syncthing~{}.tmp")      # Syncthing's temp names on macOS/Linux and on Windows
CHUNK = 1 << 20
STUCK_S = 600.0              # a need unchanged this long, or a temp file this old, is stuck
RECHECK_S = 60.0             # even an unchanged directory is looked at again this often
BACKOFF_MAX_S = 300.0

SCHEMA = "CREATE TABLE IF NOT EXISTS hashes (path TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, " \
         "sha256 TEXT NOT NULL, PRIMARY KEY (path, size, mtime_ns))"

_now = time.time             # the clock (tests replace it)


def _stat(p: Path) -> os.stat_result:
    return os.stat(p)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


class NotReady(SystemExit):
    """A version that is not ready was asked for. Its text is the plain reason (a SystemExit, so a command prints it)."""

    def __init__(self, text: str, detail: dict | None = None):
        super().__init__(text)
        self.text, self.detail = text, detail or {}

    def __str__(self) -> str:
        return self.text


# ---------------------------------------------------------------------------------------------------- the hash cache
def index_path(root: Path) -> Path:
    return Path(root) / ".ave" / "local" / "index.sqlite"


_MEM: dict[tuple, str] = _LRU(50_000)                  # (root, rel, size, mtime_ns) -> sha256: the first level, always trusted
_MEM_LOCK = threading.Lock()
_TLS = threading.local()                     # this thread's long-lived sqlite connections: {db path: (inode, connection)}


def _connection(db: Path) -> sqlite3.Connection:
    """This thread's connection to `db`, reopened when the file was replaced (Index.refresh swaps it in) or is new."""
    conns = getattr(_TLS, "conns", None)
    if conns is None:
        conns = _TLS.conns = {}
    try:
        ino = _stat(db).st_ino
    except OSError:
        ino = None
    hit = conns.get(str(db))
    if hit is not None and hit[0] == ino and ino is not None:
        return hit[1]
    if hit is not None:
        try:
            hit[1].close()
        except sqlite3.Error:
            pass
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db, timeout=2.0, check_same_thread=True)
    con.execute(SCHEMA)
    con.commit()
    conns[str(db)] = (_stat(db).st_ino, con)
    return con


class HashCache:
    """sha256 by (path, size, mtime_ns): memory first, then the project's index.sqlite. Any sqlite failure reads as "not
    known" on get and raises on put (the hasher backs off)."""

    def __init__(self, root: Path):
        self.root = str(Path(root))
        self.path = index_path(root)

    def get(self, rel: str, size: int, mtime_ns: int) -> str | None:
        k = (self.root, rel, size, mtime_ns)
        with _MEM_LOCK:
            hit = _MEM.get(k)
            if hit is not None:
                return hit
        try:
            row = _connection(self.path).execute("SELECT sha256 FROM hashes WHERE path=? AND size=? AND mtime_ns=?",
                                                 (rel, size, mtime_ns)).fetchone()
        except (sqlite3.Error, OSError):
            return None
        if row:
            with _MEM_LOCK:
                _MEM[k] = row[0]
        return row[0] if row else None

    def remember(self, rel: str, size: int, mtime_ns: int, sha: str) -> bool:
        """Keep a hash in memory; True when it is new there."""
        k = (self.root, rel, size, mtime_ns)
        with _MEM_LOCK:
            new = _MEM.get(k) != sha
            _MEM[k] = sha
        return new

    def put(self, rel: str, size: int, mtime_ns: int, sha: str) -> None:
        """Remember and save. Raises sqlite3.Error / OSError when it cannot be saved (it is still remembered)."""
        self.remember(rel, size, mtime_ns, sha)
        con = _connection(self.path)
        try:
            con.execute("DELETE FROM hashes WHERE path=?", (rel,))       # only the file as it is now is worth keeping
            con.execute("INSERT OR REPLACE INTO hashes VALUES (?,?,?,?)", (rel, size, mtime_ns, sha))
            con.commit()
        except BaseException:
            try:
                con.rollback()                 # never leave this long-lived connection holding the write lock
            except sqlite3.Error:
                pass
            raise


def remember_hash(root: Path, rel: str, st: os.stat_result, sha: str) -> None:
    """A writer that just hashed a file it wrote (a version saved here) records it, so this computer never hashes it again."""
    try:
        HashCache(root).put(rel, st.st_size, st.st_mtime_ns, sha)
    except (sqlite3.Error, OSError):
        pass                                   # remembered in memory; the hasher saves it later if it is asked again


# ---------------------------------------------------------------------------------------------------- the background hasher
class Hasher:
    """One daemon thread that hashes queued files. Each file is queued at most once at a time; a result is remembered in
    memory even when it cannot be saved, and saving failures back off exponentially."""

    def __init__(self):
        self._q: queue.Queue = queue.Queue()
        self._pending: set[tuple] = set()
        self._unsaved: dict[tuple, str] = {}
        self._fails = 0
        self._retry_at = 0.0
        self._lock = threading.Lock()
        self._listeners: list[Callable[[Path], None]] = []
        self._thread: threading.Thread | None = None
        self._idle = threading.Event()
        self._idle.set()
        self.hashed = 0                        # files hashed (tests)
        self.save_attempts = 0

    def on_hashed(self, fn: Callable[[Path], None]) -> Callable[[], None]:
        """Call `fn(project_root)` after a new hash is known (or `invalidate()` names a project); returns an off switch."""
        with self._lock:
            self._listeners.append(fn)

        def off():
            with self._lock:
                if fn in self._listeners:
                    self._listeners.remove(fn)
        return off

    def notify(self, root: Path) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(Path(root))
            except Exception:  # noqa: BLE001 - a listener never stops the hasher
                pass

    def submit(self, root: Path, rel: str, size: int, mtime_ns: int) -> None:
        key = (str(root), rel, size, mtime_ns)
        with self._lock:
            if key in self._pending:
                return
            self._pending.add(key)
            self._idle.clear()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="readiness-hash", daemon=True)
                self._thread.start()
        self._q.put(key)

    def drain(self, timeout: float = 10.0) -> bool:
        """Wait until nothing is queued (tests, and a CLI that wants a settled answer)."""
        return self._idle.wait(timeout)

    def _save(self) -> None:
        """Save what is remembered but unsaved, unless still backing off from a failure."""
        with self._lock:
            if not self._unsaved or time.monotonic() < self._retry_at:
                return
            todo = dict(self._unsaved)
        for (root, rel, size, mtime_ns), sha in todo.items():
            self.save_attempts += 1
            try:
                HashCache(Path(root)).put(rel, size, mtime_ns, sha)
            except (sqlite3.Error, OSError):
                with self._lock:
                    self._fails += 1
                    self._retry_at = time.monotonic() + min(BACKOFF_MAX_S, 2.0 ** min(self._fails, 16))
                return
            with self._lock:
                self._unsaved.pop((root, rel, size, mtime_ns), None)
                self._fails = 0

    def _run(self) -> None:
        while True:
            try:
                key = self._q.get(timeout=5.0)
            except queue.Empty:
                self._save()
                with self._lock:
                    if not self._pending and not self._unsaved:
                        self._thread = None
                        return
                continue
            root, rel, size, mtime_ns = key
            new = False
            try:
                cache = HashCache(Path(root))
                if cache.get(rel, size, mtime_ns) is None:
                    p = Path(root) / rel
                    st = _stat(p)
                    if (st.st_size, st.st_mtime_ns) == (size, mtime_ns):
                        sha = _sha256(p)
                        self.hashed += 1
                        st2 = _stat(p)
                        if (st2.st_size, st2.st_mtime_ns) == (size, mtime_ns):     # it did not change while it was read
                            new = cache.remember(rel, size, mtime_ns, sha)
                            with self._lock:
                                self._unsaved[key] = sha
            except OSError:
                pass
            finally:
                self._save()
                with self._lock:
                    self._pending.discard(key)
                    if not self._pending:
                        self._idle.set()
            if new:
                self.notify(Path(root))


HASHER = Hasher()
on_hashed = HASHER.on_hashed
drain = HASHER.drain


# ---------------------------------------------------------------------------------------------------- stat cache
class _Dir:
    __slots__ = ("mtime_ns", "at", "names")

    def __init__(self, mtime_ns, at):
        self.mtime_ns, self.at, self.names = mtime_ns, at, {}


_DIRS: dict[str, _Dir] = _LRU(5_000)
_SEEN: dict[tuple, tuple] = _LRU(20_000)               # (root, rel) -> (observation, first seen at): when a need last changed
_CACHE_LOCK = threading.Lock()


class _Look:
    """One look: each directory is stat'ed at most once, and a file's stat is reused while its directory is unchanged."""

    def __init__(self):
        self.dirs: dict[str, _Dir | None] = {}

    def _dir(self, d: Path) -> _Dir | None:
        key = str(d)
        if key in self.dirs:
            return self.dirs[key]
        try:
            mt = _stat(d).st_mtime_ns
        except OSError:
            mt = None
        now = time.monotonic()
        with _CACHE_LOCK:
            cur = _DIRS.get(key)
            if cur is None or cur.mtime_ns != mt or mt is None or now - cur.at >= RECHECK_S:
                cur = _DIRS[key] = _Dir(mt, now)
        self.dirs[key] = cur
        return cur

    def stat(self, p: Path):
        """(size, mtime_ns, is_file) or None when missing."""
        d = self._dir(p.parent)
        if d is not None and d.mtime_ns is None:
            return None                         # the folder itself is not there
        with _CACHE_LOCK:
            if d is not None and p.name in d.names:
                return d.names[p.name]
        try:
            st = _stat(p)
            got = (st.st_size, st.st_mtime_ns, _isreg(st))
        except OSError:
            got = None
        with _CACHE_LOCK:
            if d is not None:
                d.names[p.name] = got
        return got


def _isreg(st: os.stat_result) -> bool:
    import stat as _st
    return _st.S_ISREG(st.st_mode)


def invalidate(root: Path, paths=None) -> None:
    """Forget cached stats under `root` (only the folders holding `paths`, when given: e.g. Syncthing's ItemFinished items)
    and tell the listeners to look again."""
    root = Path(root)
    with _CACHE_LOCK:
        if paths is None:
            for k in [k for k in _DIRS if k == str(root) or k.startswith(str(root) + os.sep)]:
                _DIRS.pop(k, None)
        else:
            for rel in paths:
                r = _rel(rel)
                if r is not None:
                    _DIRS.pop(str((root / r).parent), None)
    HASHER.notify(root)


def reset_caches() -> None:
    """Forget every cached stat, observation and remembered hash (tests)."""
    with _CACHE_LOCK:
        _DIRS.clear()
        _SEEN.clear()
    _POLICIES.clear()
    _ROOTS.clear()
    with _STATES_LOCK:
        _STATES.clear()
    with _MEM_LOCK:
        _MEM.clear()


# ---------------------------------------------------------------------------------------------------- one need
def _rel(path) -> str | None:
    if not isinstance(path, str) or not path:
        return None
    p = PurePosixPath(path.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts:
        return None
    return p.as_posix()


def _stuck(root: Path, rel: str, observation: tuple, status: str, reason: str) -> tuple[str, str]:
    """An arriving need whose observation has not changed for STUCK_S is stuck."""
    now = _now()
    k = (str(root), rel)
    with _CACHE_LOCK:
        seen = _SEEN.get(k)
        if seen is None or seen[0] != observation:
            _SEEN[k] = (observation, now)
            return status, reason
        since = seen[1]
    if now - since >= STUCK_S:
        return "stuck", ("resend" if reason == "size" else "sync")
    return status, reason


def check_need(root: Path, need: dict, *, cache: HashCache | None = None, wait: bool = False,
               never_here: Callable[[str], bool] | None = None, look: _Look | None = None,
               mine: bool = False) -> tuple[Status, str | None]:
    """(status, why) for one need. `why` is a plain reason for anything not ready: missing, copying, size, checking, hash,
    and for stuck: sync, resend or removed (a file of a version made on this computer, or one Syncthing moved to its trash
    folder `.stversions`, was deleted here: nothing will bring it back by itself)."""
    root = Path(root)
    look = look or _Look()
    rel = _rel(need.get("path"))
    if rel is None:
        return "ready", None                          # outside the project: it cannot sync, so it cannot be a need
    p = root / rel
    st = look.stat(p)
    want_bytes = need.get("bytes")
    sized = isinstance(want_bytes, int) and not isinstance(want_bytes, bool)
    if st is None or not st[2] or (sized and st[0] != want_bytes):
        temp = None
        for pat in TEMP_PATTERNS:                     # a temp file matters only while the file itself is not whole
            t = look.stat(p.parent / pat.format(p.name))
            if t is not None:
                temp = t
                break
        if temp is not None:
            if _now() - temp[1] / 1e9 >= STUCK_S:
                return "stuck", "sync"                # Syncthing's copy has not moved for ten minutes
            return _stuck(root, rel, ("temp", st, temp), "arriving", "copying")
        if st is None or not st[2]:
            if never_here is not None and never_here(rel):
                return "ready", None                  # e.g. camera originals for a member on proxies: they never come here
            if need.get("hash") == "pending" and need.get("bytes") is None and not need.get("sha256"):
                return "ready", None                  # missing on the computer that wrote it too: nothing is on its way
            if mine or look.stat(root / ".stversions" / rel) is not None:
                return "stuck", "removed"             # deleted on this computer: it is not on its way
            return _stuck(root, rel, ("missing",), "arriving", "missing")
        return _stuck(root, rel, ("size", st[0], st[1]), "arriving", "size")
    with _CACHE_LOCK:
        _SEEN.pop((str(root), rel), None)
    want = need.get("sha256")
    if not isinstance(want, str) or not want:
        return "ready", None                          # footage, proxies and old events: size (when known) is the check
    cache = cache or HashCache(root)
    size, mtime_ns = st[0], st[1]
    got = cache.get(rel, size, mtime_ns)
    if got is None:
        if not wait:
            HASHER.submit(root, rel, size, mtime_ns)
            return "arriving", "checking"
        try:
            got = _sha256(p)
            HASHER.hashed += 1
        except OSError:
            return "arriving", "missing"
        try:
            cache.put(rel, size, mtime_ns, got)
        except (sqlite3.Error, OSError):
            pass                                      # remembered in memory
    if got.lower() == want.lower():
        return "ready", None
    return "damaged", "hash"


# ---------------------------------------------------------------------------------------------------- this member's policy
class _Policy:
    """What syncs to this computer: originals never (proxies member) or proxies skipped (full member), and where proxies go."""

    def __init__(self, root: Path):
        self.never_here: Callable[[str], bool] | None = None
        self.media = "full"
        self.listed = False                    # this computer is a member of the project (not the unlisted "proxies" fallback)
        self.device: str | None = None
        self.raw_dirs: list[str] = []
        try:
            from .zones import _this_device_id, project_layout, project_rules, zone_of
            from ..episode import resolve_layout
            rules = project_rules(root)
            self.never_here = lambda rel: zone_of(rel, rules) == "local"
            meta = json.loads((Path(root) / ".ave" / "project.json").read_text())
            dev = self.device = _this_device_id()
            member = next((m for m in meta.get("members") or [] if isinstance(m, dict) and dev in (m.get("devices") or [])), None)
            self.listed = member is not None
            self.media = (member or {"media": "proxies"}).get("media", "full")
            self.raw_dirs = [str(PurePosixPath(d.strip("/"))) for d in resolve_layout(project_layout(root, meta))["raw_dirs"]]
        except Exception:  # noqa: BLE001 - without rules every need counts
            pass

    def proxy_of(self, rel: str) -> str | None:
        """The proxy path a camera original plays from (`<raw>/proxies/<rel>.mov`), or None outside a raw folder."""
        for d in self.raw_dirs:
            if rel.startswith(d + "/"):
                inner = PurePosixPath(rel[len(d) + 1:])
                if inner.parts and inner.parts[0] == "proxies":
                    return None
                return f"{d}/proxies/{inner.with_suffix('.mov').as_posix()}"
        return None

    def needs(self, needs: list[dict]) -> list[dict]:
        """The needs as this computer checks them: a full member skips proxies; a proxies member checks, for each original it
        never receives, its recorded proxy need, or the derived proxy path."""
        if self.media != "proxies":
            return [n for n in needs if not n.get("proxy_of")]
        recorded = {n.get("proxy_of") for n in needs if n.get("proxy_of")}
        out = list(needs)
        for n in needs:
            rel = _rel(n.get("path"))
            if (self.listed and rel and not n.get("proxy_of") and rel not in recorded and self.never_here
                    and self.never_here(rel)):
                px = self.proxy_of(rel)
                if px:
                    out.append({"path": px, "proxy_of": rel})       # existence only: its size was never recorded
        return out


# ---------------------------------------------------------------------------------------------------- versions and artifacts
_POLICIES: dict[str, tuple] = _LRU(500)


def policy_of(root: Path) -> _Policy:
    """This member's policy for the project at `root`, cached on project.json's and episode.json's stats."""
    root = Path(root)
    sig = []
    for f in (root / ".ave" / "project.json", root / "episode.json", root / "studio.json"):
        try:
            s = _stat(f)
            sig.append((s.st_size, s.st_mtime_ns))
        except OSError:
            sig.append(None)
    from .zones import _this_device_id
    sig.append(_this_device_id())
    with _CACHE_LOCK:
        hit = _POLICIES.get(str(root))
    if hit is not None and hit[0] == sig:
        return hit[1]
    pol = _Policy(root)
    with _CACHE_LOCK:
        _POLICIES[str(root)] = (sig, pol)
    return pol


def _never_here(root: Path) -> Callable[[str], bool] | None:
    return policy_of(root).never_here


def needs_of(version: dict) -> list[dict]:
    """A version's needs; an old event without any needs only its own file."""
    needs = [n for n in (version.get("needs") or []) if isinstance(n, dict)]
    if not needs and isinstance(version.get("file"), str):
        needs = [{"path": version["file"]}]
    return needs


def version_detail(root: Path, version: dict, *, wait: bool = False, cache: HashCache | None = None,
                   never_here: Callable[[str], bool] | None = None, policy: _Policy | None = None,
                   look: _Look | None = None) -> dict:
    """{status, reason, path, from}: the worst need decides (damaged over stuck over arriving over ready); `path` is that
    need's file, `from` the person whose computer made the version."""
    cache = cache or HashCache(root)
    policy = policy or policy_of(root)
    look = look or _Look()
    nh = never_here or policy.never_here
    status, reason, path = "ready", None, None
    mine = bool(version.get("device")) and version.get("device") == policy.device
    for n in policy.needs(needs_of(version)):
        s, why = check_need(root, n, cache=cache, wait=wait, never_here=nh, look=look, mine=mine)
        if RANK[s] > RANK[status]:
            status, reason, path = s, why, n.get("path")
    return {"status": status, "reason": reason, "path": path, "from": version.get("person"), "actor": version.get("actor"),
            "number": version.get("number")}


def current_version(artifact) -> dict | None:
    """The kept version if it is known here, else the newest by number."""
    vs = artifact.versions
    if artifact.kept and artifact.kept in vs:
        return vs[artifact.kept]
    return latest_version(artifact)


def latest_version(artifact) -> dict | None:
    vs = artifact.versions
    return max(vs.values(), key=lambda v: v["number"]) if vs else None


def details(state: State, root: Path, *, wait: bool = False) -> dict[str, dict]:
    """`readiness()` with the reasons: {id: {status, reason, path, from, ...}} for every version and artifact."""
    root = Path(root)
    cache, policy, look = HashCache(root), policy_of(root), _Look()
    out: dict[str, dict] = {}
    for aid, a in state.artifacts.items():
        per = {}
        for vid, v in a.versions.items():
            per[vid] = out[vid] = {**version_detail(root, v, wait=wait, cache=cache, policy=policy, look=look), "artifact": aid,
                                   "version": vid}
        cur = current_version(a)
        out[aid] = dict(per[cur["version"]]) if cur else {"status": "ready", "reason": None, "path": None, "from": None,
                                                          "actor": None, "version": None, "number": None}
        out[aid]["artifact"] = aid
    return out


def readiness(state: State, root: Path, *, wait: bool = False) -> dict[str, Status]:
    """{id: "ready" | "arriving" | "stuck" | "damaged"} for every version id and artifact id in `state`. With `wait=False` (the
    default) no file is hashed here: an unknown hash is queued for the background hasher and reads "arriving" until known."""
    return {k: d["status"] for k, d in details(state, root, wait=wait).items()}


def find_version(state: State, version: str) -> tuple[str, dict] | None:
    for aid, a in state.artifacts.items():
        if version in a.versions:
            return aid, a.versions[version]
    return None


_STATES: dict[str, tuple] = _LRU(200)
_STATES_LOCK = threading.Lock()


def _state_of(root: Path) -> State | None:
    """The reduced log of the project at `root`, cached on its log files' stats; None when it is not a project."""
    root = Path(root)
    if not (root / ".ave" / "project.json").is_file():
        return None
    from .log import log_files, read_logs
    from .reducer import reduce
    try:
        fp = tuple((p.name, *(lambda s: (s.st_size, s.st_mtime_ns, s.st_ino))(p.stat())) for p in log_files(root / ".ave" / "log"))
    except OSError:
        fp = None
    with _STATES_LOCK:
        hit = _STATES.get(str(root))
    if hit is not None and fp is not None and hit[0] == fp:
        return hit[1]
    try:
        st = reduce(read_logs(root / ".ave" / "log"))
    except Exception:  # noqa: BLE001 - a log that cannot be read leaves the old behaviour in place
        return None
    with _STATES_LOCK:
        _STATES[str(root)] = (fp, st)
    return st


def version_status(root: Path, version: str, *, wait: bool = False) -> dict | None:
    """The readiness detail of one version of the project at `root`, or None when `root` is not a project or the log does not
    know the version (a file the log never recorded is judged by the caller, as before)."""
    st = _state_of(root)
    hit = find_version(st, version) if st is not None else None
    if hit is None:
        return None
    aid, v = hit
    return {**version_detail(Path(root), v, wait=wait), "artifact": aid, "version": version}


def _what(version: str, number: int | None) -> str:
    from .board import version_name
    w = version_name(version)               # the file's own number: what --version takes
    return w[:1].upper() + w[1:]


def refusal(root: Path, version: str, what: str | None = None, *, wait: bool | None = None) -> str | None:
    """Why `version` of the project at `root` must not be opened, rendered or handed off yet (a plain sentence), or None when
    it may be. Hashes inline on the main thread (the CLI, the MCP server); on any other thread (the app's request threads) an
    unknown hash is left to the background hasher and reads as being checked."""
    d = version_status(root, version, wait=_main_thread() if wait is None else wait)
    if d is None or d["status"] == "ready":
        return None
    return say(d, what or _what(version, d.get("number")))


def require_ready(root: Path, edit_path: Path) -> None:
    """NotReady with the reason when the edit file at `edit_path` is a logged version that is not ready."""
    name = Path(edit_path).name
    vid = name[:-len(".json")] if name.endswith(".json") else name
    d = version_status(root, vid, wait=_main_thread())
    if d is not None and d["status"] != "ready":
        raise NotReady(say(d, _what(vid, d.get("number"))), d)


# ---------------------------------------------------------------------------------------------------- the choke point for edit files
_ROOTS: dict[str, str] = _LRU(2_000)


def project_root_of(path: Path) -> Path | None:
    """The project holding the edit file at `path` (the nearest `.ave/project.json` within four folders up)."""
    d = Path(path).parent
    key = str(d)
    with _CACHE_LOCK:
        r = _ROOTS.get(key)
    if r is not None and (Path(r) / ".ave" / "project.json").is_file():
        return Path(r)
    found = None
    for c in [d, *list(d.parents)[:3]]:
        if (c / ".ave" / "project.json").is_file():
            found = c
            break
    if found is not None:                              # "not a project" is never cached: .ave/ may appear at any time
        with _CACHE_LOCK:
            _ROOTS[key] = str(found)
    return found


def guard_load(path: Path) -> None:
    """Called by editdoc.load before an edit file is parsed: refuse a logged version that is not ready."""
    from ..editdoc import VERSION_RE
    p = Path(path)
    if not VERSION_RE.match(p.name):
        return
    root = project_root_of(p)
    if root is not None:
        require_ready(root, p)


_CHOICE: ContextVar[dict | None] = ContextVar("ave_version_choice", default=None)


@contextmanager
def choosing(flag: str | None = None, *, explicit: bool = False):
    """What the running command lets a person choose, for the refusal's next step: `flag` is its option naming a version
    ("--version"), or None when it has none; `explicit` says the person chose the stage or version themselves (then a version
    deleted on this computer does not block the one before)."""
    tok = _CHOICE.set({"flag": flag, "explicit": explicit})
    try:
        yield
    finally:
        _CHOICE.reset(tok)


def newest_logged(root: Path, stage: str, name: str | None = None) -> dict | None:
    """The log's newest version of one edit (stage, name), or None (not a project, or never logged)."""
    st = _state_of(root)
    if st is None:
        return None
    from . import hlc as _hlc
    from .board import _VERSION_ID
    newest = None
    for a in st.artifacts.values():
        for vid, v in a.versions.items():
            m = _VERSION_ID.match(vid)
            if not m or m["stage"] != stage or (m["name"] or None) != (name or None):
                continue
            if newest is None or _hlc.parse(v["hlc"]) > _hlc.parse(newest["hlc"]):
                newest = v
    return newest


def guard_latest(root: Path, stage: str, name: str | None, picked: Path | None) -> None:
    """Called by editdoc.latest_path: refuse when the log's newest version of this edit (stage, name) is not ready here, so
    "the latest" never silently means the one before, and the first version of a stage that has not arrived is never "no edit
    yet". The next step names the running command's version option when it has one (see `choosing`), else to wait or check
    sync. A version deleted on this computer (stuck, removed) lets an explicit choice of stage or version through."""
    newest = newest_logged(root, stage, name)
    if newest is None:
        return
    picked_id = picked.name[:-len(".json")] if picked is not None else None
    d = version_detail(Path(root), newest, wait=_main_thread())
    if d["status"] == "ready":
        return                                       # the newest is here and whole: the files decide which one is latest
    choice = _CHOICE.get() or {}
    older = picked is not None and picked_id != newest["version"]
    if d.get("reason") == "removed" and choice.get("explicit") and older:
        return                                       # deleted here on purpose, and the person chose: the one before stands
    text = say(d, _what(newest["version"], newest.get("number")))
    m = None
    if older:
        from ..editdoc import VERSION_RE
        m = VERSION_RE.match(picked.name)
    if m and choice.get("flag"):
        text += f" To use the version before on purpose, name it: {choice['flag']} {int(m['n'])}."
    elif d.get("reason") != "removed":
        text += " Nothing else can be used in its place here; if it does not arrive, check sync status."
    raise NotReady(text, {**d, "version": newest["version"]})


# ---------------------------------------------------------------------------------------------------- words
def _name(person: str | None) -> str:
    return (person[:1].upper() + person[1:]) if person else "another computer"


def say(detail: dict, what: str = "This version") -> str:
    """One plain sentence for a version that is not ready, naming the person it comes from (never a device id), with the next
    step."""
    who = _name(detail.get("from"))
    if detail.get("status") == "damaged":
        f = PurePosixPath(detail.get("path") or "").name or "one of its files"
        return f"{what} arrived damaged: {f} does not match what {who} saved. Ask {who} to resend it."
    if detail.get("status") == "stuck":
        if detail.get("reason") == "removed":
            f = PurePosixPath(detail.get("path") or "").name or "one of its files"
            return (f"{what} is stuck: {f} was deleted on this computer, so it will not arrive by itself. Put it back (Syncthing "
                    f"keeps deleted files in the .stversions folder), ask {who} to resend it, or choose a stage or version on "
                    f"purpose.")
        if detail.get("reason") == "resend":
            f = PurePosixPath(detail.get("path") or "").name or "one of its files"
            return (f"{what} is stuck: {f} has not been the size {who} saved for ten minutes, and nothing is copying it. "
                    f"Ask {who} to resend it.")
        return (f"{what} is stuck: nothing more has arrived from {who} for ten minutes. Check sync status: {who}'s computer may "
                f"be off or not syncing.")
    if detail.get("reason") == "checking":
        return f"{what} is arriving from {who}: its files are here and are being checked. Try again in a moment."
    return f"{what} is still arriving from {who}: its files have not all reached this computer yet. Wait until it has arrived."


__all__ = ["HASHER", "HashCache", "NotReady", "Status", "check_need", "choosing", "newest_logged", "current_version", "details", "drain", "find_version",
           "guard_latest", "guard_load", "invalidate", "latest_version", "needs_of", "on_hashed", "readiness", "refusal",
           "remember_hash", "require_ready", "reset_caches", "say", "version_detail", "version_status"]
