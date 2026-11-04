"""Syncthing configuration built from project.json and this computer.

Each project is one Syncthing folder: id = project_id, path = the project folder, shared only with the devices of the
people listed in its project.json. Every public path (relays, global and local discovery, NAT, usage reporting, crash
reporting, auto-upgrade) is off, and Syncthing listens on one explicit address: this computer's Tailscale address in the
app, loopback in tests. A wildcard or "default" listen address is refused.

`build_config()` is pure; `prepare_folder()` writes the generated `.stignore` and the `.stfolder` marker before a folder is
added; `patch_config_xml()` applies the options to config.xml before Syncthing first starts, so it never announces itself
even for the seconds before the REST configuration lands.
"""

from __future__ import annotations

import ipaddress
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

from ..project import zones

SYNC_OPTIONS = {"relaysEnabled": False, "globalAnnounceEnabled": False, "localAnnounceEnabled": False,
                "natEnabled": False, "urAccepted": -1, "autoUpgradeIntervalH": 0, "startBrowser": False}
FOLDER_DEFAULTS = {"type": "sendreceive", "ignorePerms": True, "modTimeWindowS": 2, "maxConflicts": 10,
                   "autoNormalize": True, "fsWatcherEnabled": True,
                   "versioning": {"type": "trashcan", "params": {"cleanoutDays": "0"}}}
