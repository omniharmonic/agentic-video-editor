"""Keep each project in sync between its members' computers, with a bundled Syncthing run under the app's control.

    m = SyncManager()              # listens on this computer's Tailscale address; waits for Tailscale if it is not up
    m.start(); m.device_id()
    m.share(project, peers)        # one Syncthing folder per project, shared only with the listed members' devices
    m.share(project)               # the same, with the member devices project.json records (see members.py)
    m.status(project).state        # one of STATES; words.say(status) puts it in plain words for the Workspace

The package is `ave.syncthing` because `ave.sync` is the multicam audio sync module.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import config as C
from .api import Api, ApiError, q
from .config import FOLDER_DEFAULTS, SYNC_OPTIONS, ConfigError, Peer
from .supervisor import Supervisor, SyncError, default_home, find_binary

STATES = ("up_to_date", "syncing", "waiting", "low_disk", "drive_missing", "tailscale_off", "sync_off", "not_shared", "error")
SOMEONE = "another computer"            # what a member is called when no person's name is known: never a device id
_BUSY = {"scanning", "scan-waiting", "syncing", "sync-waiting", "sync-preparing", "cleaning", "clean-waiting"}
_MISSING = ("folder path missing", "marker missing", "no such file or directory")
_NO_SPACE = ("insufficient space", "not enough space", "no space left")
_UNITS = {"": 1, "b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12, "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3,
          "tib": 1024 ** 4}


@dataclass
class MemberStatus:
    """One other member's computer: offline | not_joined | paused | checking | sending | up_to_date."""
    name: str
    state: str
    need_items: int = 0                 # what this member still needs from here
    need_bytes: int = 0


@dataclass
class SyncStatus:
    state: str
    need_bytes: int = 0
    need_items: int = 0
    waiting_for: list[str] = field(default_factory=list)     # names of member devices that are not there (yet)
    error: str = ""
    members: list[MemberStatus] = field(default_factory=list)
    free_bytes: int | None = None                            # low_disk: what is left on the project's drive
    min_free_bytes: int | None = None                        # low_disk: the floor sync keeps free


def tailscale_listen() -> str:
    from ..collab_identity import IdentityError, MacIdentity
    try:
        address = MacIdentity().local_identity()["address"]
    except (IdentityError, OSError, KeyError) as exc:
        raise SyncError(f"Tailscale is not available ({exc}); sync stays off rather than listen on another network") from None
    return C.listen_url(address)


def _root_of(project) -> Path:
    """A Project, or a project folder path (a Path's own `.root` is "/", so paths are checked first). Resolved."""
    import os
    return C.real(project if isinstance(project, (str, os.PathLike)) else project.root)


def _read_meta(root: Path) -> dict:
    meta = json.loads((root / ".ave" / "project.json").read_text())
    if not isinstance(meta, dict):
        raise ConfigError(f"{root}/.ave/project.json must hold a JSON object")
    C.check_project_id(meta.get("project_id"))
    return meta


def decide(st: dict, peers: list[str], connected: set[str], completions: dict[str, dict], indexed: set[str],
           names: Callable[[str], str] = lambda d: SOMEONE) -> SyncStatus:
    """The status from Syncthing's numbers. `st` is /rest/db/status, `completions` /rest/db/completion per connected
    member, `indexed` the connected members that sent an index for this folder since they connected. Up to date needs
    every member connected, with a valid remote state, complete, and indexed since connecting."""
    need_b, need_i = int(st.get("needBytes") or 0), int(st.get("needTotalItems") or 0)
    busy = st.get("state") in _BUSY or bool(need_i and connected)
    waiting = [p for p in peers if p not in connected]
    members = []
    for p in peers:
        if p not in connected:
            members.append(MemberStatus(names(p), "offline"))
            continue
        c = completions.get(p) or {}
        rs = c.get("remoteState", "unknown")
        items, nbytes = int(c.get("needItems") or 0) + int(c.get("needDeletes") or 0), int(c.get("needBytes") or 0)
        if rs in ("notSharing", "paused"):
            waiting.append(p)
            members.append(MemberStatus(names(p), "not_joined" if rs == "notSharing" else "paused"))
        elif rs != "valid" or p not in indexed:
            busy = True
            members.append(MemberStatus(names(p), "checking"))
        elif float(c.get("completion", 0)) < 100 or items or nbytes:
            busy = True
            members.append(MemberStatus(names(p), "sending", items, nbytes))
        else:
            members.append(MemberStatus(names(p), "up_to_date"))
    who = [names(p) for p in waiting]
    if busy:
        return SyncStatus("syncing", need_b, need_i, who, members=members)
    if waiting:
        return SyncStatus("waiting", need_b, need_i, who, members=members)
    return SyncStatus("up_to_date", need_b, need_i, members=members)


