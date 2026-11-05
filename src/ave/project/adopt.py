"""Adopting an existing episode or studio folder into the project model, without moving a file.

`adopt(root)` adds `.ave/` (project.json and this computer's log) beside what is already there and records what exists:

  the home artifact   `art_ep` for an episode folder (or `art_studio` for a studio), at the stage its furthest work reached.
                      The stage is set at the FIRST adoption only; afterwards it moves by stage events, never by re-adopting
  versions            every edit file in the edits folder, as a `version.added` event with what it needs
  posts               a studio's `posts/<id>.json` become artifacts of kind `post`, at a stage mapped from the post's own

**Out is never inferred from files alone.** Out needs the publishing approval, so adoption puts an artifact at Out only when this
computer publishes the project and holds an approved (validly signed) or recorded-sent publish for it: its own queue item with a
good signature, or a "sent" row in its own dispatch record. A built package, unapproved queue items (drafts that
`ave package queue` made) or a post whose synced status reads "published" start at Finish instead, with a decision flagged on
the artifact that says why.

It never creates, moves, renames or changes anything outside `.ave/`, and running it again adds nothing new. Original
footage is only read: edit, caption and other small files are hashed now; footage (anything in a raw folder, any media file,
anything large) is recorded with its size and mtime and `"hash": "pending"`, for readiness work to finish later.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..episode import MEDIA_EXTS, resolve_layout
from .model import Project, ProjectError
from .reducer import STAGES, State

SMALL_BYTES = 64 * 1024 * 1024        # larger files are never hashed during adoption
EDIT_STAGE_ORDER = ("assembly", "rough", "final", "select", "short")
# A studio post's own stage -> a board stage. dropped and archived are not stages: those posts are marked archived.
POST_STAGE_MAP = {"idea": "Plan", "draft": "Draft", "active": "Refine", "review": "Refine", "in_review": "Refine",
                  "approved": "Finish", "scheduled": "Finish", "published": "Out"}
POST_ARCHIVED = ("dropped", "archived")


@dataclass
class Report:
    root: str
    kind: str
    project_id: str
    title: str
    created_project: bool
    artifacts_added: list[str] = field(default_factory=list)
    versions_added: list[str] = field(default_factory=list)
    hash_pending: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------------------------------- folder facts
def folder_kind(root: Path) -> str:
    """project.json's kind when it records one (a joining member has it before studio.json arrives), else the folder's."""
    try:
        kind = json.loads((Path(root) / ".ave" / "project.json").read_text()).get("kind")
    except (OSError, ValueError, AttributeError):
        kind = None
    if kind in ("episode", "studio"):
        return kind
    return "studio" if (Path(root) / "studio.json").is_file() else "episode"


def home_artifact(root: Path) -> str:
    """The artifact a folder's edit versions belong to."""
    return "art_studio" if folder_kind(root) == "studio" else "art_ep"


def _meta(root: Path) -> dict:
    p = Path(root) / ("studio.json" if folder_kind(root) == "studio" else "episode.json")
    try:
        m = json.loads(p.read_text())
        return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


def _layout_spec(root: Path) -> Any:
    meta = _meta(root)
    if folder_kind(root) == "studio":
        from ..studio.project import STUDIO_DEFAULTS
        return meta.get("layout", STUDIO_DEFAULTS["layout"])
    return None  # Project.create reads episode.json's layout, or looks at the folder


def align_layout(root: str | Path) -> bool:
    """Make project.json's layout the one the writers use (episode.json's), when they differ: a project adopted before
    `ave start` chose a numbered episode would otherwise keep flat sync rules, and its cuts, footage and exports would
    never leave this computer. Regenerates `.stignore` when the folder is already synced. True when it changed anything.
    A folder with no episode.json (a studio) keeps project.json's layout as it is."""
    from ..episode import _NO_EPISODE, _UNREADABLE, layout_spec_of, resolve_layout
    root = Path(root)
    spec = layout_spec_of(root)
    pj = root / ".ave" / "project.json"
    if spec is _NO_EPISODE or spec is _UNREADABLE or not pj.is_file():
        return False
    meta = json.loads(pj.read_text())
    want = spec if spec is not None else "flat"
    try:
        resolve_layout(want)
    except SystemExit:                           # the episode names no valid layout: never copy that into project.json
        return False
    try:
        if resolve_layout(meta.get("layout")) == resolve_layout(want):
            return False
    except SystemExit:                           # project.json's layout is the invalid one: the episode's replaces it
        pass
    from ..syncthing.members import write_meta
    write_meta(root, {**meta, "layout": want})   # the shared settings file: other members follow it when it arrives
    if (root / ".stignore").is_file():
        from ..syncthing.config import prepare_folder
        from .zones import _this_device_id
        prepare_folder(root, _this_device_id())
    return True


