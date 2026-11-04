"""Automatic updates for AVE Studio: the appcast, version order, when a restart is safe, and the private feed address.

The installed app updates itself with Sparkle 2 (see `desktop_updates`, which drives Sparkle's own window: download, EdDSA
verification, install and relaunch). This module holds the parts that are plain Python and testable without AppKit:

    check(feed_url) -> Update | None     the newest item in the appcast this computer can run, if newer than this app
    safe_to_restart(app) -> bool         False while a render, export, Jobs panel job, sync transfer or import runs
    RelaunchGate                         holds Sparkle's "install now" block until safe_to_restart turns true
    status()                             enabled or not, and why (a development build has no update key)

The feed address is private: the bucket is not public and the address carries an unguessable part. It is baked into a
release build's Info.plist (`SUFeedURL`) from a build input and otherwise lives only in local config
(`~/.config/ave/updates.json` or `AVE_UPDATE_FEED_URL`), never in the repo. Nothing here logs it, puts it in an error, in
`ave doctor`, or in the diagnostics bundle: `redact()` replaces it with "[update feed]" wherever text could carry it.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from xml.etree import ElementTree as ET

from . import __version__

log = logging.getLogger(__name__)

SPARKLE_NS = "http://www.andymatuschak.org/xml-namespaces/sparkle"
REDACTED = "[update feed]"
CONFIG = Path.home() / ".config" / "ave" / "updates.json"
FEED_ENV = "AVE_UPDATE_FEED_URL"
STATE = Path.home() / "Library" / "Application Support" / "AVE" / "updates.json"
MAX_FEED_BYTES = 1024 * 1024
DEV_BUILD = ("This is a development build. It was built without an update key, so it cannot check for or install updates. "
             "Install a release build of AVE Studio to get updates.")
SOURCE = "Updates come with the installed AVE Studio app. This is a source checkout: update it with git."


class UpdateError(Exception):
    """A check that could not finish. Its message never carries the feed address."""


# ------------------------------------------------------------------------------------------------------------ versions
_NUMBER, _STRING, _PERIOD = 0, 1, 2
_PART = re.compile(r"\d+|\.|[^\d.]+")


def _kind(part: str) -> int:
    return _NUMBER if part.isdigit() else _PERIOD if part == "." else _STRING


def compare_versions(a: str, b: str) -> int:
    """-1, 0 or 1, ordered exactly as Sparkle's SUStandardVersionComparator orders them, so this app and Sparkle agree:
    numbers numerically ("1.10" > "1.9"), a longer version newer when it goes on with numbers ("1.0.1" > "1.0") and older
    when it goes on with letters ("1.0b1" < "1.0")."""
    pa, pb = _PART.findall(str(a).strip()), _PART.findall(str(b).strip())
    for x, y in zip(pa, pb):
        kx, ky = _kind(x), _kind(y)
        if kx == ky:
            if kx == _NUMBER and int(x) != int(y):
                return -1 if int(x) < int(y) else 1
            if kx == _STRING and x != y:
                return -1 if x < y else 1
            continue
        if ky == _STRING:
            return 1                       # a number or a period beats letters
        if kx == _STRING:
            return -1
        return 1 if kx == _NUMBER else -1  # a number beats a stray period
    n = min(len(pa), len(pb))
    if len(pa) == len(pb):
        return 0
    longer, longer_is_a = (pa, True) if len(pa) > len(pb) else (pb, False)
    newer = 1 if longer_is_a else -1
    rest = longer[n]
    if _kind(rest) == _STRING or (_kind(rest) == _PERIOD and n + 1 < len(longer) and _kind(longer[n + 1]) == _STRING):
        return -newer                      # 1.0b1 and 1.0.b1 are older than 1.0
    return newer


# ------------------------------------------------------------------------------------------------------------- appcast
@dataclass(frozen=True)
class Update:
    version: str                           # sparkle:version, compared with CFBundleVersion
    short_version: str                     # sparkle:shortVersionString, shown to people
    length: int
    ed_signature: str
    minimum_system_version: str | None = None
    title: str = ""
    url: str = field(default="", repr=False)     # the download: under the private feed, so never shown or logged

    def describe(self) -> str:
        return f"AVE Studio {self.short_version or self.version}"


def _text(el, name):
    child = el.find(name)
    return (child.text or "").strip() if child is not None and child.text else None


def parse_appcast(data: bytes) -> list[Update]:
    """Every item of a Sparkle appcast that has a signed enclosure. Items without an EdDSA signature are left out: Sparkle
    refuses them, so offering one would only end in a failed install."""
    if len(data) > MAX_FEED_BYTES:
        raise UpdateError("The update feed is too large.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise UpdateError(f"The update feed is not readable ({exc}).") from None
    s = "{%s}" % SPARKLE_NS
    out = []
    for item in root.iter("item"):
        enc = item.find("enclosure")
        if enc is None:
            continue
        version = _text(item, s + "version") or enc.get(s + "version")
        sig = enc.get(s + "edSignature")
        if not version or not sig:
            continue
        try:
            length = int(enc.get("length") or 0)
        except ValueError:
            length = 0
        out.append(Update(version=version, short_version=_text(item, s + "shortVersionString") or enc.get(s + "shortVersionString") or version,
                          length=length, ed_signature=sig, minimum_system_version=_text(item, s + "minimumSystemVersion"),
                          title=_text(item, "title") or "", url=enc.get("url") or ""))
    return out


def system_version() -> str:
    return platform.mac_ver()[0] or "0"


def _fetch(url: str) -> bytes:
    from urllib.request import Request, urlopen
    req = Request(url, headers={"User-Agent": f"AVE Studio/{__version__}"})
    with urlopen(req, timeout=20) as r:      # noqa: S310 - https only, checked by the caller
        return r.read(MAX_FEED_BYTES + 1)


def check(feed_url: str, *, current: str | None = None, system: str | None = None,
          fetch: Callable[[str], bytes] | None = None) -> Update | None:
    """The newest update this Mac can run that is newer than `current` (this app's CFBundleVersion), else None. Items that
    need a newer macOS (`minimumSystemVersion`) are skipped. Errors say what went wrong without the feed address."""
    if not feed_url or not str(feed_url).startswith("https://"):
        raise UpdateError("No update feed is configured for this build.")
    current = current or status()["build"]
    system = system or system_version()
    try:
        data = (fetch or _fetch)(feed_url)
    except Exception as exc:  # noqa: BLE001 - any transport error, reworded without the address
        message = f"Could not reach the update feed ({redact(type(exc).__name__ + ': ' + str(exc), feed_url)})."
        log.warning(message)
        raise UpdateError(message) from None
    best = None
    for item in parse_appcast(data):
        if item.minimum_system_version and compare_versions(system, item.minimum_system_version) < 0:
            continue
        if compare_versions(item.version, current) <= 0:
            continue
        if best is None or compare_versions(item.version, best.version) > 0:
            best = item
    log.info("update check: %s", best.describe() if best else "up to date")
    return best


# ------------------------------------------------------------------------------------------------- the private address
def _secret_forms(feed_url: str) -> set[str]:
    """Every way the private part of a feed address can appear: the address, its folder, the bucket object keys (the path
    without the host: "<prefix>/appcast.xml", "<prefix>"), each long path segment, and their percent-encoded spellings."""
    from urllib.parse import quote, unquote, urlsplit
    forms = {feed_url}
    try:
        p = urlsplit(feed_url)
    except ValueError:
        return forms
    path = unquote(p.path).strip("/")
    folder = path.rsplit("/", 1)[0] if "/" in path else ""
    if p.scheme and p.netloc:
        forms.add(f"{p.scheme}://{p.netloc}/{folder}" if folder else feed_url)
    forms |= {x for x in (path, folder) if x}
    forms |= {seg for seg in folder.split("/") if len(seg) >= 8}
    for x in list(forms):
        forms |= {quote(x, safe="/:"), quote(x, safe=""), "".join(c if c.isalnum() else "%%%02X" % ord(c) for c in x),
                  "".join(c if c.isalnum() else "%%%02x" % ord(c) for c in x)}
    return {x for x in forms if len(x) >= 8}


def redact(text: str, *secrets: str | None) -> str:
    """`text` with the private feed address replaced by "[update feed]": the address, its folder, the R2 object keys under
    it (as R2 error bodies show them: <Key>, <Resource>) and percent-encoded spellings. With no secrets given, the configured
    ones."""
    text = str(text)
    secrets = tuple(s for s in (secrets or (configured_feed_url(), bundle_info().get("SUFeedURL"))) if s)
    for s in secrets:
        for value in sorted(_secret_forms(s), key=len, reverse=True):
            text = text.replace(value, REDACTED)
    return text


def local_config() -> dict:
    """Publishing and build settings kept on this computer only: {feed_url, r2_endpoint, r2_bucket}. Env overrides the file."""
    cfg: dict = {}
    try:
        cfg = json.loads(CONFIG.read_text())
        if not isinstance(cfg, dict):
            cfg = {}
    except (OSError, ValueError):
        cfg = {}
    for key, env in (("feed_url", FEED_ENV), ("r2_endpoint", "AVE_R2_ENDPOINT"), ("r2_bucket", "AVE_R2_BUCKET")):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    return cfg


def configured_feed_url() -> str | None:
    return local_config().get("feed_url") or None


def info_plist_path() -> Path | None:
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent.parent / "Info.plist"


def bundle_info(path: Path | None = None) -> dict:
    import plistlib
    path = path or info_plist_path()
    if path is None:
        return {}
    try:
        with open(path, "rb") as f:
            return plistlib.load(f)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return {}


def status(info: dict | None = None) -> dict:
    """{enabled, reason, version, build, feed}. `feed` is "configured" or "none", never the address itself."""
    frozen = info is not None or bool(getattr(sys, "frozen", False))
    info = bundle_info() if info is None else info
    version = info.get("CFBundleShortVersionString") or __version__
    build = info.get("CFBundleVersion") or version
    feed = "configured" if info.get("SUFeedURL") else "none"
    if not frozen:
        return {"enabled": False, "reason": SOURCE, "version": version, "build": build, "feed": feed}
    if not info.get("SUPublicEDKey") or not info.get("SUFeedURL"):
        return {"enabled": False, "reason": DEV_BUILD, "version": version, "build": build, "feed": feed}
    return {"enabled": True, "reason": None, "version": version, "build": build, "feed": feed}


def diagnostics_section(info: dict | None = None) -> dict:
    """What the diagnostics bundle says about updates: never the address."""
    s = status(info)
    return {k: s[k] for k in ("enabled", "reason", "version", "build", "feed")}


# ------------------------------------------------------------------------------------------------- a safe moment to restart
RESTARTING = "AVE is restarting to update. Start this again when it reopens."
_DRAIN = threading.Event()


class Restarting(ValueError):
    """New work refused: the relaunch for an update was approved (see RelaunchGate.release)."""


def draining() -> bool:
    return _DRAIN.is_set()


def refuse_if_draining() -> None:
    """Every job starter calls this first: once the restart is approved, nothing new starts until the relaunch."""
    if _DRAIN.is_set():
        raise Restarting(RESTARTING)


def end_drain() -> None:
    """The update was cancelled or failed: work may start again."""
    _DRAIN.clear()



_RENDER_OPS = ("render", "video", "design", "image", "episode-final", "episode-short", "episode-select", "episode-package",
               "episode-rough", "episode-assembly", "episode-combined", "episode-sequence", "episode-handoff")


def busy_reasons(app, *, sync: bool = True) -> list[str]:
    """What would be cut short by a restart right now, in plain words. `app` is the desktop controller: its own import
    and file-operation flags, and its session's Media tools jobs, Jobs panel runners and project sync."""
    out: list[str] = []
    if getattr(app, "busy", False):
        out.append("an import is running")
    work = getattr(app, "file_work", None)
    if work is not None and getattr(work, "busy", False):
        out.append("a file operation (an export or a package) is running")
    session = getattr(app, "session", None)
    if session is None:
        return out
    server = getattr(session, "server", None)
    studio_app = getattr(server, "app", None)
    bench = getattr(studio_app, "workbench", None)
    if bench is not None and getattr(bench, "sp", None) is not None and not getattr(bench, "fixtures", False):
        try:
            jobs = bench._jobs()
        except Exception:  # noqa: BLE001 - an unreadable job folder is not a reason to hold an update forever
            jobs = []
        for job in jobs:
            if job.get("status") not in ("queued", "running"):
                continue
            op = str(job.get("operation") or "")
            if op in ("import", "episode-intake", "episode-adopt", "video-source"):
                out.append("an import is running")
            elif any(op.startswith(r) for r in _RENDER_OPS):
                out.append("a render or export is running")
            else:
                out.append(f"a production job ({op or 'job'}) is running")
    room = getattr(server, "busy", None)                 # the Room's renders and review copies (StudioServer._background)
    if callable(room):
        try:
            out.extend(room())
        except Exception:  # noqa: BLE001
            pass
    episodes = getattr(server, "episodes", None)
    for runner in list(getattr(episodes, "runners", {}).values()):
        try:
            job = runner._running()
        except Exception:  # noqa: BLE001
            job = None
        if job:
            out.append(f"a job from the Jobs panel (ave {job.get('cmd')}) is running")
    sync = getattr(session, "_sync", None) if sync else None
    if sync is not None:
        workspace = getattr(server, "workspace", None)
        try:
            projects = workspace.projects() if workspace is not None else []
        except Exception:  # noqa: BLE001
            projects = []
        for p in projects:
            try:
                st = sync.status(p)
            except Exception:  # noqa: BLE001 - sync not running or not shared: nothing in flight
                continue
            # Only what this computer still has to RECEIVE: a restart mid-download leaves partial files here. A peer that
            # still needs something from here (or is lagging, or out of disk) resumes on its own and must not hold the update.
            if st.state == "syncing" and (getattr(st, "need_items", 0) or getattr(st, "need_bytes", 0)):
                out.append(f"{getattr(p, 'title', '') or 'a project'} is still syncing")
    return list(dict.fromkeys(out))


def safe_to_restart(app) -> bool:
    return not busy_reasons(app)


class RelaunchGate:
    """Sparkle asks before it relaunches (`updater:shouldPostponeRelaunchForUpdate:untilInvokingBlock:`). While a restart is
    not safe the gate keeps the block; `poll()` (or `release()` after a check made elsewhere) runs it once it is safe, and
    `force()` runs it now ("Install now"). Each block runs once. `last` is what it was waiting for."""

    def __init__(self, app, reasons: Callable[[object], list[str]] = busy_reasons,
                 recheck: Callable[[object], list[str]] | None = None):
        self.app, self.reasons, self.recheck = app, reasons, recheck or reasons
        self._pending: Callable[[], None] | None = None
        self._lock = threading.Lock()
        self.last: list[str] = []

    @property
    def waiting(self) -> bool:
        return self._pending is not None

    def postpone(self, resume: Callable[[], None]) -> bool:
        """True (postponed, keep `resume` for later) or False (go ahead now)."""
        why = self.reasons(self.app)
        if not why:
            return False
        with self._lock:
            self._pending, self.last = resume, why
        log.info("update install waits: %s", "; ".join(why))
        return True

    def release(self, *, check: bool = True) -> bool:
        """Approve the relaunch: raise the drain flag (no new job, render, review copy or import may start), look once more
        with the flag up (a job that started a moment ago keeps it waiting, and the flag drops again), then run the held
        block. `check=False` ("Install now") skips the look. True when the block ran."""
        if self._pending is None:
            return False
        _DRAIN.set()
        if check:
            why = self.recheck(self.app)
            if why:
                _DRAIN.clear()
                self.last = why
                return False
        with self._lock:
            resume, self._pending = self._pending, None
        if resume is None:
            _DRAIN.clear()
            return False
        try:
            resume()
        except BaseException:
            _DRAIN.clear()
            raise
        return True

    def force(self) -> bool:
        return self.release(check=False)

    def poll(self) -> bool:
        """Run the held block if a restart is now safe. True when it ran."""
        if self._pending is None:
            return False
        why = self.reasons(self.app)
        if why:
            self.last = why
            return False
        return self.release()


# ------------------------------------------------------------------------------------------- once per version, after an update
def first_launch_of(version: str, state: Path | None = None) -> bool:
    """True the first time this version runs on this computer (then `record_launch` marks it)."""
    state = state or STATE
    try:
        return json.loads(state.read_text()).get("refreshed_for") != version
    except (OSError, ValueError, AttributeError):
        return True


def record_launch(version: str, state: Path | None = None) -> None:
    state = state or STATE
    state.parent.mkdir(parents=True, exist_ok=True)
    tmp = state.with_name(f".{state.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"refreshed_for": version}) + "\n")
    os.replace(tmp, state)


__all__ = ["DEV_BUILD", "REDACTED", "RESTARTING", "RelaunchGate", "Restarting", "draining", "end_drain", "refuse_if_draining", "Update", "UpdateError", "busy_reasons", "check", "compare_versions",
           "configured_feed_url", "diagnostics_section", "first_launch_of", "local_config", "parse_appcast", "record_launch",
           "redact", "safe_to_restart", "status"]
