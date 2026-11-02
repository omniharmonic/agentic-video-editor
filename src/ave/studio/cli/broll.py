"""B-roll group: queries, search, show, pick (HUMAN-named), generate (HUMAN, spend).

`search` writes candidates with local thumbnails and never downloads a full-size file. `pick` downloads: a NAMED human approves every download
(spec 5.3, 12 #3), so it needs a human terminal and `--by NAME`. `generate` is a spend (spec 5.5): it is not enabled in this build; the command
exists so the refusal is explicit and a human is told exactly what would be needed.
"""

from __future__ import annotations

from ... import broll
from ...episode import Episode, read_json
from . import Blocked, add_dir, csv, guarded, human_gate, out, project, studio


def transcript_range(episode: str, rng: str) -> str:
    """The words spoken between START and END seconds of episode time (from work/transcript.json)."""
    try:
        t0, t1 = (float(x) for x in rng.split("-", 1))
    except ValueError:
        raise ValueError("--range is START-END in seconds, e.g. 812-860") from None
    ep = Episode(episode)
    ep.require()
    tr = ep.artifact("transcript.json")
    if not tr.exists():
        raise FileNotFoundError(f"{ep.root} has no transcript yet (`ave transcribe`)")
    words = [w["w"] for w in read_json(tr).get("words", []) if w["start"] >= t0 and w["end"] <= t1]
    if not words:
        raise ValueError(f"no words between {t0:g} s and {t1:g} s")
    return " ".join(words)


@guarded
def cmd_queries(a) -> int:
    sp = project(a)
    texts = list(a.text or [])
    if a.episode:
        if not a.range:
            raise ValueError("--episode needs --range START-END (seconds of episode time)")
        texts.append(transcript_range(a.episode, a.range))
    if a.post:
        res = broll.queries.for_post(sp, a.post, kind=a.kind, orientation=a.orientation, max_queries=a.max)
    elif texts:
        res = broll.queries.generate(texts, kind=a.kind, orientation=a.orientation, max_queries=a.max, sp=sp)
    else:
        raise ValueError("give --post P, --text TEXT, or --episode E --range A-B")
    out({"ok": True, **res})
    return 0


@guarded
def cmd_search(a) -> int:
    s = studio(a, adapters=False)
    doc = s.broll_search(query=a.query, post=a.post, providers=csv(a.providers) or None, kind=a.kind, orientation=a.orientation, per_page=a.per_page)
    doc = doc.get("search", doc)
    out({"ok": True, "search": doc["id"], "query": doc["query"], "errors": doc.get("errors", []),
         "candidates": [{"key": f"{c['provider']}:{c['asset_id']}", "kind": c["kind"], "title": c.get("title"), "w": c["w"], "h": c["h"],
                         "duration": c.get("duration"), "thumb": c.get("thumb"), "author": c.get("author"),
                         "attribution_required": c.get("attribution_required"), "critic": (c.get("critic") or {}).get("verdict"),
                         "problems": (c.get("critic") or {}).get("problems", [])} for c in doc["candidates"]],
         "next": f"Look at the thumbnails (work/broll/{doc['id']}/), then ask the human to pick: ! ave studio broll pick {doc['id']} <key> --by NAME"})
    return 0


@guarded
def cmd_show(a) -> int:
    s = studio(a, adapters=False)
    out({"ok": True, **s.broll(a.search)})
    return 0


@guarded
def cmd_pick(a) -> int:
    human_gate(a, "naming a stock download", f"ave studio broll pick {a.search} {a.candidate} --by {a.by}")
    s = studio(a, adapters=False)
    res = s.broll_pick(a.search, a.candidate, by=a.by)
    out({"ok": True, **res})
    return 0


@guarded
def cmd_generate(a) -> int:
    human_gate(a, "a generative media call (a spend)", f"ave studio broll generate --provider {a.provider} --prompt {a.prompt!r} --max-cost {a.max_cost} --by {a.by}")
    raise Blocked("generative media is not enabled in this studio: `broll.generative.providers` is empty by default (spec 5.5) and no provider "
                  "client is built yet. Nothing was called and nothing was spent.")


def add(ss) -> None:
    b = ss.add_parser("broll", help="licensed B-roll: queries, search, show, pick (human), generate (human)")
    sub = b.add_subparsers(dest="broll_cmd", required=True)
    p = sub.add_parser("queries", help="a mechanical draft of search queries (the agent edits it)")
    p.add_argument("--post")
    p.add_argument("--text", action="append")
    p.add_argument("--episode")
    p.add_argument("--range", help="START-END seconds of episode time, e.g. 812-860")
    p.add_argument("--kind", choices=["photo", "video"])
    p.add_argument("--orientation")
    p.add_argument("--max", type=int)
    add_dir(p)
    p.set_defaults(fn=cmd_queries)
    p = sub.add_parser("search", help="search providers (no full-size download)")
    p.add_argument("--query", required=True)
    p.add_argument("--post")
    p.add_argument("--providers", help="comma list: pexels,pixabay,unsplash,coverr,local")
    p.add_argument("--kind", choices=["photo", "video"])
    p.add_argument("--orientation")
    p.add_argument("--per-page", type=int, default=15)
    add_dir(p)
    p.set_defaults(fn=cmd_search)
    p = sub.add_parser("show")
    p.add_argument("search")
    add_dir(p)
    p.set_defaults(fn=cmd_show)
    p = sub.add_parser("pick", help="HUMAN: download one candidate (named approver; logs its license)")
    p.add_argument("search")
    p.add_argument("candidate", help="provider:id from `broll search`")
    p.add_argument("--by", required=True, help="the human's name")
    add_dir(p)
    p.set_defaults(fn=cmd_pick)
    p = sub.add_parser("generate", help="HUMAN: a paid generative call (not enabled in this build)")
    p.add_argument("--provider", required=True)
    p.add_argument("--prompt", required=True)
    p.add_argument("--max-cost", type=float, required=True, help="USD cap")
    p.add_argument("--by", required=True)
    add_dir(p)
    p.set_defaults(fn=cmd_generate)