def _episode(root: Path):
    if folder_kind(root) == "studio":
        from ..studio.project import StudioProject
        return StudioProject(root)
    from ..episode import Episode
    return Episode(root)


def _raw_dirs(root: Path) -> list[str]:
    try:
        return [d.strip("/") for d in resolve_layout(_meta(root).get("layout") if folder_kind(root) == "episode"
                                                     else _layout_spec(root))["raw_dirs"]]
    except SystemExit:
        return []


# ---------------------------------------------------------------------------------------------------- needs
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def need_for(root: Path, rel: str, raw_dirs: list[str] | None = None) -> dict | None:
    """What a version needs of one file: {path, sha256, bytes} for small files; footage gets {path, bytes, mtime,
    sha256: None, hash: "pending"}. A file that is not there yet is {path, sha256: None, hash: "pending"}. None for a path
    outside the project (it cannot be synced, so it cannot be a need)."""
    root = Path(root).resolve()
    rel_p = Path(rel)
    if rel_p.is_absolute():
        try:
            rel_p = rel_p.resolve().relative_to(root)
        except (ValueError, OSError):
            return None
    if ".." in rel_p.parts:
        return None
    rel = rel_p.as_posix()
    p = root / rel_p
    try:
        st = p.stat()
    except OSError:
        return {"path": rel, "sha256": None, "hash": "pending"}
    if not p.is_file():
        return None
    raw = raw_dirs if raw_dirs is not None else _raw_dirs(root)
    footage = (p.suffix.lower() in MEDIA_EXTS or st.st_size > SMALL_BYTES
               or any(rel == d or rel.startswith(d + "/") for d in raw))
    if footage:
        return {"path": rel, "bytes": st.st_size, "mtime": st.st_mtime, "sha256": None, "hash": "pending"}
    sha = _sha256(p)
    from .readiness import remember_hash
    remember_hash(root, rel, st, sha)          # this computer wrote or holds it: it never needs hashing again here
    return {"path": rel, "sha256": sha, "bytes": st.st_size}


def version_needs(root: Path, path: Path) -> list[dict]:
    """The edit file itself, its source media and its caption files."""
    root = Path(root).resolve()
    raw = _raw_dirs(root)
    rel = Path(path).resolve().relative_to(root).as_posix()
    needs = [need_for(root, rel, raw)]
    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        doc = {}
    files: list[str] = []
    for s in (doc.get("sources") or {}).values() if isinstance(doc.get("sources"), dict) else []:
        if isinstance(s, dict) and isinstance(s.get("file"), str):
            files.append(s["file"])
    caps = doc.get("captions") if isinstance(doc.get("captions"), dict) else {}
    files += [caps[k] for k in ("srt", "ass") if isinstance(caps.get(k), str)]
    seen = {rel}
    for f in files:
        n = need_for(root, f, raw)
        if n and n["path"] not in seen:
            seen.add(n["path"])
            needs.append(n)
            px = _proxy_rel(n["path"], raw)
            if px and px not in seen and (root / px).is_file():     # what a member on proxies plays this from
                pn = need_for(root, px, raw)
                if pn:
                    seen.add(px)
                    needs.append({**pn, "proxy_of": n["path"]})
    return [n for n in needs if n]


def _proxy_rel(rel: str, raw_dirs: list[str]) -> str | None:
    """`<raw>/proxies/<inner>.mov` for a file in a raw folder (ave.proxies.proxy_path, as a project-relative path)."""
    from ..episode import PROXY_DIRNAME
    for d in raw_dirs:
        if rel.startswith(d + "/"):
            inner = Path(rel[len(d) + 1:])
            if inner.parts and inner.parts[0] == PROXY_DIRNAME:
                return None
            return f"{d}/{PROXY_DIRNAME}/{inner.with_suffix('.mov').as_posix()}"
    return None


