"""Share one project with one person: invitations, the six-digit check, membership and publisher handover.

The owner's side                                          The invitee's side
    text = invite(project, "tomas", "proxies", manager=m)     (the owner sends `text` directly: Messages, a call...)
                                                              project = accept(text, folder, manager=m)
                                                              join_code(project) -> "123456", shown only here, read aloud
    confirm_invitee(project, "123456", manager=m)             confirm_join(project, manager=m)
    (the owner types what was read aloud; the owner's screen shows no code)

**Who is a member** comes only from the project's log: the `member.added` and `member.removed` events the project's owner
device wrote in its own log file. The owner device is recorded in project.json (`owner_device`, else the publisher of a
project made before it was recorded) and pinned on each computer the first time it is read
(`.ave/local/owner.json`; on an invitee, from the verified invitation). Every computer works out the devices it shares
with from that log (`share_list`): members the owner added and did not later remove. A removal wins over an add the
owner wrote before it. project.json's `members` holds names and each person's media choice for display and for the
ignore rules; it never decides who receives anything. So a removal reaches every member with the owner's log, a stale
or concurrent project.json cannot bring a removed member back, a removed member cannot re-add herself, and a member who
is not the owner cannot add anybody.

**The code**: the invitee's computer shows six digits worked out from both Syncthing device ids and the invitation's
nonce. The owner types the digits the invitee reads aloud; they are matched separately against each computer that
answered (at most 5 per invitation), and only an exact match for exactly one computer is accepted. The invitee's side
confirms by checking that the computer offering the project is the Syncthing device the invitation named. Nothing is
shared until both have confirmed.

How the owner learns who answered: the invitee's Syncthing dials the owner's device, which refuses the unknown device but
lists it as pending, with the name the invitee's computer gives itself while joining
(`ave-join:<tag>:<AVE device>:<address>`).

Both sides write project.json with the same bytes (`canonical()`), so it does not conflict when they first meet.
project.json records `kind`, so the invitee gets the studio rules before studio.json arrives.

Handing publishing to another member writes `project.publisher_changed` and project.json's `publisher_device`; nothing
sends from the new publisher until a person approves it again there (`publish_blockers`).

Syncthing's device id authenticates peers; Tailscale is only the network, so a node shared into the tailnet is accepted.
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..project import zones
from ..project.log import DEVICE_ID_RE as AVE_DEVICE_RE
from ..project.log import LogError, log_filename, read_log, read_logs
from ..project.model import KINDS, Device, Project, ProjectError, short_id
from ..project.reducer import saw
from . import config as C
from .config import ConfigError, Peer

INVITE_TTL_S = 600
MAX_ANSWERS = 5
PREFIX = "AVE-INVITE-1."
JOIN_NAME = "ave-join"
MEDIA_WORDS = {"full": "all the media", "proxies": "review proxies plus what they work on"}
_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
_ISO = "%Y-%m-%dT%H:%M:%S%z"


class MemberError(ValueError):
    """Something a person can act on, said plainly."""


# ------------------------------------------------------------------------------------------------------------- basics
def canonical(meta: dict) -> bytes:
    """The one way project.json is written by sharing, so the inviter and the invitee write identical bytes."""
    return (json.dumps(meta, indent=2, ensure_ascii=False) + "\n").encode()


def _root(project) -> Path:
    return Path(project if isinstance(project, (str, os.PathLike)) else project.root)


def read_meta(project) -> dict:
    path = _root(project) / ".ave" / "project.json"
    try:
        meta = json.loads(path.read_text())
    except FileNotFoundError:
        raise MemberError(f"{_root(project)} is not a project") from None
    except ValueError as exc:
        raise MemberError(f"{path} is not valid JSON ({exc})") from None
    if not isinstance(meta, dict):
        raise MemberError(f"{path} must hold a JSON object")
    C.check_project_id(meta.get("project_id"))
    return meta


def _write_bytes(path: Path, data: bytes) -> None:
    from ..project.model import guard_write
    guard_write(path)          # project.json of a project a newer AVE saved stays as that AVE wrote it
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    tmp.write_bytes(data)
    fd = os.open(tmp, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def write_meta(project, meta: dict) -> bytes:
    data = canonical(meta)
    _write_bytes(_root(project) / ".ave" / "project.json", data)
    if not isinstance(project, (str, os.PathLike)):
        project.meta = meta
    return data


def _device(manager) -> Device:
    dev = manager.ave_device
    return Device(dev, short_id(dev))


def _open(root: Path, device: Device) -> Project:
    p = Project(root, read_meta(root), device)
    try:
        p.check_devices()
    except ProjectError as exc:
        raise MemberError(str(exc)) from None
    return p


def _members(meta: dict) -> list[dict]:
    return [m for m in meta.get("members") or [] if isinstance(m, dict)]


def _member_of(meta: dict, device: str) -> dict | None:
    return next((m for m in _members(meta) if device in (m.get("devices") or [])), None)


def _member_named(meta: dict, person: str) -> dict | None:
    want = (person or "").strip().casefold()
    return next((m for m in _members(meta) if str(m.get("person") or "").casefold() == want), None)


def _person(meta: dict, device: str | None) -> str:
    m = _member_of(meta, device) if device else None
    return str(m.get("person")) if m and m.get("person") else "someone"


def _log(project: Project, op: str, data: dict) -> dict:
    return project.log.append(op, data=data, actor="human")


def _require_running(manager) -> None:
    if getattr(manager, "state", "running") != "running":
        raise MemberError("Sync is not running on this computer yet. Wait a moment, check that Tailscale is on, and try again.")


def comparison_code(inviter_sync: str, invitee_sync: str, nonce: str) -> str:
    """The six digits the invitee's computer shows: from both Syncthing device ids and the invitation's nonce."""
    h = hashlib.sha256(f"ave-pair/1|{inviter_sync}|{invitee_sync}|{nonce}".encode()).digest()
    return f"{int.from_bytes(h[:8], 'big') % 1_000_000:06d}"


def display_code(code: str) -> str:
    return f"{code[:3]} {code[3:]}"


def _typed(code) -> str:
    digits = re.sub(r"[\s-]", "", str(code or ""))
    if not re.fullmatch(r"\d{6}", digits):
        raise MemberError("Type the six digits the other person reads out.")
    return digits


def _tag(nonce: str) -> str:
    return hashlib.sha256(("ave-join|" + nonce).encode()).hexdigest()[:12]


def add_member(meta: dict, person: str, device: str, media: str, sync_id: str, address: str) -> dict:
    """project.json with one more member (names and media for display and ignore rules; never who receives anything).
    Deterministic: both sides of an invitation compute the same result from the same project.json."""
    out = copy.deepcopy(meta)
    out["members"] = [m for m in _members(out) if device not in (m.get("devices") or [])]
    out["members"].append({"person": person, "devices": [device], "media": media})
    devices = dict(out.get("devices") or {}) if isinstance(out.get("devices"), dict) else {}
    devices[device] = {"sync_id": sync_id, "address": address}
    out["devices"] = devices
    return out


# ------------------------------------------------------------------------------------------------------------- authority
def _owner_file(root: Path) -> Path:
    return root / ".ave" / "local" / "owner.json"


def owner_of(project) -> dict:
    """The project's owner device, pinned on this computer: {"device", and on an invitee "sync_id", "address", "person"}.
    First read from project.json's `owner_device` (else `publisher_device`, for a project made before it was recorded)
    and kept in `.ave/local/owner.json`, so a later edit of project.json cannot move ownership."""
    root = _root(project)
    try:
        rec = json.loads(_owner_file(root).read_text())
        if isinstance(rec, dict) and isinstance(rec.get("device"), str) and AVE_DEVICE_RE.match(rec["device"]):
            return rec
    except (OSError, ValueError):
        pass
    meta = read_meta(root)
    dev = meta.get("owner_device") or meta.get("publisher_device")
    if not isinstance(dev, str) or not AVE_DEVICE_RE.match(dev):
        raise MemberError("This project does not say which computer owns it, so its members cannot change.")
    rec = {"device": dev}
    _write_bytes(_owner_file(root), (json.dumps(rec) + "\n").encode())
    return rec


@dataclass
class Membership:
    members: dict[str, dict]        # sync id -> {"device", "person", "address", "media"}: added by the owner, not removed
    removed_syncs: set[str]
    removed_devices: set[str]
    bound: dict[str, str]           # AVE device -> the Syncthing id the owner ever added it with
    removals: list[dict]            # the owner's member.removed events

    def devices(self) -> set[str]:
        return {m["device"] for m in self.members.values() if m.get("device")}


def membership(project, owner_device: str | None = None) -> Membership:
    """Members from the owner's own log file only: `member.added` minus `member.removed` that came after it."""
    root = _root(project)
    owner = owner_device or owner_of(root)["device"]
    try:
        evs = [e for e in read_log(root / ".ave" / "log" / log_filename(owner))
               if e.get("device") == owner and e.get("op") in ("member.added", "member.removed")]
    except LogError:
        evs = []
    evs.sort(key=lambda e: (e["hlc"], e["id"]))
    adds = [e for e in evs if e["op"] == "member.added"]
    removes = [e for e in evs if e["op"] == "member.removed"]
    members: dict[str, dict] = {}
    bound: dict[str, str] = {}
    removed_syncs: set[str] = set()
    removed_devices: set[str] = set()
    for r in removes:
        d = r.get("data") or {}
        removed_syncs |= {s for s in d.get("sync_ids") or [] if isinstance(s, str)}
        removed_devices |= {s for s in d.get("devices") or [] if isinstance(s, str)}
    for a in adds:
        d = a.get("data") or {}
        sid, dev = d.get("sync_id"), d.get("device")
        if not isinstance(sid, str) or not C.DEVICE_ID_RE.match(sid):
            continue
        if isinstance(dev, str):
            bound.setdefault(dev, sid)
        cancelled = [r for r in removes if (sid in ((r.get("data") or {}).get("sync_ids") or [])
                                            or (dev and dev in ((r.get("data") or {}).get("devices") or [])))
                     and not saw(a, r)]        # a removal wins unless the owner added them again after reading it
        if cancelled:
            members.pop(sid, None)
            continue
        members[sid] = {"device": dev if isinstance(dev, str) else None, "person": str(d.get("person") or ""),
                        "address": str(d.get("address") or ""), "media": d.get("media")}
    live = set(members)
    return Membership(members, removed_syncs - live, removed_devices - {m["device"] for m in members.values()},
                      bound, removes)


