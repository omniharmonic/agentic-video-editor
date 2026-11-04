"""The conflict watcher: when two people changed the same synced file at once, keep both sides and ask a person.

Syncthing never merges. When two computers changed one file before seeing each other's change, it keeps one as the file
and renames the other `<name>.sync-conflict-<date>-<time>-<who>.<ext>` beside it (`<who>` is the first block of the
Syncthing id of the computer whose change that copy holds). Only the settings zone (project/zones.py) can produce one by
design: the log has one writer per file and everything permanent is written once.

`check(root)` is one pass over a project (it holds a lock); `queue(root)` asks the one background worker to run it, which
is how the app, the Syncthing event follower and the MCP tools call it, so a pass never runs on Syncthing's event thread
or inside a request.

  settings zone   **Nothing merges by itself.** Syncthing keeps no common ancestor, and no inference of one is safe, so
                  every settings conflict is one decision for a person (`decision.recorded`, kind "setting"): a per-key
                  comparison of the two files ("Maren's file: X / Tomas's file: Y", "(not set)" where a file has no such
                  key, with no guess whether it was added or removed). The person picks per key, or uses all of one
                  file, on the board or, in their own words, through Claude's decision_resolve. Nothing is written and
                  no copy is removed until then; the answer is applied with a compare-and-swap write, and the copy is
                  removed only if it is still the one that was compared. Before it is removed, the computer that applied
                  the answer keeps both sides as they were (`.ave/local/conflict-archive/<decision>.json`, local, never
                  synced). If the question is opened again (the director's Undo of an answer Claude relayed, or a
                  reopen), that computer puts the file back as it was and the other copy beside it, and the question
                  is asked again with both sides there to choose between.
                  project.json's identity and membership fields (MEMBERSHIP_KEYS) are never offered and always keep
                  the file's own values: who is a member comes only from the owner's `member.*` log events (members.py).
  any other zone  A bug: nothing is merged and nothing is removed. The pass records an alert in plain words
                  (`alerts(root)`), which the Workspace shows and Claude reads (artifact_list) until the copy is gone.

**Decision ids** are a hash of the copy's path and its content, so every computer that sees a copy records the same
decision (the reducer keeps one). A copy that itself syncs (studio posts/, campaigns/, recipes/, schedule/*.yaml) may be
recorded and answered on any computer, but only the project owner's computer (the pinned `members.owner_of`) applies the
answer; the others show "Waiting for Maren's computer to apply this" (`waiting(root)`). A copy that does not sync
(project.json, episode.json) is applied by the one computer that has it.

**One writer after the first question.** Only the computer that applies answers (the owner's for a copy that syncs,
this one for one that does not) closes a question as superseded or gone, and asks again (the copy changed, came back,
or the answer no longer fits). Two computers that hold different bytes for one copy therefore never supersede each
other's question; and a question closed as superseded is never called "came back".

**Names are fixed at record time.** `file_label`, `copy_label` and the options are worked out once, when the question is
recorded (`_sides`: the member Syncthing's modifiedBy or the copy's name points to, else "This computer's copy" or
"<device name>'s copy"), and every computer shows the record's words.

**Cost.** The worker is triggered by ItemFinished names (a conflict copy arriving, a settings file arriving, a log arriving)
and looks only at those and the copies it already knows; a slow sweep (SWEEP_S, about 10 minutes, run by the Workspace)
lists the settings-zone folders only. Media, work and the rest are never walked.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from ..project import zones

CONFLICT_RE = re.compile(r"^(?P<stem>.+?)\.sync-conflict-(?P<date>\d{8})-(?P<time>\d{6})-(?P<by>[A-Z0-9]{7})(?P<ext>\.[^./]*)?$")
MEMBERSHIP_KEYS = ("v", "project_id", "kind", "members", "devices", "owner_device", "publisher_device")
SWEEP_S = 600.0
DECISION_KIND = "setting"
PER_KEY = "Picked setting by setting"   # the choice recorded with `picks` when a person chose key by key
NOT_SET = "(not set)"
_MISSING = object()
_plock = threading.Lock()


@dataclass
class Report:
    asked: list[dict] = field(default_factory=list)      # {file, conflict, decision, summary}: waiting on a person
    settled: list[dict] = field(default_factory=list)    # {file, keys}: an answer written, conflict copy removed
    waiting: list[dict] = field(default_factory=list)    # {file, conflict, decision, owner, text}: answered, owner applies
    alerts: list[dict] = field(default_factory=list)     # {file, conflict, zone, text}: outside settings, left in place
    retry: list[str] = field(default_factory=list)       # copies whose file changed during the pass: tried again next pass
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: list(getattr(self, k)) for k in ("asked", "settled", "waiting", "alerts", "retry", "errors")}


class Changed(Exception):
    """The file changed between reading it and replacing it: nothing was written."""


# ------------------------------------------------------------------------------------------------------------- names
def original_of(rel: str) -> str | None:
    """The file a conflict copy belongs to ("episode.sync-conflict-20261102-101500-ABCDEFG.json" -> "episode.json")."""
    p = Path(rel)
    m = CONFLICT_RE.match(p.name)
    if not m:
        return None
    name = m.group("stem") + (m.group("ext") or "")
    return (p.parent / name).as_posix() if str(p.parent) != "." else name


def made_by(rel: str) -> str | None:
    m = CONFLICT_RE.match(Path(rel).name)
    return m.group("by") if m else None


def _own_project(d: Path) -> bool:
    """A folder inside this one that is a project of its own (an episode inside a studio): its own pass looks at it."""
    return (d / ".ave" / "project.json").is_file()


def _scandir(d: Path) -> list[os.DirEntry]:
    """Every folder listing the watcher makes goes through here (tests count them)."""
    try:
        with os.scandir(d) as it:
            return list(it)
    except OSError:
        return []


def _rel(root: Path, p: Path) -> str:
    r = p.relative_to(root).as_posix()
    return "" if r == "." else r


def _settings_scan(root: Path, rule_list) -> tuple[list[str], list[str]]:
    """(settings files, conflict copies of settings files), looking only in folders the settings rules name: a rooted
    settings folder (posts/) is read through; a rooted settings file (episode.json) or glob (schedule/*.yaml) costs one
    listing of its folder. Media, work and every other folder are never listed."""
    targets: dict[str, bool] = {}
    for r in rule_list:
        if r.zone != "settings":
            continue
        prefix = zones._literal_prefix(r.glob)
        if prefix is None:
            continue
        literal = not re.search(r"(?<!\\)[*?]", r.glob)
        if literal and (root / prefix).is_dir():
            targets[prefix] = True
        else:
            parent = str(Path(prefix).parent) if literal else prefix
            parent = "" if parent == "." else parent
            targets.setdefault(parent, False)
    files, copies = set(), set()
    seen: set[str] = set()
    stack = [(d, rec) for d, rec in sorted(targets.items())]
    while stack:
        d, rec = stack.pop()
        if (d, rec) in seen:
            continue
        seen.add((d, rec))
        for e in _scandir(root / d if d else root):
            r = f"{d}/{e.name}" if d else e.name
            if e.is_dir(follow_symlinks=False):
                if rec and not r.startswith(".ave/local") and not _own_project(Path(e.path)):
                    stack.append((r, True))
                continue
            orig = original_of(r)
            if orig is not None:
                if zones.zone_of(orig, rule_list) == "settings":
                    copies.add(r)
            elif not (e.name.startswith(".") and e.name.endswith(".tmp")) and zones.zone_of(r, rule_list) == "settings":
                files.add(r)
    return sorted(files), sorted(copies)


def _copies_of(root: Path, rel: str) -> list[str]:
    """The conflict copies beside one file (one listing of its folder)."""
    p = Path(rel)
    stem, ext = p.stem, p.suffix
    parent = "" if str(p.parent) == "." else p.parent.as_posix()
    out = []
    for e in _scandir(root / parent if parent else root):
        m = CONFLICT_RE.match(e.name)
        if m and m.group("stem") == stem and (m.group("ext") or "") == ext and e.is_file():
            out.append(f"{parent}/{e.name}" if parent else e.name)
    return sorted(out)


def find(root: Path) -> list[str]:
    """Every conflict copy in the project, project-relative and sorted: a full walk, for tests and `ave doctor` only. The
    watcher never walks the tree."""
    root = Path(root)
    out = []
    for d, dirs, fs in os.walk(root):
        rel_d = _rel(root, Path(d))
        dirs[:] = sorted(x for x in dirs if (f"{rel_d}/{x}" if rel_d else x) not in (".ave/local", ".stversions", ".stfolder")
                         and not _own_project(Path(d) / x))
        out += [f"{rel_d}/{f}" if rel_d else f for f in fs if CONFLICT_RE.match(f)]
    return sorted(out)


# ------------------------------------------------------------------------------------------------------------- people
def _meta(root: Path) -> dict:
    try:
        m = json.loads((root / ".ave" / "project.json").read_text())
        return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


def _people(meta: dict) -> list[str]:
    return [m["person"] for m in meta.get("members") or [] if isinstance(m, dict) and isinstance(m.get("person"), str)]


def _short(sync_id: str | None) -> str | None:
    return sync_id.split("-")[0].upper() if isinstance(sync_id, str) and sync_id else None


def _person_of_short(meta: dict, short: str | None) -> str | None:
    """The member whose Syncthing id starts with `short` (the block a conflict copy's name carries)."""
    if not short:
        return None
    devices = meta.get("devices") if isinstance(meta.get("devices"), dict) else {}
    for dev, v in devices.items():
        if isinstance(v, dict) and _short(v.get("sync_id")) == short.upper():
            for m in meta.get("members") or []:
                if isinstance(m, dict) and dev in (m.get("devices") or []):
                    return m.get("person")
    return None


def _this_device() -> str | None:
    try:
        from ..project.model import device_file
        return json.loads(device_file().read_text()).get("device_id")
    except (OSError, ValueError, AttributeError):
        return None


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def _person_of_device(meta: dict, device: str | None) -> str | None:
    for m in meta.get("members") or []:
        if isinstance(m, dict) and device and device in (m.get("devices") or []):
            return m.get("person")
    return None


def owner(root: Path) -> dict:
    """The project's owner device as this computer pinned it (members.owner_of), with the person's name; {} if unknown."""
    try:
        from .members import owner_of
        o = dict(owner_of(root))
    except Exception:  # noqa: BLE001 - no owner known: nobody here may apply a synced copy
        return {}
    o["person"] = _cap(o.get("person") or _person_of_device(_meta(root), o.get("device")) or "the owner")
    return o


def is_owner(root: Path) -> bool:
    """Whether this computer is the project's owner device: the one that applies answers for copies that sync."""
    o = owner(root)
    return bool(o.get("device")) and o.get("device") == _this_device()


@dataclass
class Side:
    """One side of a settings conflict as the record shows it, fixed once when the question is recorded: every computer
    renders these words and never works them out again."""
    person: str | None      # the member who wrote it, when known
    label: str              # "Maren's file", "This computer's copy", "Studio Mac's copy"
    option: str             # "Use all of Maren's", "Use all of this computer's copy"


def _named(person: str) -> Side:
    return Side(person, f"{person}'s file", f"Use all of {person}'s")


def _generic(label: str) -> Side:
    lower = label[:1].lower() + label[1:] if label.startswith(("This ", "The ")) else label
    return Side(None, label, f"Use all of {lower}")


def _manager_person(manager, root: Path, meta: dict, rel: str) -> str | None:
    """Who last wrote `rel` as Syncthing records it (its `modifiedBy`), mapped to a member; None if unknown."""
    fn = getattr(manager, "modified_short", None)
    if fn is None:
        return None
    try:
        return _person_of_short(meta, fn(root, rel))
    except Exception:  # noqa: BLE001 - sync not answering: the name is simply not known
        return None


def _device_name(manager, meta: dict, short: str | None) -> str | None:
    """The name Syncthing knows the computer that made a copy by (never an id), if a running manager knows it."""
    fn = getattr(manager, "device_name", None)
    if fn is None or not short:
        return None
    devices = meta.get("devices") if isinstance(meta.get("devices"), dict) else {}
    sid = next((v.get("sync_id") for v in devices.values() if isinstance(v, dict) and _short(v.get("sync_id")) == short.upper()), None)
    try:
        return fn(sid) if sid else None
    except Exception:  # noqa: BLE001
        return None


def _sides(root: Path, meta: dict, orig: str, rel: str, synced: bool, manager=None) -> tuple[Side, Side]:
    """Who wrote the file and who wrote the copy, worked out once, when the question is recorded.

    The copy: the member whose Syncthing id its name carries, else the name Syncthing knows that computer by, else "The
    other computer's copy". The file: the member Syncthing's `modifiedBy` names, else (in a two-person project) the other
    member, else "This computer's copy" for a copy that never leaves this computer, or the recording computer's person
    ("Maren's computer's copy") for one that syncs, else "The current file"."""
    b_person = _person_of_short(meta, made_by(rel))
    if b_person:
        b = _named(_cap(b_person))
    else:
        dn = _device_name(manager, meta, made_by(rel))
        b = _generic(f"{dn}'s copy" if dn else "The other computer's copy")
    a_person = _manager_person(manager, root, meta, orig)
    if not a_person:
        people = sorted(set(_people(meta)))
        if len(people) == 2 and b_person in people:
            a_person = next(x for x in people if x != b_person)
    if a_person:
        a = _named(_cap(a_person))
    elif not synced:
        a = _generic("This computer's copy")
    else:
        me = _person_of_device(meta, _this_device())
        a = _generic(f"{_cap(me)}'s computer's copy" if me else "The current file")
    if a.label.casefold() == b.label.casefold():        # one person, two computers
        a = Side(a.person, f"{a.person}'s file", f"Use all of {a.person}'s file") if a.person else _generic("This computer's copy")
        b = Side(b.person, f"{b.person}'s other copy", f"Use all of {b.person}'s other copy") if b.person else _generic("The other computer's copy")
    return a, b


# ------------------------------------------------------------------------------------------------------------- the record
def _dir(root: Path) -> Path:
    return Path(root) / ".ave" / "local" / "conflicts"


def _read_json(path: Path, default):
    try:
        v = json.loads(path.read_text())
        return v if isinstance(v, type(default)) else default
    except (OSError, ValueError):
        return default


def _write_json(path: Path, obj: Any, *, raw: bytes | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    tmp.write_bytes(raw if raw is not None else (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode())
    fd = os.open(tmp, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _sha(raw: bytes | None) -> str | None:
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


# ------------------------------------------------------------------------------------------------------------- words
LABELS = {"title": "Title", "tz": "Time zone", "layout": "Layout", "show": "Show", "season": "Season", "episode": "Episode",
          "draft": "Draft", "host": "Host", "guests": "Guests", "speakers": "Speakers", "text": "Text", "caption": "Caption",
          "first_comment": "First comment", "hashtags": "Hashtags", "link": "Link", "cta": "Call to action", "when": "When",
          "accounts": "Accounts", "platforms": "Platforms", "stage": "Stage", "status": "Status", "due": "Due date",
          "description": "Description", "name": "Name", "language": "Language", "brand": "Brand", "music": "Music"}
TEXT_CAP = 4000
WHOLE_JSON = (".ave/project.json", "episode.json", "studio.json")   # these must stay JSON objects: never written as text


def _preview(v: Any, n: int = 500) -> str:
    if v is _MISSING:
        return NOT_SET
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = " ".join(s.split())
    s = s if len(s) <= n else s[: n - 1].rstrip() + "…"
    return f"“{s}”" if isinstance(v, str) else s


def _label(k: str) -> str:
    """A person's name for a setting: a known one ("Time zone"), else the key with spaces and a capital."""
    k = str(k)
    return LABELS.get(k) or (k.replace("_", " ").replace("-", " ")[:1].upper() + k.replace("_", " ").replace("-", " ")[1:])


def _canon(v: Any) -> str | None:
    """JSON-canonical text: 1 and true, or 25 and 25.0, are different values."""
    return None if v is _MISSING else json.dumps(v, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def compare(a: Any, b: Any, *, protect: Iterable[str] = ()) -> list[dict]:
    """The settings the two files disagree on, top-level key by key: [{key, label, file, copy}] with the full values,
    and "(not set)" where one file has no such key. Membership keys are never offered."""
    if not (isinstance(a, dict) and isinstance(b, dict)):
        return []
    protect = set(protect)
    out = []
    for k in list(a) + [k for k in b if k not in a]:
        if k in protect:
            continue
        av, bv = a.get(k, _MISSING), b.get(k, _MISSING)
        if _canon(av) != _canon(bv):
            out.append({"key": k, "label": _label(k), "file": _preview(av), "copy": _preview(bv)})
    return out


def options(a: Side, b: Side) -> list[str]:
    return [a.option, b.option]


def must_be_object(orig: str) -> bool:
    """Settings files whose readers expect a JSON object (project.json, episode.json, studio.json, and every JSON file
    in posts/, campaigns/, recipes/ and schedule/proposals/): a copy that is not one can only be set aside."""
    return orig in WHOLE_JSON or orig.endswith(".json")


KEEP_FILE, USE_COPY, KEEP_REMOVED = "Keep this file", "Use the other copy", "Keep it removed"


def decision_id(rel: str, raw: bytes, after: str | None = None) -> str:
    """The same on every computer for the same copy: a hash of its path and its content. A question asked again (the
    file changed, the answer no longer fits, the copy came back) names the decision it replaces in `after`, so the new
    one is the same on every computer too."""
    key = rel.encode() + b"\0" + _sha(raw).encode() + (b"\0" + after.encode() if after else b"")
    return "dec_set_" + hashlib.sha256(key).hexdigest()[:24]


def valid_picks(d: dict, picks: Any) -> dict | None:
    """`picks` ({key: "file" | "copy"}) for exactly the keys a setting decision compares, else None."""
    keys = [x["key"] for x in (d.get("data") or {}).get("keys") or []]
    if not isinstance(picks, dict) or not keys or sorted(picks) != sorted(keys):
        return None
    if any(v not in ("file", "copy") for v in picks.values()):
        return None
    return {k: picks[k] for k in keys}


# ------------------------------------------------------------------------------------------------------------- writing
_before_replace: Callable[[Path], None] | None = None     # a seam for tests: runs between the last check and the replace


def _cas_write(path: Path, raw: bytes, expect: bytes | None) -> None:
    """Replace `path` with `raw` only if it still holds `expect` (None: it must not exist). Raises Changed otherwise.
    In a project a newer AVE saved, a shared file is never rewritten (ReadOnlyProject)."""
    from ..project.model import guard_write
    guard_write(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    tmp.write_bytes(raw)
    try:
        fd = os.open(tmp, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        if _before_replace is not None:
            _before_replace(path)
        try:
            now = path.read_bytes()
        except FileNotFoundError:
            now = None
        if now != expect:
            raise Changed(str(path))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _encode(value: Any, is_project_json: bool) -> bytes:
    if is_project_json:
        from .members import canonical
        return canonical(value)
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()


def _unlink_if(path: Path, expect: bytes) -> None:
    """Remove the conflict copy only if it is still the one that was read."""
    try:
        if path.read_bytes() != expect:
            raise Changed(str(path))
    except FileNotFoundError:
        return
    path.unlink(missing_ok=True)


# ------------------------------------------------------------------------------------------------------------- alerts
def _alerts_file(root: Path) -> Path:
    return _dir(root) / "alerts.json"


def alerts(root) -> list[dict]:
    """The recorded alerts (conflict copies outside settings) whose copy is still there: a few stat calls."""
    root = Path(root)
    return [a for a in _read_json(_alerts_file(root), []) if isinstance(a, dict) and (root / str(a.get("conflict", ""))).is_file()]


def _alert(root: Path, orig: str, rel: str, zone: str) -> dict:
    meta = _meta(root)
    who = _person_of_short(meta, made_by(rel))
    name = Path(orig).name
    text = (f"{name} was changed on two computers at once" + (f" (the other copy is {_cap(who)}'s)" if who else "") +
            ", which should not happen to this kind of file. Nothing was merged: both copies are kept. "
            "Tell Claude which one to keep, or keep it yourself and delete the other.")
    return {"file": orig, "conflict": rel, "zone": zone, "text": text}


# ------------------------------------------------------------------------------------------------------------- the pass
@contextmanager
def _locked(root: Path):
    locks = root / ".ave" / "local" / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    with _plock:
        fd = os.open(locks / "conflicts.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


def _mine_file(root: Path) -> Path:
    return _dir(root) / "decisions.json"


def check(root, *, manager=None, project=None, items: Iterable[str] | None = None) -> Report:
    """One pass (see the module docstring). `items` are the paths Syncthing just finished (None: the slow settings sweep;
    empty: only the copies already known). Never raises for what it finds on disk."""
    root = Path(root)
    rep = Report()
    if not (root / ".ave" / "project.json").is_file():
        return rep
    try:
        rule_list = zones.project_rules(root)
    except Exception as exc:  # noqa: BLE001 - an unreadable project.json: nothing can be classified this pass
        rep.errors.append(f"the project's sync rules could not be read ({exc})")
        return rep
    with _locked(root):
        mine = _read_json(_mine_file(root), {})
        copies: set[str] = {c for c in mine.values() if isinstance(c, str) and (root / c).is_file()}
        copies |= {a["conflict"] for a in alerts(root)}
        copies |= {w["conflict"] for w in waiting(root)}
        if items is None:
            copies |= set(_settings_scan(root, rule_list)[1])
        else:
            for it in items:
                it = str(it).strip("/")
                if original_of(it) is not None:
                    if (root / it).is_file():
                        copies.add(it)
                elif zones.zone_of(it, rule_list) == "settings":
                    copies |= set(_copies_of(root, it))
        proj, st = project, None
        logs = items is None or not list(items) or any(str(i).startswith(".ave/log/") for i in items)
        if logs:                                    # a sweep, an answer, or a log arriving: every setting decision in the
            try:                                    # reduced log whose copy is on this disk, whoever recorded it
                if proj is None:
                    from ..project.model import Project
                    proj = Project.open(root)
                st = proj.state()
                back = _restore_reopened(root, proj, st)  # an applied answer taken back: both sides return first
                if back:
                    st = proj.state()
                    copies |= back
                for d in st.decisions.values():
                    c = (d.get("data") or {}).get("conflict_file") if is_setting_decision(d) else None
                    if isinstance(c, str) and original_of(c) is not None and (root / c).is_file():
                        copies.add(c)
            except Exception as exc:  # noqa: BLE001
                rep.errors.append(f"the log could not be read for answers ({type(exc).__name__}: {exc})")
        new_alerts = []
        for rel in sorted(copies):
            orig = original_of(rel)
            zone = zones.zone_of(orig, rule_list)
            if zone != "settings":
                new_alerts.append(_alert(root, orig, rel, zone))
                continue
            try:
                if proj is None:
                    from ..project.model import Project
                    proj = Project.open(root)
                if st is None:
                    st = proj.state()
                if _one(root, proj, st, orig, rel, zones.zone_of(rel, rule_list) != "local", manager, rep, mine):
                    st = proj.state()                # it wrote: later copies read the log again
            except Changed:
                rep.retry.append(rel)
            except Exception as exc:  # noqa: BLE001 - one copy that cannot be handled never stops the others
                rep.errors.append(f"{rel}: {type(exc).__name__}: {exc}")
        if new_alerts != _read_json(_alerts_file(root), []):
            _write_json(_alerts_file(root), new_alerts)
        if rep.waiting != _read_json(_waiting_file(root), []):
            _write_json(_waiting_file(root), rep.waiting)
        rep.alerts = new_alerts
        try:
            _close_gone(root, proj, mine, rule_list)
        except Exception as exc:  # noqa: BLE001
            rep.errors.append(f"settled decisions could not be closed ({type(exc).__name__}: {exc})")
    if manager is not None and any(x["file"] == ".ave/project.json" for x in rep.settled):
        try:
            manager.refresh(root)                   # who shares what follows the owner's log; the ignore rules follow the file
        except Exception as exc:  # noqa: BLE001
            rep.errors.append(f"sync could not follow the settled project.json ({exc})")
    return rep


def _waiting_file(root: Path) -> Path:
    return _dir(root) / "waiting.json"


def waiting(root) -> list[dict]:
    """Answered copies that sync, waiting for the owner's computer to apply them, whose copy is still here."""
    root = Path(root)
    return [w for w in _read_json(_waiting_file(root), []) if isinstance(w, dict) and (root / str(w.get("conflict", ""))).is_file()]


def _read_side(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", "replace")


def _current(st, rel: str, b_sha: str) -> tuple[dict | None, list[dict], dict | None]:
    """(the live decision for this copy as it is now, the open ones about another content of the same copy, the newest
    decision about another content). The live one is the newest decision about these bytes that no later decision
    replaced."""
    from ..project import board
    from ..project import hlc as _hlc
    mine, older = [], []
    for d in st.decisions.values():
        data = d.get("data") or {}
        if not is_setting_decision(d) or data.get("conflict_file") != rel or "recorded" not in d:
            continue
        (mine if data.get("copy_sha") == b_sha else older).append(d)
    order = lambda d: (_hlc.parse(d["recorded"]["hlc"]), d["id"])   # noqa: E731
    replaced = {(d.get("data") or {}).get("supersedes") for d in mine}
    live = sorted((d for d in mine if d["id"] not in replaced), key=order)
    prev = max(older, key=order) if older else None
    return (live[-1] if live else None), sorted((d for d in older if board.decision_open(d)), key=order), prev


def _answered(d: dict) -> bool:
    from ..project import board
    res = d.get("resolution") or {}
    return not board.decision_open(d) and not res.get("gone") and res.get("choice") is not None


def _one(root: Path, project, st, orig: str, rel: str, synced: bool, manager, rep: Report, mine: dict) -> bool:
    """One settings copy: ask, wait, or apply an answer. True when it wrote to the log."""
    from ..project import board
    from ..project.adopt import home_artifact
    p_orig, p_conf = root / orig, root / rel
    b_raw = p_conf.read_bytes()
    try:
        a_raw = p_orig.read_bytes()
    except FileNotFoundError:
        a_raw = None
    applier = not synced or is_owner(root)
    name = Path(orig).name
    wrote = False
    if a_raw == b_raw:                              # two identical copies: nothing to decide
        if applier:
            _unlink_if(p_conf, b_raw)
        return False
    cur, older, prev = _current(st, rel, _sha(b_raw))
    # The copy changed while it was asked about: that question is over. Only the computer that applies answers says so
    # (one writer), so two computers that hold different bytes never supersede each other's question.
    if applier:
        for d in older:
            project.log.append("decision.resolved", artifact=d.get("artifact"), actor="scheduler",
                               data={"decision": d["id"], "choice": None, "gone": True, "superseded": True})
            wrote = True

    def ask(after: dict | None = None, why: str = "") -> bool:
        did = decision_id(rel, b_raw, after["id"] if after else None)
        if did in st.decisions and "data" in st.decisions[did]:
            mine[did] = rel
            _write_json(_mine_file(root), mine)
            rep.asked.append({"file": orig, "conflict": rel, "decision": did, "summary": st.decisions[did]["data"].get("summary")})
            return False
        data = _question(root, orig, rel, a_raw, b_raw, why, synced=synced, manager=manager)
        if data is None:                            # they differ only in membership: the file's values stand
            if applier:
                _unlink_if(p_conf, b_raw)
            return False
        data.update({"decision": did, **({"supersedes": after["id"]} if after else {})})
        project.log.append("decision.recorded", artifact=home_artifact(root), actor="scheduler", data=data)
        mine[did] = rel
        _write_json(_mine_file(root), mine)
        rep.asked.append({"file": orig, "conflict": rel, "decision": did, "summary": data["summary"]})
        return True

    if cur is None:
        if prev is None:                            # the first question: any computer that sees the copy may ask, and
            return ask() or wrote                   # the id makes it one decision
        if not applier:                             # a question about another content of this copy exists: the applier
            if board.decision_open(prev):           # alone asks again, and this computer shows the one there is
                rep.asked.append({"file": orig, "conflict": rel, "decision": prev["id"],
                                  "summary": (prev.get("data") or {}).get("summary")})
            return wrote
        why = ("The other copy changed after you answered, so nothing was written. Choose again." if _answered(prev)
               else "The other copy changed, so it is asked about again.")
        return ask(prev, why) or wrote
    mine[cur["id"]] = rel                           # whoever recorded it, this computer follows it from now on
    _write_json(_mine_file(root), mine)
    data, res = cur["data"], cur.get("resolution") or {}
    if res.get("gone"):                             # closed: only the applier asks again
        if not applier:
            return wrote
        why = ("The other copy changed again, so it is asked about again." if res.get("superseded")   # never "came back"
               else "The other copy came back after it was removed, so it is asked about again.")
        return ask(cur, why) or wrote
    if board.decision_open(cur):
        rep.asked.append({"file": orig, "conflict": rel, "decision": cur["id"], "summary": data.get("summary")})
        return wrote
    if not applier:
        o = owner(root)
        rep.waiting.append({"file": orig, "conflict": rel, "decision": cur["id"], "owner": o.get("person", "the owner"),
                            "text": f"The two copies of {name} are answered. Waiting for {o.get('person', 'the owner')}'s "
                                    "computer to apply this."})
        return wrote
    if data.get("file_sha") != _sha(a_raw):         # the file changed (or appeared, or went) after the question
        return ask(cur, "The file changed after the question was answered, so nothing was written. Choose again.") or wrote
    picked = _answer(data, res, b_raw)
    if picked is None:                              # an answer that does not fit this question (an old app, a hand edit)
        return ask(cur, "That answer no longer matches the two files, so nothing was written. Choose again.") or wrote
    result = _apply(orig, a_raw, b_raw, picked)
    from ..project.model import ReadOnlyProject, guard_write
    try:
        guard_write(p_orig)                        # a newer AVE wrote this project: write nothing, delete no copy, keep it open
        _archive(root, cur["id"], orig, rel, a_raw, b_raw, result if result is not None else a_raw)
        if result is not None and result != a_raw:
            _cas_write(p_orig, result, a_raw)
    except ReadOnlyProject:
        return wrote
    _unlink_if(p_conf, b_raw)                      # only after the file holds the answer, and both sides are archived
    keys = sorted(k for k, side in picked.items() if side == "copy") if isinstance(picked, dict) else []
    project.log.append("note.added", artifact=cur.get("artifact"), actor="scheduler", data={
        "kind": "settings_merged", "file": orig, "keys": keys, "decision": cur["id"],
        "text": f"Settled the two copies of {name} as chosen" + (f": {', '.join(map(_label, keys))} from the other copy" if keys else "") + "."})
    rep.settled.append({"file": orig, "keys": keys})
    mine.pop(cur["id"], None)
    _write_json(_mine_file(root), mine)
    return True


# ------------------------------------------------------------------------------------------------------------- the archive
_ARCHIVE_ID = re.compile(r"[A-Za-z0-9_-]{1,120}")


def _archive_file(root: Path, did: str) -> Path | None:
    return Path(root) / ".ave" / "local" / "conflict-archive" / f"{did}.json" if _ARCHIVE_ID.fullmatch(did or "") else None


def _archive(root: Path, did: str, orig: str, rel: str, a_raw: bytes | None, b_raw: bytes, applied: bytes | None) -> None:
    """Both sides of a settings conflict as they were, kept before the answer is written and the other copy removed (local)."""
    import base64
    f = _archive_file(root, did)
    if f is None:
        raise ValueError(f"cannot archive decision {did!r}")
    enc = lambda raw: base64.b64encode(raw).decode() if raw is not None else None    # noqa: E731
    _write_json(f, {"v": 1, "decision": did, "file": orig, "conflict": rel, "file_bytes": enc(a_raw), "copy_bytes": enc(b_raw),
                    "applied_sha": _sha(applied), "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})


def archived(root, did: str) -> dict | None:
    """The archive kept for a decision this computer applied, or None."""
    f = _archive_file(Path(root), did)
    v = _read_json(f, {}) if f is not None else {}
    return v or None


def _restore_reopened(root: Path, project, st) -> set[str]:
    """A question whose applied answer was taken back (opened again): the file as it was, and the other copy beside it, so the
    question is asked again with both sides. Only the computer that applied it has the archive. In a project a newer AVE saved,
    nothing is put back yet (the archive stays). Returns the copies put back."""
    import base64
    from ..project import board
    from ..project.model import ReadOnlyProject, guard_write
    adir = Path(root) / ".ave" / "local" / "conflict-archive"
    out: set[str] = set()
    for f in sorted(adir.glob("*.json")) if adir.is_dir() else []:
        rec = _read_json(f, {})
        did, orig, rel = rec.get("decision"), rec.get("file"), rec.get("conflict")
        d = st.decisions.get(did) if isinstance(did, str) else None
        if (d is None or not board.decision_open(d) or not isinstance(orig, str) or not isinstance(rel, str)
                or original_of(rel) != orig or ".." in Path(rel).parts or Path(rel).is_absolute()):
            continue
        try:
            a_raw = base64.b64decode(rec["file_bytes"]) if rec.get("file_bytes") is not None else None
            b_raw = base64.b64decode(rec["copy_bytes"])
        except (KeyError, TypeError, ValueError):
            continue
        p_orig, p_conf = Path(root) / orig, Path(root) / rel
        try:
            guard_write(p_orig)
            guard_write(p_conf)
            try:
                now = p_orig.read_bytes()
            except FileNotFoundError:
                now = None
            if _sha(now) == rec.get("applied_sha") and now != a_raw:     # still as the answer left it: put it back
                if a_raw is None:
                    p_orig.unlink(missing_ok=True)
                else:
                    _cas_write(p_orig, a_raw, now)
            if not p_conf.exists():
                _cas_write(p_conf, b_raw, None)
        except (ReadOnlyProject, Changed):
            continue
        name = Path(orig).name
        project.log.append("note.added", artifact=d.get("artifact"), actor="scheduler", data={
            "kind": "settings_restored", "file": orig, "decision": did,
            "text": f"The answer for {name} was taken back, so both copies are back and the question is open again."})
        f.unlink(missing_ok=True)
        out.add(rel)
    return out


def _text(raw: bytes | None) -> str:
    if raw is None:
        return "(no file)"
    t = raw.decode("utf-8", "replace")
    return t if len(t) <= TEXT_CAP else t[:TEXT_CAP] + "\n…"


def _form(raw: bytes | None) -> str | None:
    """A short tag for a whole file that is not a settings object ("Not valid JSON"), else None."""
    if raw is None:
        return None
    try:
        v = json.loads(raw)
    except ValueError:
        return "Not valid JSON"
    return None if isinstance(v, dict) else "Not a settings object"


def _question(root: Path, orig: str, rel: str, a_raw: bytes | None, b_raw: bytes, why: str, *, synced: bool = False,
              manager=None) -> dict | None:
    """The decision's content, worked out once from the two files, project.json's members and (when sync runs) who
    Syncthing says wrote each. The record carries the words every computer shows (`file_label`, `copy_label`, the
    options), so none of them works the names out again. None when there is nothing for a person to choose."""
    meta = _meta(root)
    fa, fb = _sides(root, meta, orig, rel, synced, manager)
    a = _read_side(a_raw) if a_raw is not None else _MISSING
    b = _read_side(b_raw)
    name = Path(orig).name
    is_pj = orig == ".ave/project.json"
    both = fa.person and fb.person and fa.person != fb.person
    who = {"file_who": fa.person or fa.label, "copy_who": fb.person or fb.label, "file_label": fa.label,
           "copy_label": fb.label, "people": [x for x in dict.fromkeys((fa.person, fb.person)) if x]}

    def summary(what: str = "") -> str:
        return f"{fa.person} and {fb.person} both changed {name}{what}" if both else f"{name} was changed on two computers at once{what}"

    if isinstance(a, dict) and isinstance(b, dict):
        keys = compare(a, b, protect=MEMBERSHIP_KEYS if is_pj else ())
        if not keys:
            return None
        opts = options(fa, fb)
        what = f" ({', '.join(k['label'] for k in keys[:4])}{' and more' if len(keys) > 4 else ''})"
        return {"kind": DECISION_KIND, "summary": summary(what), "options": opts,
                "chosen": None, "file": orig, "conflict_file": rel, "copy_sha": _sha(b_raw), "file_sha": _sha(a_raw),
                "whole": False, "keys": keys, **who,
                "reason": ((why + " ") if why else "") + "Both copies are kept until you choose. Pick each setting, or use all of one file.",
                "values": [{"option": opts[0], "person": fa.person, "side": "file", "preview": fa.label},
                           {"option": opts[1], "person": fb.person, "side": "copy", "preview": fb.label}]}
    # one side is not a JSON object (or the file is missing): a choice between the two whole files, shown side by side
    can_use_copy = not is_pj and (not must_be_object(orig) or isinstance(b, dict))
    first = KEEP_REMOVED if a_raw is None else KEEP_FILE
    opts = [first] + ([USE_COPY] if can_use_copy else [])
    if not can_use_copy:
        note = "The other copy is not a valid settings file, so it can only be set aside."
    else:
        note = "The file is missing here; using the other copy puts it back." if a_raw is None else ""
    return {"kind": DECISION_KIND, "summary": summary(), "options": opts, "chosen": None,
            "file": orig, "conflict_file": rel, "copy_sha": _sha(b_raw), "file_sha": _sha(a_raw), "whole": True, "keys": [],
            **who, "texts": {"file": _text(a_raw), "copy": _text(b_raw)},
            "forms": {"file": _form(a_raw), "copy": _form(b_raw)},
            "reason": " ".join(x for x in (why, note, "Both copies are kept until you choose.") if x),
            "values": [{"option": opts[0], "person": fa.person, "side": "file", "preview": fa.label}] +
                      ([{"option": opts[1], "person": fb.person, "side": "copy", "preview": fb.label}] if can_use_copy else [])}


def _answer(data: dict, res: dict, b_raw: bytes | None = None):
    """"file", "copy", or {key: "file" | "copy"} from a resolution, mapped by the answered record's own `values[].side`;
    None when it fits none of them, or would write a copy that is not a settings object where one is expected."""
    choice = res.get("choice")
    if choice == PER_KEY and not data.get("whole"):
        return valid_picks({"data": data}, res.get("picks"))
    side = next((v.get("side") for v in data.get("values") or [] if isinstance(v, dict) and v.get("option") == choice), None)
    if side == "copy" and data.get("whole") and data.get("file") == ".ave/project.json":
        return None                                 # never the copy's bytes for project.json
    if side == "copy" and data.get("whole") and must_be_object(str(data.get("file") or "")) and \
            not isinstance(_read_side(b_raw) if b_raw is not None else None, dict):
        return None                                 # never a list or text where a settings object is read
    return side if side in ("file", "copy") else None


def _apply(orig: str, a_raw: bytes | None, b_raw: bytes, picked) -> bytes | None:
    """The file's new bytes for an answer, read from the two files as they are now (None: leave the file as it is)."""
    is_pj = orig == ".ave/project.json"
    a = _read_side(a_raw) if a_raw is not None else _MISSING
    b = _read_side(b_raw)
    if picked == "file":
        return None
    if not (isinstance(a, dict) and isinstance(b, dict)):
        if picked != "copy" or is_pj or (must_be_object(orig) and not isinstance(b, dict)):
            return None
        return b_raw
    out = dict(a)
    keys = [k for k in list(a) + [k for k in b if k not in a] if not (is_pj and k in MEMBERSHIP_KEYS)]
    for k in keys:
        side = "copy" if picked == "copy" else picked.get(k, "file")
        if side == "copy":
            if k in b:
                out[k] = b[k]
            else:
                out.pop(k, None)
    return _encode(out, is_pj)


def _close_gone(root: Path, project, mine: dict, rule_list=None) -> None:
    """A decision this computer follows whose conflict copy is gone (applied elsewhere, or deleted by hand): closed if
    still open, and forgotten. Never while the copy exists. Only the computer that applies answers closes one (the owner's
    for a copy that syncs, this one for a copy that does not); the others only forget it. Only this computer's own record
    is read."""
    gone = {did: c for did, c in mine.items() if not (root / str(c)).exists()}
    if not gone:
        return
    owner_here = None
    if project is None:
        from ..project.model import Project
        try:
            project = Project.open(root)
        except Exception:  # noqa: BLE001
            return
    from ..project import board
    st = project.state()
    for did, c in gone.items():
        d = st.decisions.get(did)
        synced = rule_list is not None and zones.zone_of(str(c), rule_list) != "local"
        if synced and owner_here is None:
            owner_here = is_owner(root)
        if d is not None and board.decision_open(d) and (not synced or owner_here):
            project.log.append("decision.resolved", artifact=d.get("artifact"), actor="scheduler",
                               data={"decision": did, "choice": None, "gone": True})
        mine.pop(did, None)
    _write_json(_mine_file(root), mine)


def is_setting_decision(d: dict) -> bool:
    return (d.get("data") or {}).get("kind") == DECISION_KIND


# ------------------------------------------------------------------------------------------------------------- the worker
_wq: dict[str, dict] = {}
_wcv = threading.Condition()
_wbusy = [0]
_wthread: list[threading.Thread | None] = [None]
_listeners: list[Callable[[Path, Report], None]] = []


def queue(root, *, items: Iterable[str] | None = None, manager=None) -> None:
    """Ask the worker for a pass over `root`. `items` (paths Syncthing finished) add up until it runs; None asks for the
    settings sweep; an empty list looks only at the copies already known."""
    key = str(Path(root))
    with _wcv:
        job = _wq.setdefault(key, {"items": set(), "sweep": False, "manager": None})
        if items is None:
            job["sweep"] = True
        else:
            job["items"] |= {str(i) for i in items}
        if manager is not None:
            job["manager"] = manager
        if _wthread[0] is None or not _wthread[0].is_alive():
            _wthread[0] = threading.Thread(target=_work, name="ave-conflicts", daemon=True)
            _wthread[0].start()
        _wcv.notify_all()


def _work() -> None:
    while True:
        with _wcv:
            while not _wq:
                if not _wcv.wait(60):
                    if not _wq:
                        _wthread[0] = None
                        return
            key, job = next(iter(_wq.items()))
            del _wq[key]
            _wbusy[0] += 1
        try:
            rep = check(key, manager=job["manager"], items=None if job["sweep"] else sorted(job["items"]))
            for fn in list(_listeners):
                try:
                    fn(Path(key), rep)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001 - the worker never dies
            pass
        finally:
            with _wcv:
                _wbusy[0] -= 1
                _wcv.notify_all()


def on_report(fn: Callable[[Path, Report], None]) -> Callable[[], None]:
    _listeners.append(fn)
    return lambda: _listeners.remove(fn) if fn in _listeners else None


def drain(timeout: float = 10.0) -> bool:
    """Wait until the worker has nothing queued or running (tests)."""
    end = time.monotonic() + timeout
    with _wcv:
        while _wq or _wbusy[0]:
            left = end - time.monotonic()
            if left <= 0:
                return False
            _wcv.wait(left)
    return True