# ---------------------------------------------------------------------------------------------------- versions
def _version_data(root: Path, path: Path) -> dict:
    from ..editdoc import VERSION_RE
    m = VERSION_RE.match(path.name)
    if not m:
        raise ValueError(f"{path.name} is not an edit version name")
    return {"version": path.name[:-len(".json")], "file": Path(path).resolve().relative_to(Path(root).resolve()).as_posix(),
            "stage": m["stage"], "name": m["name"], "n": int(m["n"]), "device_tag": m["device"]}


def record_version(project: Project, path: Path, known: set[str] | None = None, actor: str | None = None) -> dict | None:
    """Log `version.added` for one edit file unless the log already has it, as `actor` (default the log's). Returns the event,
    or None if already known."""
    from ..editdoc import logged_versions
    root = Path(project.root).resolve()
    data = _version_data(root, path)
    if known is None:
        known = set(logged_versions(root))
    if data["file"] in known:
        return None
    event = project.log.append("version.added", artifact=home_artifact(root), data=data, needs=version_needs(root, path), actor=actor)
    known.add(data["file"])
    return event


def _edit_files(root: Path) -> list[Path]:
    from ..editdoc import VERSION_RE
    try:
        d = _episode(root).edits
    except SystemExit:
        return []
    if not d.is_dir():
        return []
    out = []
    for p in d.iterdir():
        m = VERSION_RE.match(p.name)
        if m and p.is_file() and not p.is_symlink():
            out.append((EDIT_STAGE_ORDER.index(m["stage"]) if m["stage"] in EDIT_STAGE_ORDER else len(EDIT_STAGE_ORDER),
                        m["stage"], m["name"] or "", int(m["n"]), m["device"] or "", p))
    return [x[-1] for x in sorted(out, key=lambda x: x[:-1])]


# ---------------------------------------------------------------------------------------------------- stages
def episode_stage(root: Path, edit_files: list[Path] | None = None) -> str:
    """The furthest existing work: an approved or sent publish -> Out, a package or queued posts -> Finish (see `_episode_out`),
    a final edit -> Finish, a rough or assembly -> Draft."""
    return _episode_stage(root, edit_files)[0]


def _episode_stage(root: Path, edit_files: list[Path] | None = None) -> tuple[str, str | None]:
    """(stage, why it is Finish and not Out, or None)."""
    from ..editdoc import VERSION_RE
    root = Path(root)
    held = None
    try:
        ep = _episode(root)
        status = ep.work / "package" / "status.json"
        built = status.is_file() and bool(json.loads(status.read_text()).get("built"))
        queue = ep.publish / "queue"
        queued = queue.is_dir() and any(queue.glob("*.json"))
        if built or queued:
            if _publish_proven(ep, [p.stem for p in sorted(queue.glob("*.json"))] if queued else []):
                return "Out", None
            held = ("Its publish queue has posts" if queued else "It has a built package") + (
                ", but none has an approved or sent publish on the computer that publishes this project, so it starts at Finish, "
                "not Out. Out needs the publishing approval: approve and send it from the publishing computer.")
            return "Finish", held
    except (OSError, ValueError, AttributeError, SystemExit):
        pass
    stages = {VERSION_RE.match(p.name)["stage"] for p in (edit_files if edit_files is not None else _edit_files(root))}
    if "final" in stages:
        return "Finish", None
    if stages & {"rough", "assembly"}:
        return "Draft", None
    return "Plan", None