def _current_folder_peers(manager, fid: str) -> list[Peer]:
    """The devices Syncthing shares this folder with now, as peers (for a project made before membership was logged)."""
    try:
        folder = next((f for f in manager._folders() if f["id"] == fid), None)
    except Exception:  # noqa: BLE001
        return []
    if not folder:
        return []
    me = manager.device_id()
    out = []
    for d in folder.get("devices", []):
        sid = d["deviceID"]
        if sid == me:
            continue
        try:
            cfg = manager.api.get(f"/rest/config/devices/{sid}")
            addr = next(a for a in cfg.get("addresses") or [] if a.startswith("tcp://"))
            out.append(Peer(str(cfg.get("name") or sid), sid, addr, str(cfg.get("name") or "")))
        except Exception:  # noqa: BLE001 - a device we cannot describe is not re-shared
            continue
    return out


def share_list(project, manager) -> list[Peer]:
    """The devices this computer shares the project with: members the owner added and did not remove, the owner itself
    (from this computer's pinned record), and, for a project from before membership was logged, the devices the folder
    is already shared with. Never a device only because project.json lists it."""
    root = _root(project)
    meta = read_meta(root)
    owner = owner_of(root)
    mem = membership(root, owner["device"])
    me = manager.device_id()
    peers: dict[str, Peer] = {}
    for sid, m in mem.members.items():
        if sid != me and m["address"]:
            peers[sid] = Peer(m["device"] or sid, sid, m["address"], m["person"])
    osid = owner.get("sync_id")
    if isinstance(osid, str) and osid != me and osid not in peers and osid not in mem.removed_syncs and owner.get("address"):
        peers[osid] = Peer(owner["device"], osid, owner["address"], str(owner.get("person") or ""))
    if "devices" not in meta:                  # legacy: keep who the folder is shared with, minus anyone removed
        for p in _current_folder_peers(manager, meta["project_id"]):
            if p.sync_id not in peers and p.sync_id not in mem.removed_syncs:
                peers[p.sync_id] = p
    return sorted(peers.values(), key=lambda p: p.sync_id)


