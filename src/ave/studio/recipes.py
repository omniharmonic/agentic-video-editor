"""Recipes (spec 9): an ordered list of steps, each `mechanical` (a command), `agent` (judgment: a checklist and the skill to read) or `human`
(a checkpoint). YAML in `config/studio/recipes/`, overridable per studio in `<studio>/recipes/` (same name wins).

    load_all(sp) / load(sp, name)      parsed recipes (`sp=None`: the shipped ones only)
    validate(recipe, skills_dir=...)   problems; empty = a well-formed recipe. Safety rules live here: a human-only command (approve, going live,
                                       resume, `--send`, a named stock pick, a spend) may only appear in a `human` step, never in a mechanical one.
    check(studio, name, args)          validates the inputs, evaluates every step's `done_when` against what is ON DISK (state is derived from artifacts,
                                       AGENTS.md rule 2), and names the next step. It never skips a human step: a human step is done only when its
                                       artifact exists, and the answer says `needs_human` while it is the next one.

`done_when` checks (all keys optional, all must hold; every path is relative to the studio folder and may use {placeholders}):
    {file: "work/content/writing.json"}                    the file exists
    {glob: "posts/*.json", min: 1}                         at least `min` files match
    {posts: {stage: "active", min: 4}}                     at least `min` posts in that stage (and, optionally, `origin: blog`, `has_media: true`)
    {variants: {status: [in_review, approved], min: 1}}    at least `min` variants in one of those DERIVED statuses
    {library: {source: "render", min: 1}}                  at least `min` library assets from that source type
    {library: {source: "render", covered: true, min: 1}}   ... whose license closure is covered AND human-attested (`library.coverage`)
    {episode_file: "work/transcript.json"}                 a file inside the episode named by the {episode} argument
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .. import resources

KINDS = ("mechanical", "agent", "human")
PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
# command fragments that are human acts (spec 12): they may only be shown to the human in a `human` step
HUMAN_ONLY = (r"\bstudio approve\b", r"\bpublish approve\b", r"\baccount live\b", r"\baccount auth\b", r"\bstudio resume\b", r"\bbroll pick\b",
              r"\bbroll generate\b", r"--send\b", r"\bschedule apply\b", r"\bstudio resolve\b", r"\bpublish run\b.*--yes", r"\bsequence approve\b",
              r"\blibrary attest\b", r"\bsend (enable|disable)\b", r"\bstudio ui\b", r"\bave ui\b", r"\bave desktop\b", r"\btrim set\b", r"\blearn taste apply\b")
BUILTIN_PLACEHOLDERS = {"studio"}


class RecipeError(ValueError):
    pass


def shipped_dir() -> Path:
    return resources.home() / "config" / "studio" / "recipes"


def _read(p: Path) -> dict:
    try:
        data = yaml.safe_load(p.read_text())
    except (OSError, yaml.YAMLError) as e:
        raise RecipeError(f"{p.name}: cannot read ({e})") from e
    if not isinstance(data, dict):
        raise RecipeError(f"{p.name}: must be a mapping")
    data.setdefault("name", p.stem)
    return data


def load_all(sp=None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    dirs = [shipped_dir()] + ([sp.recipes_dir] if sp is not None else [])
    for d in dirs:
        for p in sorted(d.glob("*.yaml")) if d.is_dir() else []:
            r = _read(p)
            r["_file"] = str(p)
            out[r["name"]] = r
    return out


def load(sp, name: str) -> dict:
    allr = load_all(sp)
    if name not in allr:
        raise RecipeError(f"no recipe {name!r}; have: {', '.join(sorted(allr)) or 'none'}")
    return allr[name]


# ------------------------------------------------------------------------------------------------ validation

def _skills() -> set[str]:
    d = resources.home() / "skills"
    return {p.name for p in d.iterdir() if (p / "SKILL.md").is_file()} if d.is_dir() else set()


def _check_done_when(step_id: str, dw: Any) -> list[str]:
    if dw is None:
        return []
    if not isinstance(dw, dict):
        return [f"step {step_id}: done_when must be a mapping"]
    known = {"file", "glob", "min", "posts", "variants", "library", "episode_file"}
    bad = [k for k in dw if k not in known]
    p = [f"step {step_id}: unknown done_when key {k!r} (have: {', '.join(sorted(known))})" for k in bad]
    if "glob" in dw and not isinstance(dw["glob"], str):
        p.append(f"step {step_id}: done_when.glob must be a string")
    for key in ("posts", "variants", "library"):
        if key in dw and not (isinstance(dw[key], dict) and isinstance(dw[key].get("min", 1), int)):
            p.append(f"step {step_id}: done_when.{key} must be a mapping with an integer min")
    return p


def validate(recipe: dict, *, skills: set[str] | None = None) -> list[str]:
    p: list[str] = []
    name = recipe.get("name")
    if not isinstance(name, str) or not re.match(r"^[a-z][a-z0-9-]{1,40}$", name or ""):
        p.append("name must be lowercase words joined by hyphens")
    for k in ("title", "summary"):
        if not isinstance(recipe.get(k), str) or not recipe[k].strip():
            p.append(f"{k} is required")
    inputs = recipe.get("inputs") or []
    names: set[str] = set()
    for i in inputs:
        if not isinstance(i, dict) or not isinstance(i.get("name"), str):
            p.append("each input needs a name")
            continue
        if i["name"] in names:
            p.append(f"input {i['name']!r} is declared twice")
        names.add(i["name"])
    steps = recipe.get("steps")
    if not isinstance(steps, list) or not steps:
        return p + ["steps must be a non-empty list"]
    skills = skills if skills is not None else _skills()
    ids: set[str] = set()
    kinds = []
    for n, s in enumerate(steps):
        sid = s.get("id") if isinstance(s, dict) else None
        if not isinstance(s, dict) or not isinstance(sid, str) or not re.match(r"^[a-z][a-z0-9_-]*$", sid):
            p.append(f"step {n + 1}: needs an id (lowercase)")
            continue
        if sid in ids:
            p.append(f"step id {sid!r} is used twice")
        ids.add(sid)
        kind = s.get("kind")
        kinds.append(kind)
        if kind not in KINDS:
            p.append(f"step {sid}: kind must be one of {', '.join(KINDS)}")
            continue
        if not isinstance(s.get("title"), str) or not s["title"].strip():
            p.append(f"step {sid}: title is required")
        cmd = s.get("command")
        if kind == "mechanical":
            if not isinstance(cmd, str) or not cmd.startswith("ave "):
                p.append(f"step {sid}: a mechanical step names one `ave ...` command")
            if not s.get("done_when"):
                p.append(f"step {sid}: a mechanical step needs done_when (state is derived from what exists on disk)")
        if kind == "agent":
            sk = s.get("skill")
            if not isinstance(sk, str) or sk not in skills:
                p.append(f"step {sid}: skill {sk!r} is not a skill in skills/ (have: {', '.join(sorted(s_ for s_ in skills if s_.startswith('ave-studio')))})")
            if not (isinstance(s.get("checklist"), list) and s["checklist"] and all(isinstance(c, str) and c.strip() for c in s["checklist"])):
                p.append(f"step {sid}: an agent step needs a non-empty checklist of strings")
            if not s.get("done_when"):
                p.append(f"step {sid}: an agent step needs done_when (what exists once the judgment is made)")
        if kind == "human":
            if not isinstance(s.get("prompt"), str) or not s["prompt"].strip():
                p.append(f"step {sid}: a human step needs a prompt telling the human what to look at and decide")
            if not s.get("done_when"):
                p.append(f"step {sid}: a human step needs done_when (the artifact their act leaves behind)")
        if kind != "human" and isinstance(cmd, str):
            for pat in HUMAN_ONLY:
                if re.search(pat, cmd):
                    p.append(f"step {sid}: {cmd!r} is a human act; it may only appear in a `human` step")
                    break
        for field in ("command", "prompt"):
            for ph in PLACEHOLDER.findall(str(s.get(field) or "")):
                if ph not in names and ph not in BUILTIN_PLACEHOLDERS:
                    p.append(f"step {sid}: {{{ph}}} is not a declared input")
        p += _check_done_when(sid, s.get("done_when"))
    if "human" not in kinds:
        p.append("every recipe ends in a human decision: add a human step (review or approval)")
    for i, st in enumerate(steps):
        if isinstance(st, dict) and re.search(r"\bstudio (tick|run)\b", str(st.get("command") or "")) and "human" not in kinds[:i]:
            p.append(f"step {st.get('id')}: a tick/run step must come after a human step (approval)")
    return p


def validate_all(sp=None) -> dict[str, list[str]]:
    out = {}
    for name, r in load_all(sp).items():
        out[name] = validate(r)
    return out


# ------------------------------------------------------------------------------------------------ check

def _fill(text: str, args: dict, sp) -> str:
    vals = {**args, "studio": str(sp.root)}
    return PLACEHOLDER.sub(lambda m: str(vals.get(m.group(1), m.group(0))), text)


def parse_args(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise RecipeError(f"--args takes key=value pairs (got {p!r})")
        k, v = p.split("=", 1)
        out[k.strip()] = v
    return out


def _evaluate(studio, step: dict, args: dict) -> tuple[bool, list[str]]:
    """(done, evidence). A step without done_when is never done by itself."""
    dw = step.get("done_when")
    if not dw:
        return False, ["no done_when"]
    sp = studio.sp
    ev: list[str] = []
    ok = True
    if "file" in dw:
        f = sp.root / _fill(dw["file"], args, sp)
        here = f.exists()
        ok &= here
        ev.append(f"{'found' if here else 'missing'}: {f.relative_to(sp.root)}")
    if "episode_file" in dw:
        epn = args.get("episode")
        f = (Path(epn) / _fill(dw["episode_file"], args, sp)) if epn else None
        here = bool(f and f.exists())
        ok &= here
        ev.append(f"{'found' if here else 'missing'}: episode {dw['episode_file']}")
    if "glob" in dw:
        n = len(list(sp.root.glob(_fill(dw["glob"], args, sp))))
        need = int(dw.get("min", 1))
        ok &= n >= need
        ev.append(f"{n} file(s) match {dw['glob']} (need {need})")
    if "posts" in dw:
        spec = dw["posts"]
        from . import store
        posts = store.list_posts(sp, stage=spec.get("stage"))
        if spec.get("origin"):
            posts = [p for p in posts if ((p.get("idea") or {}).get("origin") or {}).get("type") == spec["origin"]]
        if spec.get("has_media"):
            posts = [p for p in posts if p.get("media")]
        need = int(spec.get("min", 1))
        ok &= len(posts) >= need
        ev.append(f"{len(posts)} post(s) matching (need {need})")
    if "variants" in dw:
        spec = dw["variants"]
        want = spec.get("status")
        want = [want] if isinstance(want, str) else list(want or [])
        n = sum(1 for p in studio.posts() for v in p["variants"] if not want or v["status"] in want)
        need = int(spec.get("min", 1))
        ok &= n >= need
        ev.append(f"{n} variant(s) in {'/'.join(want) or 'any status'} (need {need})")
    if "library" in dw:
        spec = dw["library"]
        from . import store
        rows = [a for a in store.read_assets(sp).values() if not spec.get("source") or (a.get("source") or {}).get("type") == spec["source"]]
        if spec.get("covered"):
            from .. import library
            rows = [a for a in rows if library.coverage(sp, [a["id"]])["ok"]]
        n = len(rows)
        need = int(spec.get("min", 1))
        ok &= n >= need
        ev.append(f"{n} library asset(s) from {spec.get('source') or 'any source'}{' with a covered, attested license' if spec.get('covered') else ''} (need {need})")
    return ok, ev


def check(studio, name: str, args: dict[str, str] | None = None) -> dict:
    recipe = load(studio.sp, name)
    args = dict(args or {})
    problems = [f"recipe: {m}" for m in validate(recipe)]
    declared = {i["name"]: i for i in recipe.get("inputs") or []}
    for k in args:
        if k not in declared:
            problems.append(f"unknown argument {k!r}; this recipe takes: {', '.join(declared) or 'none'}")
    for k, i in declared.items():
        if k not in args and "default" in i:
            args[k] = str(i["default"])
        if i.get("required") and k not in args:
            problems.append(f"missing required argument {k}: {i.get('description', '')}".strip())
        if k in args and i.get("path") and not Path(args[k]).expanduser().exists():
            problems.append(f"{k}: {args[k]} does not exist")
    rows, nxt, seen_pending = [], None, False
    for s in recipe["steps"]:
        done, evidence = _evaluate(studio, s, args)
        state = "done" if done else ("next" if not seen_pending else "pending")
        if not done and not seen_pending:
            seen_pending = True
            nxt = s
        row = {"id": s["id"], "kind": s["kind"], "title": s["title"], "state": state, "evidence": evidence}
        if done and seen_pending:
            row["out_of_order"] = True          # its artifact exists although an earlier step is not done: a human step is never skipped by this
        if s["kind"] == "mechanical":
            row["command"] = _fill(s["command"], args, studio.sp)
        if s["kind"] == "human" and s.get("command"):
            row["command"] = _fill(s["command"], args, studio.sp)
        rows.append(row)
    out: dict[str, Any] = {"recipe": name, "title": recipe["title"], "args": args, "problems": problems, "steps": rows}
    if nxt is None:
        out["next"] = None
        out["complete"] = True
    else:
        n = {"id": nxt["id"], "kind": nxt["kind"], "title": nxt["title"]}
        if nxt["kind"] == "mechanical":
            n["command"] = _fill(nxt["command"], args, studio.sp)
        if nxt["kind"] == "agent":
            n.update(skill=nxt["skill"], checklist=nxt["checklist"])
        if nxt["kind"] == "human":
            n["prompt"] = _fill(nxt["prompt"], args, studio.sp)
            if nxt.get("command"):
                n["run"] = "! " + _fill(nxt["command"], args, studio.sp)
        out["next"] = n
        out["complete"] = False
    out["needs_human"] = bool(nxt and nxt["kind"] == "human")
    return out