def _publish_proven(ep, ids: list[str]) -> bool:
    """Whether this computer publishes `ep` and holds, for one of these queue items, a validly signed approval or a "sent" row in
    its own dispatch record. A file anyone could write (a queue status, a synced post) is never enough."""
    from ..publish import queue as pq, safety
    try:
        if pq.not_publisher(ep) is not None:
            return False
    except Exception:  # noqa: BLE001 - fails closed
        return False
    for qid in ids:
        try:
            item = pq.load(ep, qid)
        except (OSError, ValueError):
            continue
        if not isinstance(item, dict):
            continue
        try:
            if any(pq.record_mark(ep, item, plat)[0] == "sent" for plat in item.get("platforms") or []):
                return True
        except Exception:  # noqa: BLE001
            pass
        if item.get("status") in ("approved", "publishing", "published", "failed") and item.get("approvals"):
            try:
                problems = safety.approval_problems(item, str((item["approvals"][-1] or {}).get("sha256", "")))
            except Exception:  # noqa: BLE001 - no key, an unreadable record: not proven
                continue
            # a published item stays approved; an old approval has expired for a new send, not as proof it was given
            if not [x for x in problems if not x.startswith(("status is", "approval expired", "queued sha256 differs"))]:
                return True
    return False


def _post_out(root: Path, post: dict) -> bool:
    """Whether a studio post whose own status reads "published" really has an approved or sent publish on this computer."""
    from ..studio.project import StudioProject
    from ..studio.state import queue_episode
    try:
        sp = StudioProject(root)
    except Exception:  # noqa: BLE001
        return False
    for v in post.get("variants") or []:
        if not isinstance(v, dict) or v.get("dropped") or not isinstance(v.get("queue"), dict) or not v["queue"].get("id"):
            continue
        try:
            ep = queue_episode(sp, v)
        except Exception:  # noqa: BLE001
            continue
        if ep is not None and _publish_proven(ep, [str(v["queue"]["id"])]):
            return True
    return False


def _flag_out(project: Project, aid: str, why: str) -> None:
    """The decision that says why adoption put this at Finish and not Out (recorded once: its id is fixed by the artifact)."""
    did = "dec_adopt_out_" + hashlib.sha256(f"{project.project_id}|{aid}".encode()).hexdigest()[:20]
    if did in project.state().decisions:
        return
    project.log.append("decision.recorded", artifact=aid, actor="scheduler", data={
        "decision": did, "summary": "Started at Finish, not Out: no approved or sent publish was found",
        "options": ["Keep it at Finish", "It was published: I will confirm it on the publishing computer"],
        "chosen": "Keep it at Finish", "reason": why})


def post_status(post: dict) -> str:
    """A post's own stage, refined for active posts by what their variants have reached."""
    st = post.get("stage")
    if st != "active":
        return st if isinstance(st, str) else "idea"
    live = [v for v in post.get("variants") or [] if isinstance(v, dict) and not v.get("dropped")]
    if any((v.get("result") or {}).get("state") == "published" for v in live):
        return "published"
    if any(v.get("schedule") or v.get("queue") for v in live):
        return "scheduled"
    return "active"


def _post_title(post: dict, pid: str) -> str:
    idea = post.get("idea") if isinstance(post.get("idea"), dict) else {}
    copy = post.get("copy") if isinstance(post.get("copy"), dict) else {}
    for t in (post.get("title"), idea.get("text"), copy.get("base")):
        if isinstance(t, str) and t.strip():
            t = " ".join(t.split())
            return t if len(t) <= 80 else t[:79] + "…"
    return pid


# ---------------------------------------------------------------------------------------------------- adopt
def _open_or_create(root: Path) -> tuple[Project, bool]:
    if (root / ".ave").is_symlink():
        raise ProjectError(f"{root / '.ave'} is a symlink; a project's .ave folder must be a real folder inside the project")
    if (root / ".ave" / "project.json").is_file():
        return Project.open(root), False
    title = _meta(root).get("title")
    title = title if isinstance(title, str) and title.strip() else root.name
    try:
        return Project.create(root, title, layout=_layout_spec(root)), True
    except ProjectError:
        if (root / ".ave" / "project.json").is_file():  # made at the same moment by another process
            return Project.open(root), False
        raise