def is_removed(project, manager) -> bool:
    """True when the owner removed this computer from the project."""
    root = _root(project)
    try:
        mem = membership(root)
    except MemberError:
        return False
    me_sync = manager.device_id()
    if me_sync in mem.members or owner_of(root)["device"] == manager.ave_device:
        return False
    return me_sync in mem.removed_syncs or manager.ave_device in mem.removed_devices


def _require_owner(root: Path, manager) -> dict:
    owner = owner_of(root)
    if owner["device"] != manager.ave_device:
        raise MemberError("Only the computer that owns this project can change who is on it.")
    return owner


def _self_and_legacy_logged(root: Path, manager, meta: dict) -> None:
    """On the owner, before the first membership change: log the owner itself and anyone the folder is already shared
    with (a project from before membership was logged), so nobody already on it is dropped."""
    mem = membership(root, manager.ave_device)
    me = manager.device_id()
    p = None
    if me not in mem.members:
        p = p or _open(root, _device(manager))
        own = _member_of(meta, manager.ave_device) or {}
        _log(p, "member.added", {"person": _person(meta, manager.ave_device), "device": manager.ave_device, "sync_id": me,
                                 "address": manager.listen_address(), "media": own.get("media", "full")})
    if "devices" in meta:
        return
    for peer in _current_folder_peers(manager, meta["project_id"]):
        if peer.sync_id in mem.members or peer.sync_id in mem.removed_syncs:
            continue
        m = next((x for x in _members(meta) if str(x.get("person") or "").casefold() == peer.name.casefold()
                  or peer.name in (x.get("devices") or [])), None)
        dev = next(iter(m.get("devices") or []), None) if m else None
        p = p or _open(root, _device(manager))
        _log(p, "member.added", {"person": (m or {}).get("person") or peer.name, "device": dev, "sync_id": peer.sync_id,
                                 "address": peer.address, "media": (m or {}).get("media", "proxies")})


# ------------------------------------------------------------------------------------------------------------ invitation
def _encode(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    return PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def parse_invitation(text: str, *, now: float | None = None, allow_loopback: bool = False) -> dict:
    """The invitation in `text`, checked. Refuses anything malformed, for another kind of thing, or expired."""
    t = re.sub(r"\s+", "", str(text or ""))
    if not t.startswith(PREFIX):
        raise MemberError("That is not an AVE project invitation. Copy the whole text you were sent.")
    body = t[len(PREFIX):]
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, binascii.Error):
        raise MemberError("That invitation is damaged. Copy the whole text again, or ask for a new one.") from None
    try:
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise ValueError("version")
        pid = C.check_project_id(payload.get("project_id"))
        proj = payload["project"]
        if not isinstance(proj, dict) or proj.get("project_id") != pid:
            raise ValueError("project")
        if payload.get("kind") not in KINDS or proj.get("kind") != payload["kind"]:
            raise ValueError("kind")
        if not isinstance(payload.get("title"), str):
            raise ValueError("title")
        inv, who = payload["inviter"], payload["invitee"]
        if not (isinstance(inv, dict) and isinstance(inv.get("person"), str) and AVE_DEVICE_RE.match(str(inv.get("device")))
                and C.DEVICE_ID_RE.match(str(inv.get("sync_id")))):
            raise ValueError("inviter")
        C.listen_url(str(inv.get("address")), allow_loopback=allow_loopback)
        if proj.get("owner_device") != inv["device"]:
            raise ValueError("only the owner invites")
        if not (isinstance(who, dict) and isinstance(who.get("person"), str) and who["person"].strip()
                and who.get("media") in zones.MEDIA_POLICIES):
            raise ValueError("invitee")
        if not _NONCE_RE.match(str(payload.get("nonce"))) or not isinstance(payload.get("expires"), (int, float)):
            raise ValueError("nonce")
        if not _member_of(proj, inv["device"]):
            raise ValueError("inviter is not a member")
    except (KeyError, TypeError, ValueError, ConfigError):
        raise MemberError("That invitation is damaged or was not made by AVE. Ask for a new one.") from None
    if (time.time() if now is None else now) >= payload["expires"]:
        raise MemberError(f"This invitation has expired: they last 10 minutes. Ask {payload['inviter']['person']} for a new one.")
    return payload


