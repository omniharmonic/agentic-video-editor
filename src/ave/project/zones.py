"""One sync policy for every project file.

Every path in a project belongs to exactly one zone, and the zone decides how it syncs:

  log        .ave/log/<device>.jsonl: one writer per file, merged by the reducer
  permanent  written once, never edited in place (footage, versions, transcripts, exports)
  settings   shared, rarely changing; a conflict keeps both values (project.json, episode.json, posts, ...)
  local      never leaves this computer (credentials, approval keys, publish queue, dispatch history, locks, caches)

`rules()` is the only table. `zone_of()` reads it, `stignore()` writes it for Syncthing, and `credential_warnings()` (used by
`ave doctor`) checks against it, so the three cannot disagree. First match wins. Anything unmatched is local: a new file or
folder is private until a rule here says otherwise.

Glob syntax: a leading "/" roots the glob at the project folder; without it the glob matches at any depth. `*` stops at "/",
`**` crosses it, `?` is one character, a backslash makes the next character literal (layout folder names are escaped with it, so
a folder called `01 [PROJECT]` is safe). A glob matches the path itself and everything beneath it. Matching ignores case on
macOS and Windows, as Syncthing does there.
"""

from __future__ import annotations

import functools
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Literal

from ..episode import MEDIA_EXTS

Zone = Literal["log", "permanent", "settings", "local"]
ZONES: tuple[str, ...] = ("log", "permanent", "settings", "local")
MEDIA_POLICIES = ("full", "proxies")
PROXY_DIRNAME = "proxies"          # same folder name as ave.episode.PROXY_DIRNAME (kept literal: that module is heavy)

# Names that are credentials whatever folder they sit in. These are local rules at the top of the table, so they never sync;
# the doctor still reports them by name so the person knows one is lying in a shared folder.
CREDENTIAL_LOCAL_GLOBS = ("*.pem", "*.p12", "*.pfx", "*.jks", ".netrc", ".npmrc", "approval.key",
                          ".env", ".env.local", ".env.*.local", ".env.development", ".env.production", ".env.staging", ".env.test",
                          ".env.dev", ".env.prod", "id_rsa", "id_rsa.pub", "id_ed25519", "id_ed25519.pub", "client_secret*.json",
                          "credentials.json", "token.json", "*_token.json")
# Names that are credentials only when they are not part of the creative material: they stay local everywhere except inside a
# raw footage folder, where a file called credentials_scene.json is a script, not a key. Placed after the raw-folder rules.
CREDENTIAL_LATE_GLOBS = ("credentials-*.json", "credentials_*.json")
# Broader names that are usually, but not always, credentials. Warn-only, and only for non-media files in a syncing zone
# (a clip called "secret_garden.mov" is not a secret).
CREDENTIAL_WARN_GLOBS = ("*token*", "*secret*", "*.key", "*.env", "*credential*")
# Media, images and captions are never credentials. No credential glob above ends in one of these; a test enforces it.
_MEDIA_EXTS = frozenset(MEDIA_EXTS) | {".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".bmp", ".heic", ".svg", ".srt",
                                       ".ass", ".vtt", ".psd", ".exr", ".dpx"}
CASE_INSENSITIVE = sys.platform in ("darwin", "win32")
_SPECIAL = "[]{}*?\\"


@dataclass(frozen=True)
class Rule:
    glob: str
    zone: Zone
    deletable: bool = False        # Syncthing (?d): may be deleted along with its folder (OS junk)


def _norm(p: str) -> str:
    p = str(PurePosixPath(p.replace("\\", "/")))
    return p.strip("/")


def _lit(name: str) -> str:
    """A literal path with every Syncthing pattern character escaped."""
    return "".join("\\" + c if c in _SPECIAL else c for c in name)


def _unescape(glob: str) -> str:
    return re.sub(r"\\(.)", r"\1", glob)


