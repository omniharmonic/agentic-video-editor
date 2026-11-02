"""Library group: add, list, show, search, tag, scan, reindex, verify. Bytes enter by COPY (never move) with a license trail or not at all."""

from __future__ import annotations

import getpass
from pathlib import Path

from ... import library
from ...publish import safety
from .. import store
from . import Blocked, add_dir, csv, guarded, human_gate, out, project, studio


def _entry_from_args(a) -> dict | None:
    if a.source == "own":
        return None                       # add_file writes the standard own-footage entry
    need = {"license": a.license, "license_url": a.license_url, "license_text": a.license_text}
    missing = [f"--{k.replace('_', '-')}" for k, v in need.items() if not v]
    if missing:
        raise ValueError(f"a {a.source} file needs its license on record: pass {', '.join(missing)} (and --url, --attribution when the license asks for it)")
    entry = {**need, "url": a.url or "", "source": a.provider or a.source}
    if a.attribution:
        entry["attribution"] = {"required": True, "text": a.attribution}
    return entry


@guarded
def cmd_add(a) -> int:
    sp = project(a)
    entry = _entry_from_args(a)
    added, existing, failed = [], [], []
    for raw in a.paths:
        p = Path(raw).expanduser()
        try:
            rec, created = library.add_file_result(sp, p, source={"type": a.source, "provider": a.provider, "url": a.url}, license_entry=entry, by=a.by,
                                                   tags=csv(a.tags), alt=a.alt or "", attested=False)
            (added if created else existing).append({"id": rec["id"], "file": rec["file"], "kind": rec["kind"], "sha256": rec["sha256"], "w": rec["w"],
                                                     "h": rec["h"], "faces": (rec.get("people") or {}).get("faces")})
        except (ValueError, FileNotFoundError, OSError) as e:
            failed.append({"path": str(p), "error": str(e)})
    note = [f"{x['id']}: {x['faces']} face(s) detected and no release recorded; get a release before posting identifiable people"
            for x in added if x.get("faces")]
    pending = [x["id"] for x in added]
    out({"ok": not failed, "added": added, "already_in_library": existing, "failed": failed, "warnings": note,
         "license": {"attested": False, "pending_human_attestation": pending,
                     "note": "The license was RECORDED as claimed by " + str(a.by) + ", not attested: these files do not count toward a post's license "
                             "closure (and so cannot be queued or sent) until a human runs `ave studio library attest`."} if pending else None})
    return 2 if failed else 0


@guarded
def cmd_attest(a) -> int:
    """HUMAN. Look at each pending license claim, then attest it. Without this, an `--source own|stock|import` add (or an adopted episode file)
    never counts toward the license closure."""
    human_gate(a, "attesting a license claim", "ave studio library attest" + (" " + " ".join(a.ids) if a.ids else " --all"))
    if not a.ids and not a.all:
        raise ValueError("name the assets to attest, or pass --all")
    sp = project(a)
    pending = library.unattested(sp)
    chosen = [r for r in pending if not a.ids or r["asset"] in a.ids]
    unknown = [i for i in (a.ids or []) if i not in {r["asset"] for r in pending}]
    if unknown:
        raise Blocked(f"nothing to attest for: {', '.join(unknown)} (unknown asset, or its license is already attested)")
    if not chosen:
        out({"ok": True, "attested": [], "note": "no license claim is waiting for a human"})
        return 0
    out({"claims": chosen, "look": "Open each file and read the license text. You are stating these may be posted publicly."})
    typed = input(f"Type 'attest' to stand behind the {len(chosen)} license claim(s) above: ").strip().lower()
    if typed != "attest":
        raise Blocked("nothing attested")
    who = a.by or getpass.getuser()
    done = [library.attest_license(sp, r["asset"], who)["asset"] for r in chosen]
    out({"ok": True, "attested": done, "by": who})
    return 0


@guarded
def cmd_list(a) -> int:
    s = studio(a, adapters=False)
    res = s.library(q=None, kind=a.kind, tag=a.tag, unused=a.unused, limit=a.limit, license=a.license, source=a.source)
    out({"ok": True, **res})
    return 0


@guarded
def cmd_show(a) -> int:
    s = studio(a, adapters=False)
    out({"ok": True, **s.library_asset(a.id)})
    return 0


@guarded
def cmd_search(a) -> int:
    s = studio(a, adapters=False)
    res = s.library(q=a.query, kind=a.kind, tag=a.tag, unused=False, limit=a.limit)
    out({"ok": True, "query": a.query, **res})
    return 0