def _invitations_dir(root: Path) -> Path:
    return root / ".ave" / "local" / "invitations"


def invite(project, person: str, media: str = "proxies", *, manager, now: float | None = None) -> str:
    """An invitation for `person` to this one project, valid for 10 minutes. `media` is what they will keep: "full" or
    "proxies". Only the owner's computer can invite. The text grants nothing by itself: joining needs the code."""
    root = _root(project)
    now = time.time() if now is None else now
    person = (person or "").strip()
    if not person or len(person) > 80 or any(c in person for c in "\n\r\t"):
        raise MemberError("Write the person's name, as the project should show it.")
    if media not in zones.MEDIA_POLICIES:
        raise MemberError(f"Media must be one of {', '.join(zones.MEDIA_POLICIES)}.")
    _require_running(manager)
    me = manager.ave_device
    meta = read_meta(root)
    if not _member_of(meta, me):
        raise MemberError("This computer is not on this project, so it cannot invite anyone to it.")
    owner = _require_owner(root, manager)
    if _member_named(meta, person):
        raise MemberError(f"{person} is already on this project.")
    sync_id, address = manager.device_id(), manager.listen_address()
    _self_and_legacy_logged(root, manager, meta)
    updated = copy.deepcopy(meta)
    updated["kind"] = meta["kind"] if meta.get("kind") in KINDS else ("studio" if (root / "studio.json").is_file() else "episode")
    updated["owner_device"] = owner["device"]
    devices = dict(updated.get("devices") or {}) if isinstance(updated.get("devices"), dict) else {}
    devices[me] = {"sync_id": sync_id, "address": address}
    updated["devices"] = devices
    if canonical(updated) != canonical(meta):
        write_meta(project, updated)
        meta = updated
    nonce = secrets.token_hex(16)
    payload = {"v": 1, "project_id": meta["project_id"], "title": str(meta.get("title") or ""), "kind": meta["kind"],
               "project": meta,
               "inviter": {"person": _person(meta, me), "device": me, "sync_id": sync_id, "address": address},
               "invitee": {"person": person, "media": media}, "expires": now + INVITE_TTL_S, "nonce": nonce}
    d = _invitations_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{nonce}.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return _encode(payload)


def _pending_invitations(root: Path, now: float) -> list[dict]:
    out = []
    d = _invitations_dir(root)
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            inv = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(inv, dict) or not _NONCE_RE.match(str(inv.get("nonce"))):
            continue
        if now >= float(inv.get("expires") or 0):
            f.unlink(missing_ok=True)              # an expired invitation can never be confirmed
            continue
        out.append(inv)
    return out


def cancel_invitation(project, nonce: str | None = None) -> None:
    d = _invitations_dir(_root(project))
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        if nonce is None or f.stem == nonce:
            f.unlink(missing_ok=True)


@dataclass(frozen=True)
class Answer:
    """A computer answering an invitation: who it says it is. It carries no code: the owner's screen never shows one."""
    nonce: str
    person: str
    media: str
    device: str
    sync_id: str
    address: str
    problem: str = ""              # why this answer can never be confirmed (it claims a device that is not its own)


def _answer_matches(a: Answer, inviter_sync: str, typed: str) -> bool:
    return comparison_code(inviter_sync, a.sync_id, a.nonce) == typed


def answers(project, *, manager, now: float | None = None) -> list[Answer]:
    """Computers that answered one of this project's open invitations: at most MAX_ANSWERS per invitation, the earliest
    first. Several can answer (anyone who saw the text could try); the code the invitee reads aloud tells them apart."""
    root = _root(project)
    now = time.time() if now is None else now
    invs = {_tag(i["nonce"]): i for i in _pending_invitations(root, now)}
    if not invs:
        return []
    meta = read_meta(root)
    mem = membership(root)
    taken = {d for m in _members(meta) for d in (m.get("devices") or [])} | mem.devices()
    per: dict[str, int] = {}
    out = []
    pending = sorted((manager.pending_devices() or {}).items(), key=lambda kv: (str((kv[1] or {}).get("time") or ""), kv[0]))
    for sync_id, info in pending:
        parts = str((info or {}).get("name") or "").split(":", 3)
        if len(parts) != 4 or parts[0] != JOIN_NAME or parts[1] not in invs:
            continue
        inv = invs[parts[1]]
        device, address = parts[2], parts[3]
        try:
            if not C.DEVICE_ID_RE.match(sync_id) or not AVE_DEVICE_RE.match(device):
                continue
            address = C.listen_url(address, allow_loopback=manager.allow_loopback)
        except ConfigError:
            continue
        if per.get(inv["nonce"], 0) >= MAX_ANSWERS:
            continue
        per[inv["nonce"]] = per.get(inv["nonce"], 0) + 1
        problem = ""
        if device in taken or device == inv["inviter"]["device"] or sync_id in mem.members:
            problem = "That computer says it is a device already on this project. Nothing was shared. Make a new invitation."
        elif device in mem.bound and mem.bound[device] != sync_id:
            problem = "That computer claims a device this project knows under another identity. Nothing was shared."
        out.append(Answer(inv["nonce"], inv["invitee"]["person"], inv["invitee"]["media"], device, sync_id, address, problem))
    return out