# Sync stops writing into a folder whose drive has less than this free (Syncthing's own default is 1 %, which on a big
# external drive is tens of GB and on a full laptop disk too little for one more clip). The status says how much is free.
MIN_DISK_FREE = {"value": 5, "unit": "GB"}
# Beyond the architecture's list: no crash reports to syncthing.net, no LAN addresses in announcements (announcing is off anyway).
EXTRA_OPTIONS = {"crashReportingEnabled": False, "announceLANAddresses": False}
SYNC_PORT = 22417                   # AVE's own port, so a Syncthing the person runs on 22000 never collides
TAILSCALE_NETS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
PROJECT_ID_RE = re.compile(r"^prj_[A-Za-z0-9]{1,64}$")
GUI_USER = "ave"
MARKER = ".stfolder"
DEVICE_ID_RE = re.compile(r"^[A-Z2-7]{7}(-[A-Z2-7]{7}){7}$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Peer:
    """Another computer, as verified at pairing: its AVE device id (the id project.json lists), its Syncthing device id and
    the address it listens on."""
    device: str
    sync_id: str
    address: str
    name: str = ""


def _ip_ok(ip, allow_loopback: bool) -> bool:
    return any(ip in n for n in TAILSCALE_NETS if n.version == ip.version) or (allow_loopback and ip.is_loopback)


def listen_url(address: str, port: int = SYNC_PORT, *, allow_loopback: bool = False) -> str:
    """`tcp://<ip>:<port>` for one explicit Tailscale address (loopback only when `allow_loopback`, for tests). Wildcards,
    hostnames, "dynamic"/"default", other schemes and any address outside Tailscale's ranges are refused."""
    if "://" in address:
        u = urlsplit(address)
        if u.scheme != "tcp" or not u.hostname or u.port is None:
            raise ConfigError(f"address must be tcp://<ip>:<port>, got {address!r}")
        host, port = u.hostname, u.port
    else:
        host = address
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise ConfigError(f"address must be an IP address, got {host!r}") from None
    if ip.is_unspecified or ip.is_multicast:
        raise ConfigError(f"refusing {ip}: that is every interface")
    if not _ip_ok(ip, allow_loopback):
        raise ConfigError(f"refusing {ip}: not a Tailscale address (100.64.0.0/10 or fd7a:115c:a1e0::/48)")
    return f"tcp://[{ip}]:{port}" if ip.version == 6 else f"tcp://{ip}:{port}"


def allowed_networks(allow_loopback: bool = False) -> list[str]:
    return [str(n) for n in TAILSCALE_NETS] + (["127.0.0.0/8", "::1/128"] if allow_loopback else [])


def check_project_id(pid) -> str:
    if not isinstance(pid, str) or not PROJECT_ID_RE.match(pid):
        raise ConfigError(f"project.json has an invalid project_id {pid!r}")
    return pid


def real(root) -> Path:
    return Path(os.path.realpath(Path(root).expanduser()))


def options(listen: str, *, allow_loopback: bool = False) -> dict:
    return {**SYNC_OPTIONS, **EXTRA_OPTIONS, "listenAddresses": [listen_url(listen, allow_loopback=allow_loopback)]}


def device_entry(sync_id: str, name: str, addresses: list[str], *, allow_loopback: bool = False) -> dict:
    if not DEVICE_ID_RE.match(sync_id):
        raise ConfigError(f"not a Syncthing device id: {sync_id!r}")
    return {"deviceID": sync_id, "name": name, "addresses": addresses, "autoAcceptFolders": False, "introducer": False,
            "compression": "metadata", "paused": False, "allowedNetworks": allowed_networks(allow_loopback)}


def peer_entry(p: "Peer", *, allow_loopback: bool = False) -> dict:
    return device_entry(p.sync_id, p.name or p.device, [listen_url(p.address, allow_loopback=allow_loopback)],
                        allow_loopback=allow_loopback)


def member_devices(meta: dict) -> dict[str, str]:
    """AVE device id -> person, for every device listed under a member of this project."""
    out: dict[str, str] = {}
    for m in meta.get("members") or []:
        if isinstance(m, dict):
            for d in m.get("devices") or []:
                if isinstance(d, str):
                    out[d] = str(m.get("person") or "")
    return out


def shared_peers(meta: dict, peers: Iterable[Peer]) -> list[Peer]:
    """Only the peers whose AVE device is a listed member of this project."""
    listed = member_devices(meta)
    return [p for p in peers if p.device in listed]


def folder_entry(root: Path, meta: dict, local_sync_id: str, peers: Iterable[Peer], *, members_only: bool = True,
                 min_disk_free: dict | None = None) -> dict:
    pid = check_project_id(meta.get("project_id"))
    chosen = shared_peers(meta, peers) if members_only else list(peers)
    devices = [local_sync_id] + sorted({p.sync_id for p in chosen} - {local_sync_id})
    return {**FOLDER_DEFAULTS, "versioning": {**FOLDER_DEFAULTS["versioning"], "params": dict(FOLDER_DEFAULTS["versioning"]["params"])},
            "id": pid, "label": str(meta.get("title") or pid), "path": str(real(root)), "markerName": MARKER,
            "devices": [{"deviceID": d} for d in devices], "paused": False, "minDiskFree": dict(min_disk_free or MIN_DISK_FREE)}


def build_config(local_sync_id: str, listen: str, projects: Iterable[tuple[Path, dict]], peers: Iterable[Peer],
                 local_name: str = "", *, allow_loopback: bool = False) -> dict:
    """{"options", "devices", "folders"} for this computer. A peer appears only if it is a member of at least one project."""
    peers = list(peers)
    folders = [folder_entry(root, meta, local_sync_id, peers) for root, meta in projects]
    used = {d["deviceID"] for f in folders for d in f["devices"]} - {local_sync_id}
    by_id = {p.sync_id: p for p in peers}
    devices = [device_entry(local_sync_id, local_name, ["dynamic"])]
    devices += [peer_entry(by_id[s], allow_loopback=allow_loopback) for s in sorted(used)]
    return {"options": options(listen, allow_loopback=allow_loopback), "devices": devices, "folders": folders}


# ---------------------------------------------------------------------------------------------------------------- on disk
def prepare_folder(root: Path, ave_device: str) -> str:
    """Write the generated `.stignore` (this device's rules from project.json) and the `.stfolder` marker. Returns the
    ignore text. The marker is what lets Syncthing tell an unplugged drive from "everything was deleted"."""
    root = Path(root)
    if not root.is_dir():
        raise ConfigError(f"{root} is not there (drive unplugged?)")
    if not (root / ".ave" / "project.json").is_file():
        # An empty mount point where the drive should be: making the marker here would tell Syncthing an empty folder is
        # the project, and it would send "everything was deleted" to every member.
        raise ConfigError(f"{root} has no .ave/project.json (drive not connected?)")
    text = zones.stignore(zones.project_rules(root, ave_device))
    tmp = root / f".stignore.{os.getpid()}.tmp"
    tmp.write_text(text)
    os.replace(tmp, root / ".stignore")
    (root / MARKER).mkdir(exist_ok=True)
    return text


def _xml_value(v) -> str:
    return ("true" if v else "false") if isinstance(v, bool) else str(v)


def patch_config_xml(path: Path, listen: str, socket_path: Path, api_key: str, *, allow_loopback: bool = False) -> None:
    """Apply the options, the listen address and the GUI (a 0600 Unix socket inside the 0700 home) to config.xml while
    no Syncthing runs on this home."""
    tree = ET.parse(path)
    root = tree.getroot()
    opts = root.find("options")
    if opts is None:
        raise ConfigError(f"{path} has no <options>")
    for el in opts.findall("listenAddress"):
        opts.remove(el)
    la = ET.SubElement(opts, "listenAddress")
    la.text = listen_url(listen, allow_loopback=allow_loopback)
    for key, value in {**SYNC_OPTIONS, **EXTRA_OPTIONS}.items():
        _set(opts, key, _xml_value(value))
    g = root.find("gui")
    if g is None:
        g = ET.SubElement(root, "gui")
    g.set("enabled", "true")
    g.set("tls", "false")
    _set(g, "address", f"unix://{socket_path}")
    _set(g, "unixSocketPermissions", "0600")
    _set(g, "apikey", api_key)
    dd = root.find("defaults/device")
    if dd is not None:
        _set(dd, "autoAcceptFolders", "false")
    tmp = path.with_name(path.name + ".ave.tmp")
    tree.write(tmp, encoding="utf-8", xml_declaration=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _set(parent, tag: str, text: str) -> None:
    el = parent.find(tag)
    if el is None:
        el = ET.SubElement(parent, tag)
    el.text = text


def read_gui(path: Path) -> tuple[str, str]:
    """(gui address, api key) from config.xml, to talk to an instance left running by an app that crashed."""
    g = ET.parse(path).getroot().find("gui")
    return (g.findtext("address") or "", g.findtext("apikey") or "") if g is not None else ("", "")