@guarded
def cmd_tag(a) -> int:
    sp = project(a)
    rec = library.get(sp, a.id)
    tags = list(rec.get("tags") or [])
    for t in csv(a.add):
        if t not in tags:
            tags.append(t)
    tags = [t for t in tags if t not in csv(a.remove)]
    fields = {"tags": tags}
    if a.alt is not None:
        fields["alt"] = a.alt
    new = library.update_asset(sp, a.id, **fields)
    out({"ok": True, "id": a.id, "tags": new.get("tags"), "alt": new.get("alt")})
    return 0


@guarded
def cmd_scan(a) -> int:
    """Files dropped in inbox/ that are not in the library yet (read-only: adding them is `library add`)."""
    sp = project(a)
    known = {r["sha256"] for r in store.read_assets(sp).values()}
    found = []
    for d in sp.raw_dirs:
        for f in sorted(d.rglob("*")) if d.is_dir() else []:
            if f.is_file() and not f.name.startswith(".") and f.suffix.lower() in library.registry.KIND_BY_EXT:
                found.append({"path": str(f), "sha256": safety.sha256_file(f)})
    new = [f for f in found if f["sha256"] not in known]
    out({"ok": True, "inbox": [str(d) for d in sp.raw_dirs], "new": new, "already_in_library": len(found) - len(new),
         "next": "ave studio library add <paths> --source own --by NAME" if new else "nothing new"})
    return 0


@guarded
def cmd_reindex(a) -> int:
    sp = project(a)
    out({"ok": True, "indexed": library.reindex(sp)})
    return 0


@guarded
def cmd_verify(a) -> int:
    sp = project(a)
    res = library.verify(sp)
    out(res)
    return 0 if res["ok"] else 2


def add(ss) -> None:
    lib = ss.add_parser("library", help="the asset library: add, list, show, search, tag, scan, reindex, verify")
    sub = lib.add_subparsers(dest="library_cmd", required=True)
    p = sub.add_parser("add", help="copy files into the library by sha, with a license trail")
    p.add_argument("paths", nargs="+")
    p.add_argument("--source", required=True, choices=["own", "stock", "import"])
    p.add_argument("--by", required=True, help="who adds it (recorded on the asset and in the license log)")
    p.add_argument("--tags")
    p.add_argument("--alt")
    p.add_argument("--provider")
    p.add_argument("--url")
    p.add_argument("--license")
    p.add_argument("--license-url")
    p.add_argument("--license-text")
    p.add_argument("--attribution", help="attribution text the license requires")
    add_dir(p)
    p.set_defaults(fn=cmd_add)
    p = sub.add_parser("attest", help="HUMAN: stand behind the license claims made by `library add` / `adopt` (they do not count until you do)")
    p.add_argument("ids", nargs="*", metavar="ASSET")
    p.add_argument("--all", action="store_true", help="every pending claim")
    p.add_argument("--by")
    add_dir(p)
    p.set_defaults(fn=cmd_attest)
    p = sub.add_parser("list")
    p.add_argument("--kind", choices=["image", "video", "audio", "document"])
    p.add_argument("--tag")
    p.add_argument("--source")
    p.add_argument("--license", choices=["ok", "missing"])
    p.add_argument("--unused", action="store_true")
    p.add_argument("--limit", type=int, default=50)
    add_dir(p)
    p.set_defaults(fn=cmd_list)
    p = sub.add_parser("show")
    p.add_argument("id")
    add_dir(p)
    p.set_defaults(fn=cmd_show)
    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("--kind")
    p.add_argument("--tag")
    p.add_argument("--limit", type=int, default=50)
    add_dir(p)
    p.set_defaults(fn=cmd_search)
    p = sub.add_parser("tag", help="add or remove tags, set alt text")
    p.add_argument("id")
    p.add_argument("--add")
    p.add_argument("--remove")
    p.add_argument("--alt")
    add_dir(p)
    p.set_defaults(fn=cmd_tag)
    p = sub.add_parser("scan", help="list files in inbox/ that are not in the library yet (read-only)")
    add_dir(p)
    p.set_defaults(fn=cmd_scan)
    p = sub.add_parser("reindex", help="rebuild the derived search index")
    add_dir(p)
    p.set_defaults(fn=cmd_reindex)
    p = sub.add_parser("verify", help="hashes, license chain and license coverage of every asset")
    add_dir(p)
    p.set_defaults(fn=cmd_verify)