def wait_for_answers(project, *, manager, cancelled=lambda: False, poll_s: float = 0.5) -> list[Answer]:
    """Block until a computer answers one of this project's open invitations. Stops when the invitations expire or when
    `cancelled()` says so, with a plain reason."""
    root = _root(project)
    while True:
        if cancelled():
            cancel_invitation(root)
            raise MemberError("Sharing cancelled. The invitation no longer works.")
        if not _pending_invitations(root, time.time()):
            raise MemberError("The invitation expired before anyone used it. Make a new one when they are ready.")
        got = answers(root, manager=manager)
        if got:
            return got
        time.sleep(poll_s)


def confirm_invitee(project, code, *, manager, now: float | None = None) -> Project:
    """The owner typed the code the invitee read aloud. It is matched against each answering computer's own code, and
    only an exact match for exactly one computer is accepted: that computer is shared with first, then logged as
    `member.added` and written into project.json. A code that matches nothing shares nothing."""
    root = _root(project)
    now = time.time() if now is None else now
    typed = _typed(code)
    _require_running(manager)
    _require_owner(root, manager)
    invs = {i["nonce"]: i for i in _pending_invitations(root, now)}
    if not invs:
        raise MemberError("The invitation expired before it was confirmed. Nothing was shared. Make a new one.")
    found = answers(root, manager=manager, now=now)
    match = [a for a in found if _answer_matches(a, invs[a.nonce]["inviter"]["sync_id"], typed)]
    if not match:
        raise MemberError("That code does not match any computer that answered. Nothing was shared. "
                          "Ask them to read the code on their screen again.")
    if len(match) > 1:
        for a in match:
            cancel_invitation(root, a.nonce)
        raise MemberError("More than one computer answered with that code. Nothing was shared. Make a new invitation.")
    a = match[0]
    if a.problem:
        cancel_invitation(root, a.nonce)
        raise MemberError(a.problem)
    meta = read_meta(root)
    if _member_named(meta, a.person):
        raise MemberError(f"{a.person} is already on this project.")
    peers = share_list(root, manager)
    manager.share(root, peers + [Peer(a.device, a.sync_id, a.address, a.person)], authorized=True)
    try:
        p = _open(root, _device(manager))
        _log(p, "member.added", {"person": a.person, "device": a.device, "media": a.media, "sync_id": a.sync_id,
                                 "address": a.address})
        write_meta(project, add_member(meta, a.person, a.device, a.media, a.sync_id, a.address))
    except Exception:
        manager.share(root, peers, authorized=True)      # roll back: they were never recorded, so they are not shared
        raise
    cancel_invitation(root, a.nonce)
    manager.dismiss_pending(a.sync_id)
    return _open(root, _device(manager))


# --------------------------------------------------------------------------------------------------------- joining
def _join_file(root: Path) -> Path:
    return root / ".ave" / "local" / "joining.json"


def _folder_name(title: str, pid: str) -> str:
    name = re.sub(r"[/:\\\0]", "-", title).strip().strip(".")
    return name[:100] or pid


def accept(text: str, root, *, manager, now: float | None = None) -> Project:
    """Accept an invitation into `root`, the folder the invitee chose: the project folder is made there, named after the
    project, with project.json written from the invitation. Nothing is shared until `confirm_join`."""
    now = time.time() if now is None else now
    inv = parse_invitation(text, now=now, allow_loopback=manager.allow_loopback)
    _require_running(manager)
    me, my_sync = manager.ave_device, manager.device_id()
    if inv["inviter"]["sync_id"] == my_sync or inv["inviter"]["device"] == me:
        raise MemberError("This invitation was made on this computer. Send it to the person you are inviting.")
    if _member_of(inv["project"], me):
        raise MemberError("This computer is already on this project.")
    existing = next((f for f in manager._folders() if f["id"] == inv["project_id"]), None)
    if existing:
        raise MemberError(f"This project is already on this computer, at {existing['path']}.")
    parent = Path(root).expanduser()
    if not parent.is_dir():
        raise MemberError(f"{parent} is not there. Choose a folder that exists.")
    target = parent / _folder_name(inv["title"], inv["project_id"])
    if target.exists():
        raise MemberError(f"There is already a folder called {target.name} in {parent}. Choose another place.")
    my_address = manager.listen_address()
    meta = add_member(inv["project"], inv["invitee"]["person"].strip(), me, inv["invitee"]["media"], my_sync, my_address)
    target.mkdir()
    for d in ("log", "local"):
        (target / ".ave" / d).mkdir(parents=True, exist_ok=True)
    write_meta(target, meta)
    _join_file(target).write_text(json.dumps({"invitation": inv, "sync_id": my_sync}, indent=2, ensure_ascii=False) + "\n")
    inviter = inv["inviter"]
    manager.pairing.add(inviter["sync_id"])
    manager.add_device(Peer(inviter["device"], inviter["sync_id"], inviter["address"], inviter["person"]))
    manager.set_name(f"{JOIN_NAME}:{_tag(inv['nonce'])}:{me}:{my_address}")
    return _open(target, _device(manager))


def _joining(root: Path) -> dict:
    try:
        rec = json.loads(_join_file(root).read_text())
        rec["invitation"]["nonce"]
        return rec
    except (OSError, ValueError, KeyError, TypeError):
        raise MemberError("There is no invitation waiting to be confirmed in this project.") from None


def join_code(project) -> str:
    """The six digits only the invitee's screen shows, read aloud to the owner."""
    rec = _joining(_root(project))
    inv = rec["invitation"]
    return comparison_code(inv["inviter"]["sync_id"], rec["sync_id"], inv["nonce"])