def disk_bytes(min_free: dict | None) -> int | None:
    """Syncthing's {"value", "unit"} size as bytes; None for a percentage (it depends on the drive)."""
    if not isinstance(min_free, dict) or str(min_free.get("unit", "")).strip() == "%":
        return None
    try:
        return int(float(min_free.get("value") or 0) * _UNITS[str(min_free.get("unit", "")).strip().lower()])
    except (KeyError, TypeError, ValueError):
        return None


def free_space(root: Path) -> int | None:
    import shutil
    try:
        return shutil.disk_usage(root).free
    except OSError:
        return None


def _parse_time(value) -> float | None:
    """Syncthing's RFC 3339 time (nanoseconds allowed) as a Unix time, or None."""
    import re
    from datetime import datetime
    if not isinstance(value, str) or not value or value.startswith("0001-"):
        return None
    v = re.sub(r"(\.\d{6})\d+", r"\1", value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(v).timestamp()
    except ValueError:
        return None


class SyncManager:
    SETTLE_S = 10.0                    # a connection this old with a valid, complete remote state is up to date
    def __init__(self, home: Path | None = None, listen: str | None = None, *, binary: Path | None = None,
                 ave_device: str | None = None, name: str = "", allow_loopback: bool = False,
                 listen_provider: Callable[[], str] | None = None, tailscale_poll_s: float = 30.0):
        self.home = Path(home) if home else default_home()
        self._listen, self._binary, self._ave_device, self.name = listen, binary, ave_device, name
        self.allow_loopback = allow_loopback
        self._provider = listen_provider or tailscale_listen
        self.tailscale_poll_s = tailscale_poll_s
        self.supervisor: Supervisor | None = None
        self.waiting_reason = ""
        self.start_error = ""
        self._peer_names: dict[str, str] = {}
        self._stopping = threading.Event()
        self._poller: threading.Thread | None = None
        self._ev_thread: threading.Thread | None = None
        self._ev_lock = threading.Lock()
        self._conn_epoch: dict[str, object] = {}          # device -> its latest DeviceConnected event id (the connection)
        self._indexed: dict[tuple[str, str], object] = {} # (device, folder) -> connection in which its index became known
        self._serial = 0
        self._ev_gen: tuple | None = None                # (supervisor, restart count) the tracking below belongs to
        self._conn_seen: dict[str, float] = {}           # device -> monotonic time the follower saw it connect
        self._since = 0
        self.pairing: set[str] = set()                   # devices known only for a pairing in progress: never pruned
        self.on_refresh_error: Callable[[str, Exception], None] | None = None
        self.refresh_errors: dict[str, str] = {}         # folder -> why following another member's change failed
        self._config_lock = threading.RLock()            # share, unshare and prune never interleave
        self.min_disk_free = dict(C.MIN_DISK_FREE)       # each folder's floor of free space (tests raise it past the disk)
        self._quick = threading.local()                  # a per-thread short timeout for status looks (see quick())

    @property
    def ave_device(self) -> str:
        if self._ave_device is None:
            from ..project import Device
            self._ave_device = Device.load().id
        return self._ave_device

    @property
    def api(self) -> Api:
        if not self.supervisor:
            raise SyncError("sync is not started" + (f": {self.waiting_reason}" if self.waiting_reason else ""))
        t = getattr(self._quick, "timeout", None)
        if t is not None:                           # a status look: never wait for a Syncthing that is not answering
            api = self.supervisor.api
            if not (self.supervisor.running() and api):
                raise SyncError("syncthing is not running")
            return Api(api.socket_path, api.key, timeout=t)
        return self.supervisor.wait_ready()

    def quick(self, timeout: float = 2.0):
        """`with m.quick(): m.status(p)`: every Syncthing call on this thread fails after `timeout` seconds instead of
        waiting up to 30 s for a Syncthing that is restarting or hung, so a status look is always short."""
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            before = getattr(self._quick, "timeout", None)
            self._quick.timeout = timeout
            try:
                yield self
            finally:
                self._quick.timeout = before
        return _cm()

    @property
    def state(self) -> str:
        """stopped | waiting_for_tailscale | error (still retrying; see start_error) | starting | running | restarting"""
        if self.supervisor and self.supervisor.wanted:
            return self.supervisor.state
        if self._poller and self._poller.is_alive():
            return "error" if self.start_error else "waiting_for_tailscale"
        return "stopped"

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Start sync. Without Tailscale, keep checking every `tailscale_poll_s` and start once it is up."""
        if (self.supervisor and self.supervisor.wanted) or (self._poller and self._poller.is_alive()):
            return
        self._stopping.clear()
        if self._listen:
            self._start_on(C.listen_url(self._listen, allow_loopback=self.allow_loopback))
            return
        try:
            listen = self._provider()
        except (SyncError, ConfigError) as exc:
            self.waiting_reason = str(exc)
            self.start_error = str(exc) if isinstance(exc, ConfigError) else ""
            self._poller = threading.Thread(target=self._wait_for_tailscale, name="ave-sync-tailscale", daemon=True)
            self._poller.start()
            return
        self._start_on(listen)

    def _wait_for_tailscale(self) -> None:
        while not self._stopping.wait(self.tailscale_poll_s):
            try:
                listen = self._provider()
            except SyncError as exc:
                self.waiting_reason, self.start_error = str(exc), ""
                continue
            except ConfigError as exc:              # e.g. Tailscale handed us an address outside its ranges
                self.waiting_reason, self.start_error = str(exc), str(exc)
                continue
            try:
                self._start_on(listen)
                self.waiting_reason = self.start_error = ""
            except (SyncError, ConfigError, ApiError) as exc:
                self.waiting_reason = self.start_error = str(exc)
                continue
            return

    def _start_on(self, listen: str) -> None:
        sup = Supervisor(self._binary or find_binary(), self.home, listen, allow_loopback=self.allow_loopback)
        self._serial += 1
        sup.serial = self._serial                   # event tracking belongs to one supervisor
        self.supervisor = sup
        sup.start()
        if self._stopping.is_set():                 # stop() ran while this was starting
            sup.stop()
            raise SyncError("sync was stopped while starting")
        if not (self._ev_thread and self._ev_thread.is_alive()):
            self._ev_thread = threading.Thread(target=self._follow_events, name="ave-sync-events", daemon=True)
            self._ev_thread.start()
        try:
            self.api.patch("/rest/config/options", C.options(listen, allow_loopback=self.allow_loopback))
            self.api.patch(f"/rest/config/devices/{q(self.device_id())}", {"name": self.name})
        except BaseException:
            sup.stop()                              # never leave a half-configured Syncthing running
            raise

    def stop(self) -> None:
        self._stopping.set()
        if self.supervisor:
            self.supervisor.stop()
        for t in (self._poller, self._ev_thread):
            if t and t is not threading.current_thread():
                t.join(45)
        if self.supervisor:
            self.supervisor.stop()                  # one the waiting loop started after the first read above

    def device_id(self) -> str:
        if self.supervisor and self.supervisor.running():
            return self.api.get("/rest/system/status")["myID"]
        return Supervisor(self._binary or find_binary(), self.home, self._listen or "127.0.0.1", allow_loopback=True).device_id()

    # ------------------------------------------------------------------ events
    def _follow_events(self) -> None:
        while not self._stopping.is_set():
            sup = self.supervisor
            if not sup or not sup.wanted:
                return
            gen = (sup.serial, sup.restarts)
            if gen != self._ev_gen:                 # a new Syncthing numbers its events from 1 again
                with self._ev_lock:
                    self._since, self._conn_epoch, self._indexed, self._conn_seen = 0, {}, {}, {}
                    self._ev_gen = gen
            try:
                evs = sup.wait_ready(5).events(self._since, 5)     # default subscription: buffered from startup
            except (ApiError, SyncError):
                self._stopping.wait(0.5)
                continue
            settings: set[str] = set()
            finished: dict[str, set[str]] = {}
            with self._ev_lock:
                if self._ev_gen != (getattr(self.supervisor, "serial", None), getattr(self.supervisor, "restarts", None)):
                    continue                        # these events belong to a Syncthing that has since been replaced
                for e in evs:
                    self._since = max(self._since, e.get("id", 0))
                    d = e.get("data") or {}
                    if e.get("type") == "DeviceConnected":
                        self._conn_epoch[d.get("id", "")] = e["id"]
                        self._conn_seen[d.get("id", "")] = time.monotonic()
                    elif e.get("type") == "DeviceDisconnected":
                        self._conn_epoch.pop(d.get("id", ""), None)
                    elif e.get("type") == "RemoteIndexUpdated" and d.get("device") in self._conn_epoch:
                        self._indexed[(d["device"], d.get("folder", ""))] = self._conn_epoch[d["device"]]
                    if e.get("type") == "ItemFinished" and not d.get("error") and isinstance(d.get("item"), str):
                        finished.setdefault(d.get("folder", ""), set()).add(d["item"])
                    if e.get("type") == "ItemFinished" and not d.get("error") and (
                            d.get("item") in (".ave/project.json", "episode.json")
                            or (str(d.get("item", "")).startswith(".ave/log/") and str(d.get("item", "")).endswith(".jsonl"))):
                        settings.add(d.get("folder", ""))
            for fid in sorted(settings):            # project.json, episode.json (the layout) or a log arrived: follow it
                self._refresh_folder(fid)
            if finished:
                self._files_arrived(finished)
            self._sweep_conflicts()

    def _files_arrived(self, finished: dict[str, set[str]]) -> None:
        """Files Syncthing just finished: readiness looks at them again at once (never waiting for its stat cache)."""
        try:
            from ..project import readiness
            paths = {f["id"]: f.get("path") for f in self._folders()}
            for fid, items in finished.items():
                if paths.get(fid):
                    readiness.invalidate(paths[fid], sorted(items))
        except Exception:  # noqa: BLE001 - readiness never stops the event follower
            pass
        try:                                        # conflict copies, settings and logs that arrived: the watcher's worker
            from . import conflicts
            for fid, items in finished.items():
                want = [i for i in items if conflicts.CONFLICT_RE.match(i.rsplit("/", 1)[-1])
                        or i in (".ave/project.json", "episode.json", "studio.json") or i.startswith((".ave/log/", "posts/",
                        "campaigns/", "recipes/", "schedule/"))]
                if want and paths.get(fid):
                    conflicts.queue(paths[fid], items=want, manager=self)   # never runs on this thread
        except Exception:  # noqa: BLE001 - the conflict watcher never stops the event follower
            pass

    def _sweep_conflicts(self) -> None:
        """While sync runs, every shared project gets the conflict watcher's settings sweep every SWEEP_S (on its worker),
        whether or not any window is open."""
        from . import conflicts
        now = time.monotonic()
        if now - getattr(self, "_swept", -conflicts.SWEEP_S) < conflicts.SWEEP_S:
            return
        self._swept = now
        try:
            for f in self._folders():
                if f.get("path") and (Path(f["path"]) / ".ave" / "project.json").is_file():
                    conflicts.queue(f["path"], manager=self)
        except Exception:  # noqa: BLE001 - the sweep never stops the event follower
            pass

    def modified_short(self, project, rel: str) -> str | None:
        """The first block of the Syncthing id of the computer whose change `rel` holds (Syncthing's `modifiedBy`)."""
        root = _root_of(project)
        with self.quick(2.0):
            info = self.api.get("/rest/db/file", folder=self._fid(root), file=rel) or {}
        short = str(((info.get("global") or {}).get("modifiedBy")) or "")
        return short.upper() or None

    def device_name(self, sync_id: str) -> str | None:
        """The name Syncthing knows a computer by, when it is a name a person would recognise; else None."""
        name = self._name_of(sync_id)
        return None if name == SOMEONE else name

    def modified_by(self, project, rel: str) -> str | None:
        """The person whose computer last changed `rel` in this project, as Syncthing records it, else None."""
        root = _root_of(project)
        short = self.modified_short(root, rel)
        if not short:
            return None
        for sid, person in self._people(root).items():
            if sid.split("-")[0].upper() == short:
                return person
        return None

    def _refresh_folder(self, fid: str) -> None:
        try:
            folder = next((f for f in self._folders() if f["id"] == fid), None)
            if folder:
                self.refresh(folder["path"])
        except Exception as exc:  # noqa: BLE001 - a bad project.json must never stop the event follower
            self.refresh_errors[fid] = f"{type(exc).__name__}: {exc}"
            if self.on_refresh_error:
                self.on_refresh_error(fid, exc)
        else:
            self.refresh_errors.pop(fid, None)

    def _conn_age(self, dev: str, conns: dict) -> float:
        """Seconds this member's connection has been up: Syncthing's startedAt, else when the follower saw it connect."""
        started = _parse_time((conns.get(dev) or {}).get("startedAt"))
        if started is not None:
            return max(0.0, time.time() - started)
        seen = self._conn_seen.get(dev)
        return time.monotonic() - seen if seen is not None else 0.0

    def _mark_valid(self, folder: str, completions: dict[str, dict], conns: dict) -> None:
        """The idle-folder case: Syncthing sends no index update on reconnect when nothing changed. A member whose
        connection has been up for SETTLE_S, with remoteState valid and 100 %, counts as known. Before that, a valid 100 %
        may come from our stored copy of its index while its delta is still on the way, so only RemoteIndexUpdated counts."""
        with self._ev_lock:
            if not self._tracking_current():
                return
            for dev, c in completions.items():
                if c.get("remoteState") != "valid" or float(c.get("completion", 0)) < 100 \
                        or any(int(c.get(k) or 0) for k in ("needBytes", "needItems", "needDeletes")):
                    continue
                if self._conn_age(dev, conns) < self.SETTLE_S:
                    continue
                if dev not in self._conn_epoch:     # connected before the event follower saw it (e.g. events aged out)
                    self._conn_epoch[dev] = ("conn", (conns.get(dev) or {}).get("startedAt", ""))
                self._indexed[(dev, folder)] = self._conn_epoch[dev]

    def _tracking_current(self) -> bool:
        sup = self.supervisor
        return bool(sup) and self._ev_gen == (sup.serial, sup.restarts)

    def indexed_since_connect(self, folder: str) -> set[str]:
        """Members whose index is known on their current connection. Empty until the event follower has caught up with
        the Syncthing now running, so state from a previous run or crash can never count."""
        with self._ev_lock:
            if not self._tracking_current():
                return set()
            return {dev for (dev, f), epoch in self._indexed.items() if f == folder and self._conn_epoch.get(dev) == epoch}

    def events(self, timeout_s: int = 30, types: list[str] | None = None, since: int = 0) -> list[dict]:
        return self.api.events(since, timeout_s, types)

    # ------------------------------------------------------------------ sharing
    def _folders(self) -> list[dict]:
        return self.api.get("/rest/config/folders") or []

    def folders(self) -> list[dict]:
        """Every project folder sync knows on this computer: its config ("id", "label", "path", "devices", ...)."""
        return [dict(f) for f in self._folders()]

    def _folder_by_path(self, root: Path, folders: list[dict] | None = None) -> dict | None:
        return next((f for f in (self._folders() if folders is None else folders) if C.real(f["path"]) == root), None)

    def share(self, project, peers: Iterable[Peer] | None = None, *, authorized: bool = False) -> dict:
        """Make (or update) the project's folder, shared with exactly the listed members' devices among `peers`. With no
        `peers`, the devices members.share_list() works out from the owner's log. `authorized` peers (from that log) are
        shared with as given; otherwise only peers whose AVE device project.json lists. Writes the generated .stignore
        and .stfolder first. Returns the folder config that was applied."""
        with self._config_lock:
            return self._share(project, peers, authorized)

    def _share(self, project, peers, authorized: bool) -> dict:
        root = _root_of(project)
        meta = _read_meta(root)
        if peers is None:
            from .members import share_list
            peers, authorized = share_list(root, self), True
        fid = meta["project_id"]
        existing = next((f for f in self._folders() if f["id"] == fid), None)
        if existing and C.real(existing["path"]) != root:
            raise SyncError(f"This project is already synced from {existing['path']}. The folder at {root} has the same "
                            "project id, so it looks like a copy; open the original, or make the copy a new project.")
        me = self.device_id()
        peers = list(peers)
        for p in peers:
            C.listen_url(p.address, allow_loopback=self.allow_loopback)          # refuse anything outside Tailscale first
        C.prepare_folder(root, self.ave_device)
        for p in (peers if authorized else C.shared_peers(meta, peers)):
            if p.sync_id != me:
                self.api.put(f"/rest/config/devices/{q(p.sync_id)}", C.peer_entry(p, allow_loopback=self.allow_loopback))
                if p.name:
                    self._peer_names[p.sync_id] = p.name
        folder = C.folder_entry(root, meta, me, peers, members_only=not authorized, min_disk_free=self.min_disk_free)
        self.api.put(f"/rest/config/folders/{q(fid)}", folder)
        self._prune_devices(me)
        try:
            self.api.post("/rest/db/scan", folder=fid)
        except ApiError:
            pass
        return folder

    def _prune_devices(self, me: str) -> None:
        used = {d["deviceID"] for f in self._folders() for d in f.get("devices", [])} | self.pairing
        for d in self.api.get("/rest/config/devices"):
            if d["deviceID"] != me and d["deviceID"] not in used:
                self.api.delete(f"/rest/config/devices/{q(d['deviceID'])}")

    def refresh(self, project) -> None:
        """project.json or a log changed (a member was added or removed, a media choice changed, it arrived from another
        member): regenerate .stignore and share with exactly the devices the owner's log says (members.share_list).
        Nothing is shared with a device only because project.json lists it."""
        from .members import MemberError, is_removed
        root = _root_of(project)
        _read_meta(root)
        try:
            removed = is_removed(root, self)
        except MemberError:
            removed = None                          # no owner known: only the ignore rules follow
        if removed:
            try:
                self.unshare(root)                  # the owner removed this computer: stop; the files already here stay
            except ApiError:
                pass
        elif removed is False:
            self.share(root)
        else:
            C.prepare_folder(root, self.ave_device)
            try:
                self.rescan(root)
            except (ApiError, SyncError):
                pass

    # ------------------------------------------------------------------ pairing (used by members.py)
    def listen_address(self) -> str:
        """The one address this Syncthing listens on, as peers should dial it."""
        addrs = self.api.get("/rest/config/options").get("listenAddresses") or []
        if len(addrs) != 1:
            raise SyncError(f"sync listens on {addrs!r}, not one address")
        return C.listen_url(addrs[0], allow_loopback=self.allow_loopback)

    def set_name(self, name: str | None = None) -> None:
        """The name this computer gives itself to peers (the manager's own name when None)."""
        self.api.patch(f"/rest/config/devices/{q(self.device_id())}", {"name": self.name if name is None else name})

    def add_device(self, peer: Peer) -> None:
        """Know a peer's device without sharing any folder with it (so it can be dialled during pairing)."""
        self.api.put(f"/rest/config/devices/{q(peer.sync_id)}", C.peer_entry(peer, allow_loopback=self.allow_loopback))
        if peer.name:
            self._peer_names[peer.sync_id] = peer.name

    def forget_device(self, sync_id: str) -> None:
        """Forget a device no folder is shared with (a pairing that was cancelled or expired)."""
        if any(d["deviceID"] == sync_id for f in self._folders() for d in f.get("devices", [])):
            return
        try:
            self.api.delete(f"/rest/config/devices/{q(sync_id)}")
        except ApiError:
            pass

    def pending_devices(self) -> dict[str, dict]:
        """Devices that tried to connect and are not known here: {sync id: {"name", "address", "time"}}."""
        return self.api.get("/rest/cluster/pending/devices") or {}

    def dismiss_pending(self, sync_id: str) -> None:
        try:
            self.api.request("DELETE", "/rest/cluster/pending/devices", {"device": sync_id})
        except ApiError:
            pass

    def _fid(self, project) -> str:
        """The folder id: Syncthing's folder at this path, else project.json's id."""
        root = _root_of(project)
        f = self._folder_by_path(root)
        if f:
            return f["id"]
        try:
            return _read_meta(root)["project_id"]
        except (OSError, ValueError) as exc:
            raise SyncError(f"{root} is not a shared project ({exc})") from None

    def unshare(self, project) -> None:
        with self._config_lock:
            self.api.delete(f"/rest/config/folders/{q(self._fid(project))}")
            self._prune_devices(self.device_id())

    def rescan(self, project) -> None:
        self.api.post("/rest/db/scan", folder=self._fid(project))

    # ------------------------------------------------------------------ status
    def status(self, project, *, folders: list[dict] | None = None) -> SyncStatus:
        """`folders` is a /rest/config/folders the caller already has (a look across every project fetches it once)."""
        root = _root_of(project)
        down = self.down()
        if down is not None:
            return self._without_sync(root, down)
        try:
            folder = self._folder_by_path(root, folders)
        except (ApiError, SyncError) as exc:
            return SyncStatus("error", error=str(exc))
        if folder is None:
            if not root.is_dir():
                return SyncStatus("error", error=f"{root} is not there and is not a shared project")
            try:
                fid = _read_meta(root)["project_id"]
            except (OSError, ValueError) as exc:
                return SyncStatus("error", error=f"{root} has no readable .ave/project.json ({exc})")
            other = next((f for f in (self._folders() if folders is None else folders) if f["id"] == fid), None)
            if other:
                return SyncStatus("error", error=f"this project is synced from {other['path']}; {root} looks like a copy")
            return SyncStatus("not_shared")
        fid = folder["id"]
        if not root.is_dir() or not (root / C.MARKER).is_dir():
            return SyncStatus("drive_missing", error=f"{root} is not there")
        try:
            st = self.api.get("/rest/db/status", folder=fid)
        except ApiError as exc:
            return SyncStatus("error", error=str(exc))
        err = str(st.get("error") or st.get("invalid") or "")
        if st.get("state") == "error" or err:
            if any(m in err.lower() for m in _MISSING):
                return SyncStatus("drive_missing", error=err)
            if any(m in err.lower() for m in _NO_SPACE):
                return self._low_disk(root, folder, err)
            return SyncStatus("error", error=err)
        if int(st.get("pullErrors") or st.get("errors") or 0):
            space = self._space_error(fid)
            if space:
                return self._low_disk(root, folder, space)
        me = self.device_id()
        peers = [d["deviceID"] for d in folder.get("devices", []) if d["deviceID"] != me]
        conns = (self.api.get("/rest/system/connections") or {}).get("connections") or {}
        connected = {p for p in peers if (conns.get(p) or {}).get("connected")}
        completions = {p: self.api.get("/rest/db/completion", folder=fid, device=p) for p in connected}
        self._mark_valid(fid, completions, conns)
        people = self._people(root)
        return decide(st, peers, connected, completions, self.indexed_since_connect(fid),
                      lambda sid: people.get(sid) or self._name_of(sid))

    def down(self, why: str = "") -> SyncStatus | None:
        """What a shared project says while sync itself is not there, or None while it runs. `why` is a failure the
        caller just had talking to it (a status look that timed out)."""
        st = self.state
        if st == "waiting_for_tailscale":
            return SyncStatus("tailscale_off", error=self.waiting_reason)
        if st == "error":
            return SyncStatus("sync_off", error=self.start_error or self.waiting_reason or "start")
        if st == "stopped":
            return SyncStatus("sync_off", error="stopped")
        if why:
            return SyncStatus("sync_off", error=why)
        if not (self.supervisor and self.supervisor.running()):
            if getattr(self._quick, "timeout", None) is None:
                return None                     # an ordinary caller (wait()) waits for Syncthing to be ready
            return SyncStatus("sync_off", error="starting" if st == "starting" else "restarting")
        return None

    def _people(self, root: Path) -> dict[str, str]:
        """Syncthing device id -> the person, from project.json's members and devices: names never come from ids."""
        try:
            meta = _read_meta(root)
        except (OSError, ValueError):
            return {}
        person = C.member_devices(meta)
        devices = meta.get("devices") if isinstance(meta.get("devices"), dict) else {}
        return {v["sync_id"]: person[d] for d, v in devices.items()
                if isinstance(v, dict) and isinstance(v.get("sync_id"), str) and person.get(d)}

    def without_sync(self, project, s: SyncStatus) -> SyncStatus:
        return self._without_sync(_root_of(project), s)

    def _without_sync(self, root: Path, s: SyncStatus) -> SyncStatus:
        """Sync is not running (Tailscale is off): a project nobody else is on is simply not shared; a missing folder is
        still a drive that is not connected."""
        try:
            meta = _read_meta(root)
        except (OSError, ValueError):
            return SyncStatus("drive_missing", error=f"{root} is not there") if not root.is_dir() else \
                SyncStatus("error", error=f"{root} has no readable .ave/project.json")
        try:
            mine = self.ave_device
        except Exception:  # noqa: BLE001 - no device file yet: every listed device counts as someone else's
            mine = ""
        others = [d for d in C.member_devices(meta) if d != mine]
        return s if others else SyncStatus("not_shared")

    def _space_error(self, fid: str) -> str:
        """The first "insufficient space" pull error for this folder, or ""."""
        try:
            errs = (self.api.get("/rest/folder/errors", folder=fid) or {}).get("errors") or []
        except ApiError:
            return ""
        return next((str(e.get("error")) for e in errs if any(m in str(e.get("error", "")).lower() for m in _NO_SPACE)), "")

    def _low_disk(self, root: Path, folder: dict, err: str) -> SyncStatus:
        home_disk = "database" in err.lower()          # the disk Syncthing keeps its index on, not the project's drive
        return SyncStatus("low_disk", error=err, free_bytes=free_space(self.home if home_disk else root),
                          min_free_bytes=None if home_disk else disk_bytes(folder.get("minDiskFree")))

    def _name_of(self, sync_id: str) -> str:
        """The name Syncthing knows the device by, if it is a person's name; else "another computer". Never an id: not a
        Syncthing id, an AVE device id ("dev_..."), or a pairing name ("ave-join:...")."""
        if sync_id not in self._peer_names:
            try:
                name = str(self.api.get(f"/rest/config/devices/{q(sync_id)}").get("name") or "")
            except (ApiError, SyncError):
                return SOMEONE
            self._peer_names[sync_id] = name
        name = self._peer_names[sync_id]
        if not name or name.startswith(("dev_", "ave-join:")) or C.DEVICE_ID_RE.match(name) or name[:7] == sync_id[:7]:
            return SOMEONE
        return name

    def wait(self, project, states: str | tuple[str, ...], timeout: float = 60.0) -> SyncStatus:
        """Block until the project's status is one of `states`, woken by Syncthing's event stream."""
        want = (states,) if isinstance(states, str) else tuple(states)
        deadline = time.monotonic() + timeout
        s = self.status(project)
        since = 0
        while s.state not in want:
            left = deadline - time.monotonic()
            if left <= 0:
                raise SyncError(f"status stayed {s.state} ({s}), wanted {want}")
            try:
                evs = self.api.events(since, max(1, min(2, int(left))))
                since = max([since] + [e.get("id", 0) for e in evs])
            except ApiError:
                time.sleep(0.2)
            s = self.status(project)
        return s


__all__ = ["FOLDER_DEFAULTS", "SYNC_OPTIONS", "STATES", "Api", "ApiError", "ConfigError", "MemberStatus", "Peer", "SyncError",
           "SyncManager", "SyncStatus", "Supervisor", "decide", "find_binary"]
