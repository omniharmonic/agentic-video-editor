"""Two isolated Syncthing homes on loopback, and a realistic project tree to sync between them.

Each home is a private folder with its own Syncthing config, keys and database, listening on 127.0.0.1 at its own port.
Nothing touches the real ~/Library/Application Support/AVE or any Syncthing the person runs: `--home` and HOME both point
into the test's tmp folder. Skipped when no syncthing binary is found (AVE_SYNCTHING or the local release build).
"""

from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from ave.episode import resolve_layout
from ave.project import zones
from ave.syncthing import Peer, SyncError, SyncManager, find_binary


def binary_or_skip() -> Path:
    try:
        return find_binary()
    except SyncError as exc:
        pytest.skip(str(exc))


def short_dir() -> Path:
    """A short private folder: the control socket path must fit in 104 bytes, and pytest's tmp_path is too long."""
    import tempfile
    return Path(tempfile.mkdtemp(prefix="avs", dir="/tmp")).resolve()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Home:
    name: str
    ave_device: str
    port: int
    manager: SyncManager

    @property
    def peer(self) -> Peer:
        return Peer(self.ave_device, self.manager.device_id(), f"tcp://127.0.0.1:{self.port}", self.name)


def make_home(base: Path | None, name: str, ave_device: str) -> Home:
    """`base=None` uses the app's default home under HOME (tests set a private HOME)."""
    port = free_port()
    m = SyncManager(base / name / "syncthing" if base else None, f"tcp://127.0.0.1:{port}", binary=binary_or_skip(),
                    ave_device=ave_device, name=name, allow_loopback=True)
    return Home(name, ave_device, port, m)


def wait_until(pred, timeout: float = 90.0, every: float = 0.25, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while True:
        v = pred()
        if v:
            return v
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(every)


# ------------------------------------------------------------------------------------------------------------ project trees
ODD = {"preset": "numbered", "work": "01 [PROJECT]/work", "publish": "01 [PROJECT]/publish", "edits": "01 [PROJECT]/ed{its}",
       "captions": "01 [PROJECT]/cap*s", "raw_dirs": ["02 FOOTAGE?"], "exports": "!out", "music": "#music"}
INTERNAL = {".stignore", ".stfolder", ".stversions"}
# Local files each computer's own sync manager writes for itself (the pinned owner, the publisher it saw): never synced.
OWN_LOCAL = {".ave/local/owner.json", ".ave/local/publisher.json"}


def write_project_json(root: Path, project_id: str, title: str, layout, members: list[dict], publisher: str) -> dict:
    meta = {"v": 1, "project_id": project_id, "title": title, "layout": layout, "members": members, "publisher_device": publisher}
    (root / ".ave").mkdir(parents=True, exist_ok=True)
    (root / ".ave" / "project.json").write_text(json.dumps(meta, indent=2) + "\n")
    return meta


def tree_paths(layout_spec, studio: bool, writer: str) -> list[str]:
    """Every kind of file a project folder holds: each zone, each layout folder, credentials, AppleDouble and OS junk,
    and names no rule mentions."""
    L = resolve_layout(layout_spec)
    w, ed, cap, pub, exp, gfx, mus = (L[k] for k in ("work", "edits", "captions", "publish", "exports", "gfx", "music"))
    p = [f".ave/log/{writer}.jsonl", f".ave/log/.{writer}.jsonl.tmp", ".ave/local/index.sqlite", ".ave/local/locks/a.lock",
         ".ave/room/stage.json", "studio.json" if studio else "episode.json", "PROGRESS.md", "readme.txt", "notes/idea.txt",
         ".DS_Store", "._episode.json", "credentials-prod.json", ".env", "approval.key", "id_ed25519",
         f"{ed}/rough.v1.json", f"{ed}/._rough.v1.json", f"{ed}/.DS_Store", f"{ed}/token.json", f"{ed}/signing.pem",
         f"{ed}/secret_garden.json", f"{cap}/a.srt", f"{cap}/a.ass",
         f"{w}/transcripts/a.json", f"{w}/render/a.wav", f"{w}/state.json", f"{w}/.env.local",
         f"{gfx}/lower.png", f"{mus}/bed.wav", f"{exp}/final.mp4", f"{exp}/client_secret_123.json", f"{exp}/Token.JSON",
         f"{pub}/queue/q.json", f"{pub}/approval.key"]
    for raw in L["raw_dirs"]:
        p += [f"{raw}/a.mov", f"{raw}/day2/b.wav", f"{raw}/proxies/a.mp4", f"{raw}/proxies/._a.mp4", f"{raw}/._a.mov",
              f"{raw}/credentials_scene.json", f"{raw}/.DS_Store", f"{raw}/cam.p12"]
    if studio:
        p += ["posts/p1.json", "campaigns/c1.json", "recipes/r.yaml", "schedule/proposals/a.json", "schedule/acct1.yaml",
              "schedule/state/acct1.json", "schedule/heartbeat.json", "library/assets/a.png", "accounts/acct1.yaml",
              "accounts/acct1.live.json", "ledger/events.jsonl", "policy/publish.yaml", "PAUSE"]
    return p


def write_tree(root: Path, paths: list[str]) -> None:
    for rel in paths:
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(f"{rel}\n".encode() * 3)


def _local(rel: str) -> bool:
    """Anything under .ave/local/ is this computer's own (locks, the conflict watcher's history, caches): never synced."""
    return rel == ".ave/local" or rel.startswith(".ave/local/")


def files_in(root: Path) -> set[str]:
    out = set()
    for f in root.rglob("*"):
        rel = f.relative_to(root).as_posix()
        if rel.split("/")[0] in INTERNAL or rel in OWN_LOCAL or _local(rel) or not f.is_file():
            continue
        out.add(rel)
    return out


def dirs_in(root: Path) -> set[str]:
    """Folders, leaving out those that hold only this computer's own local files (OWN_LOCAL) and the parents of nothing else."""
    def own_only(d: Path) -> bool:
        files = [f.relative_to(root).as_posix() for f in d.rglob("*") if f.is_file()]
        return bool(files) and all(f in OWN_LOCAL or _local(f) for f in files)
    return {d.relative_to(root).as_posix() for d in root.rglob("*")
            if d.is_dir() and d.relative_to(root).parts[0] not in INTERNAL and not _local(d.relative_to(root).as_posix())
            and not own_only(d)}


def expected_on_receiver(sender_root: Path, sender_device: str, receiver_root: Path, receiver_device: str) -> set[str]:
    """What zone_of says arrives: a file syncs when neither side's rules keep it local."""
    rs = zones.project_rules(sender_root, sender_device)
    rr = zones.project_rules(receiver_root, receiver_device)
    return {p for p in files_in(sender_root) if zones.zone_of(p, rs) != "local" and zones.zone_of(p, rr) != "local"}