def joining(project) -> dict | None:
    """What the invitee is joining, for the sheet: inviter, title, media, code. None when nothing is waiting."""
    try:
        rec = _joining(_root(project))
    except MemberError:
        return None
    inv = rec["invitation"]
    return {"inviter": inv["inviter"]["person"], "title": inv["title"], "media": inv["invitee"]["media"],
            "code": join_code(project), "expires": inv["expires"]}


def _inviter_offers(manager, sync_id: str, fid: str) -> bool:
    """The inviter confirmed: the Syncthing device the invitation named is connected and offers this project."""
    try:
        offered = ((manager.api.get("/rest/cluster/pending/folders") or {}).get(fid) or {}).get("offeredBy") or {}
        conns = (manager.api.get("/rest/system/connections") or {}).get("connections") or {}
    except Exception:  # noqa: BLE001
        return False
    return sync_id in offered and bool((conns.get(sync_id) or {}).get("connected"))


def confirm_join(project, *, manager, now: float | None = None, wait_s: float = 30.0) -> Project:
    """The invitee read the code aloud and the owner typed it. Before anything is shared, check that the computer
    offering the project is the Syncthing device the invitation named (so the owner really confirmed); then share."""
    root = _root(project)
    rec = _joining(root)
    inv = rec["invitation"]
    who = inv["inviter"]["person"]
    if (time.time() if now is None else now) >= inv["expires"]:
        cancel_join(root, manager=manager)
        raise MemberError(f"The invitation expired before it was confirmed. Nothing was shared. Ask {who} for a new one.")
    _require_running(manager)
    deadline = time.monotonic() + wait_s
    while not _inviter_offers(manager, inv["inviter"]["sync_id"], inv["project_id"]):
        if time.monotonic() >= deadline:
            raise MemberError(f"{who}'s computer has not confirmed yet. Nothing was shared. Ask {who} to type the code "
                              "on your screen, then try again.")
        time.sleep(0.25)
    owner = {"device": inv["inviter"]["device"], "sync_id": inv["inviter"]["sync_id"],
             "address": inv["inviter"]["address"], "person": who}
    _write_bytes(_owner_file(root), (json.dumps(owner) + "\n").encode())
    manager.set_name()
    manager.share(root)
    manager.pairing.discard(inv["inviter"]["sync_id"])
    _join_file(root).unlink(missing_ok=True)
    return _open(root, _device(manager))


def cancel_join(project, *, manager) -> None:
    """Stop joining: forget the inviter's device and remove the folder `accept` made (it holds nothing else yet)."""
    root = _root(project)
    try:
        rec = _joining(root)
    except MemberError:
        return
    sync_id = rec["invitation"]["inviter"]["sync_id"]
    manager.pairing.discard(sync_id)
    try:
        manager.set_name()
        manager.forget_device(sync_id)
    finally:
        if {p.name for p in root.iterdir()} <= {".ave"}:
            shutil.rmtree(root, ignore_errors=True)
        else:
            _join_file(root).unlink(missing_ok=True)


# ------------------------------------------------------------------------------------------------------- membership
def members(project) -> list[dict]:
    """[{person, devices, media, publisher}] for display."""
    meta = read_meta(project)
    pub = meta.get("publisher_device")
    return [{"person": m.get("person"), "devices": list(m.get("devices") or []), "media": m.get("media", "full"),
             "publisher": pub in (m.get("devices") or [])} for m in _members(meta)]


def removal_message(person: str, title: str) -> str:
    return (f"{person} is no longer on {title or 'this project'}. Nothing new reaches their computer from this one from now "
            f"on, and the other members' computers stop too as soon as they next hear from this one. "
            f"They keep everything they already received.")


def remove(project, person: str, *, manager) -> str:
    """Take `person` off the project (owner only). Sharing with their devices stops first; then `member.removed` is
    logged, which every member follows, and project.json drops them. Safe to retry. Returns the sentence to show."""
    root = _root(project)
    _require_running(manager)
    _require_owner(root, manager)
    meta = read_meta(root)
    _self_and_legacy_logged(root, manager, meta)   # a project from before membership was logged: who is on it, by Syncthing id
    mem = membership(root)
    want = (person or "").strip().casefold()
    m = _member_named(meta, person)
    syncs = {s for s, x in mem.members.items() if x["person"].casefold() == want}
    devices = {d for d in (m or {}).get("devices") or [] if isinstance(d, str)}
    devices |= {mem.members[s]["device"] for s in syncs if mem.members[s]["device"]}
    syncs |= {s for s, x in mem.members.items() if x.get("device") in devices}
    name = str((m or {}).get("person") or next((mem.members[s]["person"] for s in syncs), person))
    title = str(meta.get("title") or "")
    if not m and not syncs:
        gone = any(str((r.get("data") or {}).get("person") or "").casefold() == want for r in mem.removals)
        if not gone:
            raise MemberError(f"{person} is not on this project.")
        manager.share(root, share_list(root, manager), authorized=True)    # a retry: make sure sharing really stopped
        return removal_message(name, title)
    if manager.ave_device in devices or manager.device_id() in syncs:
        raise MemberError("You cannot remove yourself here. Ask another member to remove you.")
    if meta.get("publisher_device") in devices:
        raise MemberError(f"{name}'s computer publishes this project. Hand publishing to someone else first.")
    keep = [p for p in share_list(root, manager) if p.sync_id not in syncs and p.device not in devices]
    manager.share(root, keep, authorized=True)  # their devices leave the folder and Syncthing forgets them
    _log(_open(root, _device(manager)), "member.removed", {"person": name, "devices": sorted(devices), "sync_ids": sorted(syncs)})
    if m:
        out = copy.deepcopy(meta)
        out["members"] = [x for x in _members(out) if str(x.get("person") or "").casefold() != want]
        if isinstance(out.get("devices"), dict):
            out["devices"] = {k: v for k, v in out["devices"].items() if k not in devices}
        write_meta(project, out)
    return removal_message(name, title)