@functools.lru_cache(maxsize=None)
def _regex(glob: str, ci: bool) -> re.Pattern[str]:
    rooted = glob.startswith("/")
    body, i, out = glob.lstrip("/"), 0, []
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            out.append(re.escape(body[i + 1]))
            i += 2
            continue
        if body.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        out.append("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c))
        i += 1
    return re.compile(("^" if rooted else "^(?:.*/)?") + "".join(out) + "(?:/.*)?$", re.I if ci else 0)


def _layout_dir(layout: dict, key: str, value: str) -> str:
    n = _norm(value) if isinstance(value, str) else ""
    if not n or n == "." or str(value).startswith(("/", "\\")) or ".." in n.split("/") or re.match(r"^[A-Za-z]:", n):
        raise ValueError(f"layout {key} must be a folder inside the project (not empty, '.', absolute or containing '..'), got {value!r}")
    return n


def rules(layout: dict, member: dict | None = None, *, studio: bool = False) -> list[Rule]:
    """The ordered rule table for one project and one member. `layout` is a resolved episode layout (`resolve_layout`),
    `member` is a project.json member ({"media": "full" | "proxies"}); `studio=True` adds the studio folders."""
    media = (member or {}).get("media", "full")
    if media not in MEDIA_POLICIES:
        raise ValueError(f"member media must be one of {MEDIA_POLICIES}, got {media!r}")
    raw_dirs = [_lit(_layout_dir(layout, "raw_dirs", d)) for d in layout["raw_dirs"]]
    work, edits, captions, publish, exports = (_lit(_layout_dir(layout, k, layout[k])) for k in ("work", "edits", "captions", "publish", "exports"))
    gfx, music = _lit(_layout_dir(layout, "gfx", layout["gfx"])), _lit(_layout_dir(layout, "music", layout["music"]))
    r: list[Rule] = []

    def add(glob: str, zone: Zone, deletable: bool = False) -> None:
        r.append(Rule(glob, zone, deletable))

    for g in CREDENTIAL_LOCAL_GLOBS:           # first, so no later rule can ever make one sync
        add(g, "local")
    add("._*", "local", True)                  # AppleDouble
    add(".DS_Store", "local", True)
    add("/.ave/project.json", "settings")
    add("/.ave/log/*.jsonl", "log")
    add("/.ave", "local")                      # local/ (index, leases, locks, room, sync state), lock and temp files
    add("/studio.json" if studio else "/episode.json", "settings")
    # Raw footage folders, most specific first so a nested rule wins over its parent.
    for d in raw_dirs:
        if media == "proxies":
            add(f"/{d}/{PROXY_DIRNAME}", "permanent")
            add(f"/{d}", "local")
        else:
            add(f"/{d}", "permanent")
    for g in CREDENTIAL_LATE_GLOBS:
        add(g, "local")
    if studio:
        # Shared state of the studio's plan.
        # posts/ syncs (ruling): every member sees the same plan. A post's status in a synced file is only a hint: the send path
        # must never act on it (Task 20 proves it decides from the publisher's own ledger and approval, which stay local).
        add("/posts", "settings")
        add("/campaigns", "settings")
        add("/recipes", "settings")
        add("/schedule/proposals", "settings")
        add("/schedule/*.yaml", "settings")    # per-account scheduling rules
        add("/library/assets", "permanent")    # hashed, license-chained assets
        # Everything that could post twice or leak access stays on the computer that publishes.
        for d in ("accounts", "ledger", "policy", "schedule", "PAUSE"):
            add(f"/{d}", "local")
    add(f"/{edits}", "permanent")
    add(f"/{captions}", "permanent")
    add(f"/{work}/transcripts", "permanent")   # costly to rebuild
    add(f"/{gfx}", "permanent")
    add(f"/{music}", "permanent")
    add(f"/{exports}", "permanent")
    # Rebuildable, private or publisher-only.
    add(f"/{publish}", "local")
    add(f"/{work}", "local")
    add("/PROGRESS.md", "local")
    return r


def zone_of(rel_path: str, rule_list: Iterable[Rule], *, ci: bool | None = None) -> str:
    """The zone of a project-relative path: the first matching rule's zone, else "local". Case-insensitive on macOS and Windows."""
    p, ci = _norm(rel_path), CASE_INSENSITIVE if ci is None else ci
    for rule in rule_list:
        if _regex(rule.glob, ci).match(p):
            return rule.zone
    return "local"


def stignore(rule_list: Iterable[Rule]) -> str:
    """The `.stignore` for these rules, in rule order. Syncthing's first match wins, `!` includes, a pattern also covers
    everything beneath it, and Syncthing still walks an ignored folder when a `!` pattern could match something inside it
    (that is why `/.ave` can be ignored after `!/.ave/log/*.jsonl`). The closing `*` makes "unmatched means local" true for
    Syncthing as well. Layout names arrive already escaped by `rules()`.

    Syncthing's own temp files and `*.sync-conflict-*` are deliberately not ignored: the conflict watcher must see them."""
    lines = ["# generated by AVE from .ave/project.json: do not edit, it is rewritten on every computer"]
    for x in rule_list:
        g = x.glob
        if g[:1] in ("!", "#"):               # a leading ! or # would be read as syntax
            g = "\\" + g
        lines.append(("(?d)" if x.deletable else "") + ("!" if x.zone != "local" else "") + g)
    lines.append("*")
    return "\n".join(lines) + "\n"


def _name_re(globs: Iterable[str]) -> re.Pattern[str]:
    return re.compile("^(?:" + "|".join(re.escape(g).replace(r"\*", ".*").replace(r"\?", ".") for g in globs) + ")$", re.I)


_CRED_LOCAL_RE = _name_re(CREDENTIAL_LOCAL_GLOBS)
_CRED_LATE_RE = _name_re(CREDENTIAL_LATE_GLOBS)
_CRED_WARN_RE = _name_re(CREDENTIAL_WARN_GLOBS)


def _literal_prefix(glob: str) -> str | None:
    """The folder path a rooted glob is anchored at, up to its first wildcard segment ("/a/b/*.jsonl" -> "a/b"). None when unrooted."""
    if not glob.startswith("/"):
        return None
    out = []
    for seg in glob.strip("/").split("/"):
        if re.search(r"(?<!\\)[*?]", seg):
            break
        out.append(_unescape(seg))
    return "/".join(out)


def credential_warnings(root: Path, rule_list: Iterable[Rule]) -> list[str]:
    """Project-relative paths of credential-like files for the doctor. Unambiguous names (CREDENTIAL_LOCAL_GLOBS) are reported
    wherever they sit outside a private folder, even though the rules keep them off the network. Broad names are reported only
    when non-media and in a syncing zone. Private folders are not walked unless a syncing rule lives inside them."""
    root, rl, ci = Path(root), list(rule_list), CASE_INSENSITIVE
    prefixes = [_literal_prefix(r.glob) for r in rl if r.zone != "local"]
    walk_all = None in prefixes
    anchors = [p for p in prefixes if p]
    hits: list[str] = []
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for p in entries:
            rel = p.relative_to(root).as_posix()
            z = zone_of(rel, rl, ci=ci)
            if p.is_dir() and not p.is_symlink():
                if _CRED_LOCAL_RE.match(p.name) or (_CRED_LATE_RE.match(p.name) and z == "local"):
                    hits.append(rel + "/")         # a credential rule matches this folder, so everything inside it stays private
                    continue
                low = rel.lower() if ci else rel
                if z == "local" and not walk_all and not any(
                        (a.lower() if ci else a) == low or (a.lower() if ci else a).startswith(low + "/") for a in anchors):
                    continue                       # a private folder with nothing syncing inside it
                stack.append(p)
            elif _CRED_LOCAL_RE.match(p.name) or (_CRED_LATE_RE.match(p.name) and z == "local") or (
                    z != "local" and _CRED_WARN_RE.match(p.name) and Path(p.name).suffix.lower() not in _MEDIA_EXTS):
                hits.append(rel)
    return sorted(hits)


def _this_device_id() -> str | None:
    """This computer's device id, read without creating anything."""
    import json
    from .model import device_file
    try:
        meta = json.loads(device_file().read_text())
        return meta["device_id"] if isinstance(meta, dict) and isinstance(meta.get("device_id"), str) else None
    except (OSError, ValueError, KeyError):
        return None


def project_layout(root: Path, meta: dict) -> Any:
    """The layout the writers use: episode.json's when the folder has one (`episode.layout_spec_of`, exactly what Episode
    resolves), else project.json's (a studio project, or a member whose episode.json has not arrived yet)."""
    from ..episode import _NO_EPISODE, _UNREADABLE, layout_spec_of, resolve_layout
    spec = layout_spec_of(root)
    if spec is _NO_EPISODE or spec is _UNREADABLE:
        return meta.get("layout")
    try:
        resolve_layout(spec)
    except SystemExit:                       # e.g. {"layout": "nope"} from another member: keep project.json's layout
        return meta.get("layout")
    return spec


@dataclass(frozen=True)
class Policy:
    """What decides a project's sync rules on one computer: the layout, whether it is a studio, every member, and this
    computer's own member entry (None when it is not listed, which gets the "proxies" rules)."""
    layout: dict
    studio: bool
    members: list
    me: dict | None


def project_policy(root: Path, device_id: str | None = None) -> Policy:
    """Read `.ave/project.json` and the writers' layout (`project_layout`) once. project.json's `kind` decides the studio
    rules (so a member who has only the invitation's project.json gets them before studio.json arrives); a project.json
    written before kind was recorded falls back to whether the folder has a studio.json. `project_rules` and the Sources
    view's words (where.py) both read this, so they cannot disagree."""
    import json

    from ..episode import resolve_layout
    root = Path(root)
    meta = json.loads((root / ".ave" / "project.json").read_text())
    if not isinstance(meta, dict):
        raise ValueError("project.json must hold a JSON object")
    device_id = device_id or _this_device_id()
    members = [m for m in meta.get("members") or [] if isinstance(m, dict)] if isinstance(meta.get("members"), list) else []
    me = next((m for m in members if device_id and device_id in (m.get("devices") or [])), None)
    kind = meta.get("kind")
    studio = kind == "studio" if kind in ("episode", "studio") else (root / "studio.json").is_file()
    try:
        layout = resolve_layout(project_layout(root, meta))
    except SystemExit as exc:                # resolve_layout's CLI-style error must never end a sync thread
        raise ValueError(f"{root}: {exc}") from None
    return Policy(layout=layout, studio=studio, members=members, me=me)


def project_rules(root: Path, device_id: str | None = None) -> list[Rule]:
    """The rules for the project at `root` on this computer (`project_policy`). A device that is not listed gets the safest
    media policy, "proxies"."""
    pol = project_policy(root, device_id)
    return rules(pol.layout, pol.me or {"media": "proxies"}, studio=pol.studio)