def adopt(root: str | Path) -> Report:
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ProjectError(f"{root} is not a folder")
    project, created = _open_or_create(root)
    if not created and align_layout(root):
        project = Project.open(root)
    kind = folder_kind(root)
    report = Report(root=str(root), kind=kind, project_id=project.project_id, title=project.title, created_project=created)
    state: State = project.state()

    def missing(aid: str) -> bool:
        a = state.artifacts.get(aid)
        return a is None or a.created is None

    edits = _edit_files(root)
    home = home_artifact(root)
    if missing(home):
        data = {"kind": kind, "title": project.title}
        held = None
        if kind == "episode":
            data["stage"], held = _episode_stage(root, edits)
        project.log.append("artifact.created", artifact=home, data=data)
        report.artifacts_added.append(home)
        if held:
            _flag_out(project, home, held)
            report.warnings.append(held)

    from ..editdoc import logged_versions
    known = set(logged_versions(root))
    for p in edits:
        e = record_version(project, p, known)
        if e:
            report.versions_added.append(e["data"]["version"])
            report.hash_pending += [n["path"] for n in e["needs"] if n.get("hash") == "pending"]

    if kind == "studio":
        posts = root / "posts"
        seen_ids: dict[str, str] = {}
        for p in sorted(posts.glob("*.json")) if posts.is_dir() else []:
            if p.name.startswith(".") or not p.is_file():
                continue
            try:
                post = json.loads(p.read_text())
                if not isinstance(post, dict):
                    raise ValueError("not an object")
            except (OSError, ValueError) as exc:
                report.warnings.append(f"posts/{p.name}: could not be read ({exc}); left as it is")
                continue
            pid = post.get("id") if isinstance(post.get("id"), str) and post.get("id") else p.stem
            aid = "art_post_" + pid
            if pid in seen_ids:
                report.warnings.append(f"posts/{p.name}: post id {pid!r} is also used by posts/{seen_ids[pid]}; only the first is recorded")
                continue
            seen_ids[pid] = p.name
            if not missing(aid):
                continue
            status = post_status(post)
            data: dict[str, Any] = {"kind": "post", "title": _post_title(post, pid), "post": pid,
                                    "file": p.relative_to(root).as_posix(), "post_stage": status}
            held = None
            if status in POST_ARCHIVED:
                data["archived"] = True
            else:
                data["stage"] = POST_STAGE_MAP.get(status, "Plan")
                if data["stage"] == "Out" and not _post_out(root, post):
                    data["stage"] = "Finish"
                    held = ("This post's status says published, but this computer has no approved or sent publish for it (a "
                            "status that synced from another computer is only shown, never proof), so it starts at Finish, not Out.")
            project.log.append("artifact.created", artifact=aid, data=data)
            report.artifacts_added.append(aid)
            if held:
                _flag_out(project, aid, held)

    report.hash_pending = sorted(set(report.hash_pending))
    report.warnings += project.log.warnings
    return report


# ---------------------------------------------------------------------------------------------------- status
def status(root: str | Path) -> dict:
    """What the project log says about a folder, for `ave project status`."""
    from ..editdoc import logged_versions
    root = Path(root).expanduser().resolve()
    if not (root / ".ave" / "project.json").is_file():
        return {"root": str(root), "adopted": False, "kind": folder_kind(root),
                "edit_files": len(_edit_files(root)), "hint": f"ave project adopt {root}"}
    project = Project.open(root)
    st = project.state()
    known = set(logged_versions(root))
    unrecorded = [p.name for p in _edit_files(root) if p.relative_to(root).as_posix() not in known]
    artifacts = []
    pending: set[str] = set()
    for aid in sorted(st.artifacts, key=lambda a: (a not in ("art_ep", "art_studio"), a)):
        a = st.artifacts[aid]
        vs = sorted(a.versions.values(), key=lambda v: v["number"])
        for v in vs:
            pending |= {n["path"] for n in v["needs"] if n.get("hash") == "pending"}
        artifacts.append({"id": aid, "kind": a.kind, "title": a.title, "stage": a.stage,
                          "archived": bool(a.fields.get("archived")), "versions": len(vs),
                          "latest": vs[-1]["version"] if vs else None})
    return {
        "root": str(root), "adopted": True, "kind": folder_kind(root), "project_id": project.project_id,
        "title": project.title, "layout": project.layout,
        "device": {"id": project.device.id, "tag": project.device.short, "person": project.person},
        "members": project.members, "artifacts": artifacts, "conflicts": len(st.conflicts),
        "unrecorded_edits": unrecorded, "hash_pending": sorted(pending), "warnings": st.warnings,
    }


__all__ = ["Report", "STAGES", "adopt", "episode_stage", "home_artifact", "need_for", "post_status", "record_version", "status",
           "version_needs"]