def set_media(project, media: str, *, manager) -> None:
    """This computer's own media choice: "full" or "proxies". Regenerates .stignore right away."""
    if media not in zones.MEDIA_POLICIES:
        raise MemberError(f"Media must be one of {', '.join(zones.MEDIA_POLICIES)}.")
    root = _root(project)
    meta = read_meta(root)
    me = manager.ave_device
    out = copy.deepcopy(meta)
    m = _member_of(out, me)
    if not m:
        raise MemberError("This computer is not on this project.")
    if m.get("media") == media:
        return
    m["media"] = media
    write_meta(project, out)
    manager.refresh(root)


# --------------------------------------------------------------------------------------------------- publisher handover
def hand_over(project, person: str, *, device_id: str | None = None, to_device: str | None = None) -> str:
    """Give publishing to `person`'s computer. Only the current publisher can. Writes `project.publisher_changed` and
    project.json's publisher_device; the new publisher sends nothing until a person approves it again there."""
    root = _root(project)
    me = device_id or (project.device.id if isinstance(project, Project) else Device.load().id)
    meta = read_meta(root)
    chain = publisher_chain(root)
    if (chain[-1] if chain else meta.get("publisher_device")) != me:
        raise MemberError("Only the computer that publishes this project can hand publishing over.")
    if meta.get("publisher_device") != me and _member_of(meta, me) is not None and _member_named(meta, person) is _member_of(meta, me):
        # project.json was edited to name another computer, but no handover ever left this one: put the settings right again
        out = copy.deepcopy(meta)
        out["publisher_device"] = me
        write_meta(project, out)
        return f"This computer publishes {meta.get('title') or 'this project'} again. Nothing was handed over."
    m = _member_named(meta, person)
    if not m:
        raise MemberError(f"{person} is not on this project.")
    devices = [d for d in m.get("devices") or [] if isinstance(d, str)]
    if to_device is None:
        if len(devices) != 1:
            raise MemberError(f"{m.get('person')} has {len(devices)} computers on this project. Choose which one publishes.")
        to_device = devices[0]
    if to_device not in devices:
        raise MemberError(f"That computer is not {m.get('person')}'s.")
    if to_device == me:
        raise MemberError("This computer already publishes this project.")
    note_publisher(root, me)
    out = copy.deepcopy(meta)
    out.setdefault("owner_device", me)          # ownership stays with this computer; only publishing moves
    out["publisher_device"] = to_device
    write_meta(project, out)
    p = Project(root, out, Device(me, short_id(me)))
    _log(p, "project.publisher_changed", {"from": me, "to": to_device, "person": m.get("person")})
    who = _title(str(m.get("person") or "someone"))
    return (f"{who}'s computer now publishes {meta.get('title') or 'this project'}. Nothing sends from there "
            f"until {who} approves it again on that computer.")


def _title(person: str) -> str:
    return person[:1].upper() + person[1:] if person and person != "someone" else person


def _where(meta: dict, device: str | None) -> str:
    who = _title(_person(meta, device))
    return f"{who}'s computer" if who != "someone" else "another computer"


def publisher_chain(project) -> list[str]:
    """See `publisher_tenures`: just the devices, oldest first."""
    return [d for d, _ in publisher_tenures(project)]


def publisher_tenures(project) -> list[tuple[str, str | None]]:
    """Every computer that has published this project, oldest first; the last one publishes now. It starts at the pinned owner
    (`owner_of`, kept in `.ave/local/owner.json`, never re-read from project.json) and follows only `project.publisher_changed`
    events that the publisher of the moment wrote in its own log file, handing publishing from itself. project.json's
    `publisher_device` is not read here: a member who edits that synced file cannot make themselves the publisher.
    Empty when the owner is not known. Each entry is (device, the HLC of the handover event that made it the publisher; None for
    the owner), so an event's place in the chain can be found from its own HLC."""
    root = _root(project)
    try:
        chain: list[tuple[str, str | None]] = [(owner_of(root)["device"], None)]
    except MemberError:
        return []
    last: tuple[str, str] | None = None
    for _ in range(256):
        cur = chain[-1][0]
        try:
            evs = read_log(root / ".ave" / "log" / log_filename(cur))
        except LogError:
            break
        moves = sorted((e["hlc"], e["id"], (e.get("data") or {}).get("to")) for e in evs
                       if e.get("op") == "project.publisher_changed" and e.get("device") == cur
                       and (e.get("data") or {}).get("from") == cur and isinstance((e.get("data") or {}).get("to"), str)
                       and AVE_DEVICE_RE.match((e.get("data") or {}).get("to")) and (last is None or (e["hlc"], e["id"]) > last))
        if not moves:
            break
        last = moves[0][:2]
        chain.append((moves[0][2], moves[0][0]))
    return chain


def publisher_of(project, device_id: str | None = None) -> tuple[str | None, str | None]:
    """(the device that publishes, None) or (None, the plain sentence saying why nobody may send). Fails closed:

    - the publisher is the last of `publisher_chain` (the pinned owner plus the owner-chain of handover events);
    - a shared project whose project.json names no publisher has none, so nobody sends;
    - a solo project (one computer) with no publisher named uses its owner, or else its only computer;
    - a project.json that names a different computer than the chain (a hand edit, or a handover whose event has not arrived
      yet) has none until the two agree."""
    root = _root(project)
    meta = read_meta(root)
    devices = {d for m in _members(meta) for d in (m.get("devices") or []) if isinstance(d, str)}
    chain = publisher_chain(root)
    try:
        added = bool(membership(root, chain[0]).members) if chain else False
    except (MemberError, LogError):
        added = False
    shared = len(devices) > 1 or added
    declared = meta.get("publisher_device")
    if not chain:
        if not shared and len(devices) == 1 and not declared:
            return next(iter(devices)), None
        return None, ("Nobody can publish this project yet: it does not say which computer owns it. Nothing sends from any "
                      "computer until that is fixed.")
    current = chain[-1]
    if not declared:
        if shared:
            return None, (f"Publishing is on hold: this project's settings do not say which computer publishes. Nothing sends "
                          f"from any computer until {_where(meta, current)} hands publishing over again.")
        return current, None
    if declared != current:
        return None, (f"Publishing is on hold: this project's settings say {_where(meta, declared)} publishes, but no handover "
                      f"from {_where(meta, current)} has reached this computer. If it does not arrive once sync is up to "
                      f"date, hand publishing over again from {_where(meta, current)}.")
    return current, None


def publisher_message(project, device_id: str | None = None) -> str | None:
    """None when this computer publishes the project; otherwise the one plain sentence every other computer shows."""
    root = _root(project)
    pub, why = publisher_of(root)
    if why:
        return why
    me = device_id or Device.load().id
    if pub == me:
        return None
    meta = read_meta(root)
    where = _where(meta, pub)
    if not any(me in (m.get("devices") or []) for m in _members(meta)):
        return (f"Publishing happens on {where}. This is a copy of the project on a computer that is not part of it, so it "
                f"sends nothing. To publish from here, hand publishing over from {where} and approve again here.")
    return f"Publishing happens on {where}. This computer shows what is queued but never sends."


def _publisher_file(root: Path) -> Path:
    return root / ".ave" / "local" / "publisher.json"


def _handover_events(root: Path) -> list[dict]:
    return [e for e in read_logs(root / ".ave" / "log") if e.get("op") == "project.publisher_changed"]


def _stamp(now: float | None) -> str:
    return time.strftime(_ISO, time.localtime(time.time() if now is None else now))


def note_publisher(root, device_id: str | None = None, *, now: float | None = None) -> dict | None:
    """This computer's record of who publishes: {"publisher", "since", "changed"}. "changed" turns true, with "since" set to
    this computer's clock, the first time it sees publisher_device differ from what it saw before, or a handover event,
    whichever comes first. None when the folder is not a project."""
    root = Path(root)
    if not (root / ".ave" / "project.json").is_file():
        return None
    meta0 = read_meta(root)
    pub = meta0.get("publisher_device") or meta0.get("owner_device")    # none named: a solo project publishes from its owner
    f = _publisher_file(root)
    try:
        rec = json.loads(f.read_text())
        if not isinstance(rec, dict) or "publisher" not in rec:
            rec = None
    except (OSError, ValueError):
        rec = None
    before = json.dumps(rec, sort_keys=True)
    if rec is None:
        meta = read_meta(root)
        moved = isinstance(meta.get("owner_device"), str) and pub != meta["owner_device"]   # no longer the creator
        rec = {"publisher": pub, "since": _stamp(now) if moved else None, "changed": moved}
    if rec["publisher"] != pub:
        rec = {"publisher": pub, "since": _stamp(now), "changed": True}
    elif not rec.get("changed") and _handover_events(root):
        rec = {"publisher": pub, "since": _stamp(now), "changed": True}
    if json.dumps(rec, sort_keys=True) != before:
        _write_bytes(f, (json.dumps(rec) + "\n").encode())
    return rec


def publish_blockers(root, approval_at: str | None, device_id: str | None = None, *, approval_device: str | None = None,
                     now: float | None = None) -> list[str]:
    """Why nothing may send from this computer because publishing changed hands. Keyed on project.json's
    publisher_device, whether or not the handover event has arrived. Empty when publishing never changed hands here.
    After a change: refused on any computer that is not the publisher, and on the publisher unless the approval was made
    on this computer after it saw the change."""
    root = Path(root)
    if not (root / ".ave" / "project.json").is_file():
        return []
    rec = note_publisher(root, device_id, now=now)
    if not rec or not rec.get("changed"):
        return []
    me = device_id or Device.load().id
    pub = rec["publisher"]
    if pub != me:
        return [f"Publishing for this project moved to {_person(read_meta(root), pub)}'s computer; nothing sends from this one."]
    try:
        ok = (approval_at is not None and approval_device == me
              and datetime.strptime(approval_at, _ISO) >= datetime.strptime(rec["since"], _ISO))
    except (TypeError, ValueError):
        ok = False
    if not ok:
        return ["Publishing moved to this computer. Approve this again here before it sends."]
    return []


__all__ = ["INVITE_TTL_S", "MAX_ANSWERS", "Answer", "MemberError", "Membership", "accept", "add_member", "answers",
           "cancel_invitation", "cancel_join", "canonical", "comparison_code", "confirm_invitee", "confirm_join",
           "display_code", "hand_over", "invite", "is_removed", "join_code", "joining", "members", "membership",
           "note_publisher", "owner_of", "parse_invitation", "publish_blockers", "publisher_chain", "publisher_message", "publisher_of", "remove", "removal_message", "set_media",
           "share_list", "wait_for_answers"]
