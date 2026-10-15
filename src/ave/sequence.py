"""Opening and closing sequences: plan, build, preview, approve, and the isolated hooks `ave final` and the Resolve handoff call.

WHY it is built this way (read before changing):
  * A sequence is rendered as ONE EditDoc through the same compiler as the body (`compile.render`): a flat brand plate (a token
    colour, never footage) as the video source, the motion-kit cards as alpha overlays, the cut's own room tone as the audio under
    the cards, music/stings as licence-gated music items (the compiler ducks them under speech), and, for a cold open, the hook's own
    segments (sliced from an approved clip or a chosen quote) in front. So frame counts, fps, canvas, audio rate and loudness
    verification are the compiler's, identical to the body's.
  * The programme (`assemble`) is the body's picture and the sequences' pictures joined by STREAM COPY (the body is not re-encoded), the
    audio is joined in float with a short equal-power fade-out / fade-in pair at each join (no click, no step), then loudness-normalised
    once to the preset and measured on the encoded file. The fades meet at the join; they do not overlap (a sequence has no audio
    beyond its own ends), so this is a fade through the room tone rather than an overlapped crossfade.
  * Everything is data: policy `config/sequences.default.yaml` (per-show overridable), text and cold-open pick in `episode.json`,
    asset credits from the licence log. `plan` writes `work/sequence.json` (what will be built, with the SOURCE of every piece of text,
    licences, warnings and the items a human must approve). The human runs `ave sequence approve`; `ave final` builds sequences only
    for an approved plan whose content has not changed since.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import shutil
import subprocess
import time
from fractions import Fraction
from pathlib import Path

import numpy as np
import soundfile as sf
import yaml

from . import captions, compile as comp, config, editdoc, ffutil, motion, presets
from .assets import licenses
from .episode import Episode, read_json, write_json

DEFAULT_POLICY = config.REPO_ROOT / "config" / "sequences.default.yaml"
FINAL_POLICY = config.REPO_ROOT / "config" / "final.default.yaml"
SEQ_KIT = motion.KIT / "sequences"
SR = ffutil.SAMPLE_RATE
WHICH = ("opening", "closing")
MAX_COLUMN_PX = 15000            # Chromium's texture limit is 16384 px; the credits column must fit in one screenshot
TAIL_PROBE_DB = -40.0            # body head/tail level above which a join fade would soften speech


class SequenceError(ValueError):
    """The plan cannot be built, or the files are wrong. The message says what to change."""


# ====================================================================================================================
# POLICY
# ====================================================================================================================

def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def _check_policy(p: dict) -> dict:
    o, c = p["opening"], p["closing"]
    for key in ("title_card", "hosts"):
        s = o[key]
        if not 0 < s["min_s"] <= s["duration_s"] <= s["max_s"]:
            raise ValueError(f"sequences policy opening.{key}: need 0 < min_s <= duration_s <= max_s")
    if not 3 <= o["title_card"]["max_s"] <= 8:
        raise ValueError("sequences policy opening.title_card.max_s must be between 3 and 8 seconds")
    if c["end_card"]["hold_s"] <= 0 or c["tail_room_tone_s"] < 0:
        raise ValueError("sequences policy closing: end_card.hold_s must be > 0 and tail_room_tone_s >= 0")
    if c["credits"]["assets"] not in ("all", "required"):
        raise ValueError("sequences policy closing.credits.assets must be all or required")
    if c["end_card"]["layout"] not in ("end-screen", "centered"):
        raise ValueError("sequences policy closing.end_card.layout must be end-screen or centered")
    if not 0 <= p["audio"]["join_fade_s"] <= 1.0:
        raise ValueError("sequences policy audio.join_fade_s must be between 0 and 1 second")
    return p


def load_policy(path: Path | str | None = None) -> dict:
    base = yaml.safe_load(DEFAULT_POLICY.read_text())
    if path:
        base = _merge(base, yaml.safe_load(Path(path).read_text()) or {})
    return _check_policy(base)


def resolve_policy(ep: Episode, explicit: Path | str | None = None, home: Path | None = None) -> dict:
    """--policy > <ep>/policy/sequences.yaml > ~/.config/ave/sequences.yaml > default (each merged over the default)."""
    home = Path(home) if home else Path.home()
    if explicit:
        if not Path(explicit).is_file():
            raise FileNotFoundError(f"policy file not found: {explicit}")
        return load_policy(explicit)
    base = yaml.safe_load(DEFAULT_POLICY.read_text())
    for cand in (home / ".config" / "ave" / "sequences.yaml", ep.root / "policy" / "sequences.yaml"):   # later wins
        if cand.is_file():
            base = _merge(base, yaml.safe_load(cand.read_text()) or {})
    return _check_policy(base)


def final_policy(ep: Episode, home: Path | None = None) -> dict:
    """config/final.default.yaml merged under ~/.config/ave/final.yaml and <ep>/policy/final.yaml."""
    home = Path(home) if home else Path.home()
    base = yaml.safe_load(FINAL_POLICY.read_text())
    for cand in (home / ".config" / "ave" / "final.yaml", ep.root / "policy" / "final.yaml"):
        if cand.is_file():
            base = _merge(base, yaml.safe_load(cand.read_text()) or {})
    return base


def wanted(ep: Episode, which: str, override: bool | None = None, home: Path | None = None) -> bool:
    """Does `ave final` build this sequence? CLI override beats the final policy `sequences: {opening, closing}`."""
    if override is not None:
        return bool(override)
    return bool((final_policy(ep, home).get("sequences") or {}).get(which))


# ====================================================================================================================
# SMALL HELPERS
# ====================================================================================================================

def _fpsf(fps: float) -> float:
    return float(ffutil.fps_fraction(fps))


def _frames(seconds: float, fps: float) -> int:
    """Whole frames, rounded UP, at least one: a duration in a plan is always a multiple of the frame time."""
    return max(1, int(math.ceil(seconds * _fpsf(fps) - 1e-6)))


def _secs(frames: int, fps: float) -> float:
    return frames / _fpsf(fps)


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _text(value, source: str, approve: bool = False) -> dict:
    return {"value": value, "source": source, "approve": approve}


def _tag(name: str | None) -> str:
    return name or "main"


def plan_path(ep: Episode, name: str | None = None) -> Path:
    return ep.work / (f"sequence.{name}.json" if name else "sequence.json")


def approval_path(ep: Episode, name: str | None = None) -> Path:
    return ep.work / (f"sequence.{name}.approval.json" if name else "sequence.approval.json")


def seq_dir(ep: Episode, name: str | None = None) -> Path:
    return ep.work / "sequence" / _tag(name)


def _meta(ep: Episode) -> dict:
    return read_json(ep.meta_path) if ep.meta_path.exists() else {}


def _clip_text(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _word(w: dict) -> str:
    return str(w.get("w") or w.get("word") or w.get("text") or "")


def _tokens(s: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", s.lower().replace("’", "'"))


# ====================================================================================================================
# THE BASE CUT (spec for canvas / fps / room tone / assets)
# ====================================================================================================================

def base_doc(ep: Episode, name: str | None = None) -> tuple[str, dict]:
    for st in ("final", "rough", "assembly"):
        if editdoc.list_versions(ep, st, name):
            return st, editdoc.load_latest(ep, st, name)
    raise SystemExit(f"No final, rough or assembly edit{' ' + name if name else ''} yet for {ep.root.name}: sequences take their picture "
                     f"size, frame rate and room tone from the cut. Run: ave assemble {ep.root}")


# ====================================================================================================================
# IDENTITY: show, number, hosts, guests, links (episode.json) -> text with sources
# ====================================================================================================================

def identity(meta: dict, pol: dict) -> dict:
    show = meta.get("show") or {}
    title = str(meta.get("title") or "").strip()
    out: dict = {"warnings": []}
    name, number = show.get("name"), meta.get("number")
    m = re.match(r"^(?P<n>.*?)[\s#:\-–]*(?P<d>\d{1,4})$", title)
    if name:
        out["show"] = _text(str(name), "episode.json show.name")
    elif m and m["n"].strip():
        out["show"] = _text(m["n"].strip(), "derived from episode.json title (strip the trailing number)", approve=True)
    else:
        out["show"] = _text(title, "episode.json title (no show.name set)", approve=True)
    if number not in (None, ""):
        out["number"] = _text(str(number), "episode.json number")
    elif m:
        out["number"] = _text(m["d"], "derived from the trailing number of episode.json title", approve=True)
    else:
        out["number"] = _text("", "none: set episode.json number")
    et = meta.get("episode_title")
    out["title"] = _text(str(et), "episode.json episode_title") if et else _text("", "none: set episode.json episode_title")
    if not et:
        out["warnings"].append("no episode.json episode_title: the title card shows the show mark and episode number only")
    out["tagline"] = _text(str(show.get("tagline") or ""), "episode.json show.tagline")
    logo = show.get("logo")
    out["logo"] = _text(str(logo) if logo else "", "episode.json show.logo")

    def people(items, default_title, src):
        res = []
        for it in items or []:
            if isinstance(it, str):
                it = {"name": it}
            nm = str(it.get("name") or "").strip()
            if nm:
                res.append({"name": nm, "title": str(it.get("title") or default_title or "").strip()})
        return res

    dt = pol["opening"]["hosts"].get("default_title", "")
    if show.get("hosts"):
        out["hosts"] = _text(people(show["hosts"], dt, "show.hosts"), "episode.json show.hosts")
    else:
        sp = [{"name": str(v.get("name")).strip(), "title": dt} for v in (meta.get("speakers") or {}).values()
              if isinstance(v, dict) and v.get("name") and str(v["name"]).strip()]
        out["hosts"] = _text(sp, "episode.json speakers (names confirmed with the human; titles default to the policy's)", approve=bool(sp))
    out["guests"] = _text(people(meta.get("guests"), "", "guests"), "episode.json guests")
    links = []
    for it in show.get("links") or []:
        if isinstance(it, dict) and it.get("value"):
            links.append({"label": str(it.get("label") or ""), "value": str(it["value"])})
    out["links"] = _text(links, "episode.json show.links")
    return out


# ====================================================================================================================
# COLD OPEN: resolve the human's pick to an EditDoc slice
# ====================================================================================================================

def _whole_frames_slice(doc: dict, o0: float, o1: float, fps: float) -> dict:
    n = max(1, round((o1 - o0) * _fpsf(fps)))
    o1 = min(doc["duration"], o0 + n / _fpsf(fps))
    sl = editdoc.slice_doc(doc, o0, o1, stage="select")
    sl["duration"] = _secs(max(1, round((o1 - o0) * _fpsf(fps))), fps)
    for name in ("video", "dialogue"):
        if sl["tracks"][name]:
            sl["tracks"][name][-1]["o1"] = sl["duration"]
    if sl["tracks"]["dialogue"]:
        sl["tracks"]["dialogue"][0]["xfade_ms"] = 0
    return sl


def _find_clip(ep: Episode, clip_id: str) -> dict:
    from . import clips as clipmod
    data = clipmod.load_clips(ep)
    if not data:
        raise SequenceError("cold open names a clip but there is no work/clips.json yet (ave clips propose ... then ave clips build)")
    for c in data["clips"]:
        if c["id"] == clip_id:
            return c
    raise SequenceError(f"cold open clip {clip_id!r} is not in work/clips.json (ids: {', '.join(c['id'] for c in data['clips'][:12])}...)")


def resolve_cold_open(ep: Episode, pick: dict, fps: float) -> dict:
    """{doc, source, approval[], warnings[]}. `pick` is episode.json sequences.cold_open: one of clip / quote / range / edit,
    optional `trim: [a, b]` (seconds inside a clip or edit)."""
    kinds = [k for k in ("clip", "quote", "range", "edit") if pick.get(k)]
    if len(kinds) != 1:
        raise SequenceError("episode.json sequences.cold_open needs exactly one of clip / quote / range / edit "
                            f"(got {kinds or 'none'})")
    kind, warns, approval = kinds[0], [], ["the cold open is the human's choice (it plays before the title and gives away a moment)"]
    if kind in ("clip", "edit"):
        if kind == "clip":
            c = _find_clip(ep, str(pick["clip"]))
            e = c.get("edit") or {}
            path = ep.root / e["path"] if e.get("path") else None
            if not path or not path.exists():
                raise SequenceError(f"clip {c['id']} has no built edit: run `ave clips build {ep.root}` first")
            doc = editdoc.load(path)
            src = {"kind": "clip", "id": c["id"], "title": c.get("title"), "status": c.get("status"), "edit": e["path"],
                   "range": e.get("range"), "source_edit": e.get("source")}
            if c.get("status") != "approved":
                approval.append(f"clip {c['id']} is {c.get('status') or 'unknown'}, not approved (ave clips review / decide)")
        else:
            nm = str(pick["edit"])
            stage, _, slug = nm.partition(".")
            if stage != "select" or not slug or not editdoc.list_versions(ep, "select", slug):
                raise SequenceError(f"cold open edit {nm!r}: use select.<slug> of a built select (see edits/)")
            v = editdoc.list_versions(ep, "select", slug)[-1]
            doc = editdoc.load(editdoc.path_for(ep, "select", v, slug))
            src = {"kind": "edit", "edit": nm, "version": v}
        a, b = (pick.get("trim") or [0.0, doc["duration"]])
        if not 0 <= a < b <= doc["duration"] + 1e-3:
            raise SequenceError(f"cold open trim {pick.get('trim')} is outside the {doc['duration']:.2f} s of {src.get('id') or src.get('edit')}")
        doc = _whole_frames_slice(doc, float(a), float(b), fps)
        src["window"] = [round(float(a), 3), round(float(b), 3)]
    else:
        spec = pick[kind]
        stage, name = spec.get("stage", "rough"), spec.get("name")
        if not editdoc.list_versions(ep, stage, name):
            raise SequenceError(f"cold open {kind}: no {stage}{'.' + name if name else ''} edit to cut from")
        ver = editdoc.list_versions(ep, stage, name)[-1]
        base = editdoc.load(editdoc.path_for(ep, stage, ver, name))
        if kind == "range":
            o0, o1 = float(spec["o0"]), float(spec["o1"])
        else:
            tr = read_json(ep.artifact("transcript.json"))
            words = captions.map_words_to_output(tr["words"], base)
            want = _tokens(str(spec["text"]))
            toks = [_tokens(_word(w)) for w in words]
            flat = [(t, i) for i, ts in enumerate(toks) for t in ts]
            hit = None
            for s in range(len(flat) - len(want) + 1):
                if [t for t, _ in flat[s:s + len(want)]] == want:
                    hit = (flat[s][1], flat[s + len(want) - 1][1])
                    break
            if not want or hit is None:
                raise SequenceError(f"cold open quote {spec['text']!r} was not found in the {stage} cut's transcript "
                                    "(words must match; punctuation and case do not matter)")
            o0 = max(0.0, words[hit[0]]["start"] - float(spec.get("pad_before_s", 0.2)))
            o1 = min(base["duration"], words[hit[1]]["end"] + float(spec.get("pad_after_s", 0.35)))
        if not 0 <= o0 < o1 <= base["duration"] + 1e-3:
            raise SequenceError(f"cold open range {o0:.2f}-{o1:.2f} s is outside the {stage} cut (0-{base['duration']:.2f} s)")
        doc = _whole_frames_slice(base, o0, o1, fps)
        src = {"kind": kind, "stage": stage, "name": name, "version": ver, "range": [round(o0, 3), round(o1, 3)]}
        if kind == "quote":
            src["text"] = spec["text"]
    try:
        tr = read_json(ep.artifact("transcript.json"))
        words = captions.map_words_to_output(tr["words"], doc)
        src["excerpt"] = _clip_text(" ".join(_word(w).strip() for w in words), 240)
    except Exception:
        src["excerpt"] = ""
    return {"doc": doc, "source": src, "approval": approval, "warnings": warns}


# ====================================================================================================================
# PLAN
# ====================================================================================================================

def _validate_props(template: str, props: dict, errors: list[str], where: str) -> dict:
    schema = json.loads((SEQ_KIT / template / "schema.json").read_text())
    probs, out = motion.validate_props(schema, props)
    for k, rule in schema.get("properties", {}).items():
        v = out.get(k)
        if rule.get("type") == "array" and k in out:
            if not isinstance(v, list):
                probs.append(f"props.{k}: must be a list")
            else:
                if len(v) < rule.get("minItems", 0):
                    probs.append(f"props.{k}: needs at least {rule['minItems']} item(s)")
                if len(v) > rule.get("maxItems", 10 ** 6):
                    probs.append(f"props.{k}: {len(v)} items, the layout holds at most {rule['maxItems']}")
    errors += [f"{where}: {p}" for p in probs]
    return out


def _kit_hash(template: str) -> str:
    h = hashlib.sha256()
    for f in (SEQ_KIT / template / "template.html", SEQ_KIT / template / "schema.json", motion.KIT / "brand" / "tokens.json",
              motion.KIT / "lib" / "timeline.js", motion.KIT / "lib" / "base.css"):
        h.update(f.read_bytes())
    return h.hexdigest()[:10]


def _asset(ep: Episode, rel: str, role: str, used_in: list[str]) -> dict:
    p = Path(rel) if Path(rel).is_absolute() else ep.root / rel
    a: dict = {"file": rel, "role": role, "used_in": used_in}
    if not p.is_file():
        return {**a, "status": "missing-file"}
    sha = licenses.sha256_file(p)
    e = licenses.log_for(ep).find(sha256=sha)
    a["sha256"] = sha
    if not e:
        return {**a, "status": "unlicensed"}
    att = e.get("attribution") or {}
    return {**a, "status": "licensed", "license": e.get("license"), "source": e.get("source"), "author": e.get("author"),
            "url": e.get("url"), "attribution_required": bool(att.get("required")), "attribution_text": att.get("text") or "",
            "title": e.get("title") or e.get("notes") or ""}


def credit_line(a: dict) -> str:
    """One credits line for a licensed asset: the logged attribution text when there is one, else built from the log."""
    if a.get("attribution_text"):
        return a["attribution_text"]
    title = a.get("title") or re.sub(r"[_\-]+", " ", Path(a["file"]).stem).strip().title()
    who = f" by {a['author']}" if a.get("author") else ""
    lic = f" ({a['license']})" if a.get("license") else ""
    return f"{title}{who}{lic}"


def _music_cfg(ep: Episode, cfg: dict | None, role: str, item_id: str, assets: list, errors: list, warnings: list) -> dict | None:
    if not cfg or not cfg.get("file"):
        return None
    a = _asset(ep, str(cfg["file"]), role, [item_id])
    assets.append(a)
    if a["status"] == "missing-file":
        errors.append(f"{role}: file {cfg['file']} does not exist")
    elif a["status"] == "unlicensed":
        errors.append(f"{role}: {cfg['file']} has no license-log entry (ave assets log-license); the sequence cannot use it")
    return {**{k: v for k, v in cfg.items() if k != "file"}, "file": str(cfg["file"])}


def plan(ep: Episode, name: str | None = None, policy: Path | str | None = None, home: Path | None = None, doc: dict | None = None,
         write: bool = True, _want: dict | None = None) -> dict:
    """What would be built, with the source of every piece of text. Writes work/sequence.json (unless write=False).

    `doc` is the cut being finished (ave final passes the final EditDoc it is building); otherwise the latest final, rough or assembly."""
    pol = resolve_policy(ep, policy, home)
    stage, bdoc = ("(given)", doc) if doc is not None else base_doc(ep, name)
    meta = _meta(ep)
    ident = identity(meta, pol)
    fps, cv = float(bdoc["fps"]), bdoc["canvas"]
    errors: list[str] = []
    warnings: list[str] = list(ident["warnings"])
    assets: list[dict] = []
    pick = (meta.get("sequences") or {}).get("cold_open")
    room = bdoc["tracks"].get("room_tone")
    if not room and pol["audio"]["room_tone"]:      # an older rough cut: borrow cleanup's pick (not saved into any edit)
        try:
            from . import cleanup
            room, _ = cleanup.auto_room_tone(ep, bdoc, cleanup.resolve_policy(ep, home=home)[0])
        except Exception:
            room = None
    out: dict = {"version": 1, "episode": ep.root.name, "name": name, "base": {"stage": stage, "fps": fps, "canvas": cv,
                 "duration": bdoc.get("duration"), "room_tone": room},
                 "identity": {k: v for k, v in ident.items() if k != "warnings"}}

    def card(kind, template, duration, props, text, o0, extra_approval=None, outro=True):
        props = _validate_props(template, props, errors, kind)
        frames = _frames(duration, fps)
        approvals = list(extra_approval or [])
        approvals += [f"{k}: {v['value']!r} ({v['source']})" for k, v in text.items() if v.get("approve")]
        return {"id": kind, "kind": kind, "template": template, "frames": frames, "duration": _secs(frames, fps), "o0": o0,
                "o1": o0 + _secs(frames, fps), "props": props, "outro": outro, "kit": _kit_hash(template),
                "text": text, "needs_approval": approvals}

    # ---------------- opening ----------------
    op = pol["opening"]
    items: list[dict] = []
    cold = None
    t = 0.0
    co_pol = op["cold_open"]
    co_on = pick is not None if co_pol["enabled"] == "auto" else bool(co_pol["enabled"])
    if co_pol["enabled"] is False and pick:
        warnings.append("episode.json has a sequences.cold_open pick but the policy has opening.cold_open.enabled: false; ignored")
    if co_pol["enabled"] is True and not pick:
        warnings.append("opening.cold_open.enabled is true but episode.json sequences.cold_open names nothing: no cold open")
        co_on = False
    if co_on and pick:
        try:
            cold = resolve_cold_open(ep, pick, fps)
        except SequenceError as e:
            errors.append(str(e))
    if cold:
        d = cold["doc"]["duration"]
        if d > co_pol["max_s"] + 1e-6:
            errors.append(f"cold open is {d:.1f} s, longer than opening.cold_open.max_s ({co_pol['max_s']} s): pick a tighter window (trim)")
        elif d < co_pol["min_s"]:
            warnings.append(f"cold open is {d:.1f} s, shorter than {co_pol['min_s']} s: a hook this short may not land")
        tail = _frames(co_pol["tail_pad_s"], fps)
        text = {"excerpt": _text(cold["source"].get("excerpt", ""), f"transcript words of the {cold['source']['kind']} pick")}
        items.append({"id": "cold_open", "kind": "cold_open", "template": None, "frames": round(d * _fpsf(fps)) + tail,
                      "duration": d + _secs(tail, fps), "o0": 0.0, "o1": d + _secs(tail, fps), "hook_s": d, "source": cold["source"],
                      "captioned": bool(co_pol["captions"]), "kit": None, "text": text, "needs_approval": cold["approval"]})
        t = d + _secs(tail, fps)
        music_co = _music_cfg(ep, co_pol.get("music"), "cold-open music", "cold_open", assets, errors, warnings)
    else:
        music_co = None
    if op["title_card"]["enabled"]:
        s = op["title_card"]
        props = {"show": ident["show"]["value"], "number": ident["number"]["value"], "title": ident["title"]["value"],
                 "tagline": ident["tagline"]["value"]}
        if ident["logo"]["value"]:
            lp = Path(ident["logo"]["value"])
            lp = lp if lp.is_absolute() else ep.root / lp
            if lp.is_file():
                props["logo"] = lp.resolve().as_uri()
            else:
                errors.append(f"show.logo {ident['logo']['value']} does not exist")
        it = card("title_card", s["template"], s["duration_s"], props,
                  {"show": ident["show"], "number": ident["number"], "title": ident["title"], "tagline": ident["tagline"]}, t)
        if not 3 <= it["duration"] <= 6.5:
            warnings.append(f"title card is {it['duration']:.1f} s; 3 to 6 s reads as a title, longer feels like waiting")
        items.append(it)
        t = it["o1"] + op["gap_s"]
    if op["hosts"]["enabled"]:
        s = op["hosts"]
        people = ident["hosts"]["value"]
        if not people:
            warnings.append("no hosts to introduce: set episode.json show.hosts (or name the speakers); the host card is skipped")
        else:
            if s["max_s"] and len(people) > 4:
                errors.append("host card: at most 4 people fit; list the others in the credits")
            if pol["critic"]["host_title_required"] and any(not p["title"] for p in people):
                warnings.append("a host has no title (policy critic.host_title_required): set show.hosts[].title, e.g. Co-host")
            it = card("host_intro", s["template"], s["duration_s"], {"heading": s["heading"], "people": people[:4]},
                      {"heading": _text(s["heading"], "sequences policy opening.hosts.heading"), "people": ident["hosts"]},
                      t, extra_approval=["host titles (e.g. Co-host) are as printed on screen"])
            items.append(it)
            t = it["o1"]
    sting = None
    if op["sting"].get("file"):
        a = _asset(ep, str(op["sting"]["file"]), "opening sting", ["title_card"])
        assets.append(a)
        if a["status"] != "licensed":
            errors.append(f"opening sting {op['sting']['file']}: " + ("does not exist" if a["status"] == "missing-file" else "has no license-log entry"))
        sting = dict(op["sting"])
    bed = _music_cfg(ep, op.get("music"), "opening music", "title_card", assets, errors, warnings)
    open_total = items[-1]["o1"] if items else 0.0
    out["opening"] = {"enabled": bool(items), "items": items, "duration": open_total, "frames": sum(i["frames"] for i in items),
                      "music": {"cold_open": music_co, "bed": bed, "sting": sting}}
    if open_total > pol["critic"]["opening_max_s"]:
        warnings.append(f"the opening runs {open_total:.0f} s (policy critic.opening_max_s {pol['critic']['opening_max_s']}): "
                        "viewers wait that long before the conversation starts")

    # ---------------- closing ----------------
    cl = pol["closing"]
    citems: list[dict] = []
    t = 0.0
    links = ident["links"]["value"]
    if cl["thanks"]["enabled"]:
        s = cl["thanks"]
        lines = links[:4] if s["use_links"] else []
        it = card("thanks", s["template"], s["duration_s"], {"heading": s["heading"], "body": s["body"], "lines": lines},
                  {"heading": _text(s["heading"], "sequences policy closing.thanks.heading"), "body": _text(s["body"], "sequences policy closing.thanks.body"),
                   "lines": ident["links"]}, t, extra_approval=["links and calls to action printed on screen"] if lines else None)
        citems.append(it)
        t = it["o1"]
    if cl["cta"]["enabled"]:
        s = cl["cta"]
        lines = (s["lines"] or links)[:4]
        it = card("cta", s["template"], s["duration_s"], {"heading": s["heading"], "body": s["body"], "lines": lines},
                  {"heading": _text(s["heading"], "sequences policy closing.cta.heading"), "lines": _text(lines, "policy cta.lines or episode.json show.links")},
                  t, extra_approval=["call to action text and links"])
        citems.append(it)
        t = it["o1"]
    if cl["credits"]["enabled"]:
        s = cl["credits"]
        sections, ctext = [], {}
        hosts = ident["hosts"]["value"]
        if hosts:
            sections.append({"heading": s["hosts_role"], "lines": [h["name"] for h in hosts][:12]})
            ctext["hosts"] = ident["hosts"]
        guests = ident["guests"]["value"]
        if guests:
            sections.append({"heading": s["guests_role"], "lines": [g["name"] + (f", {g['title']}" if g["title"] else "") for g in guests][:12]})
            ctext["guests"] = ident["guests"]
        editor = s.get("editor")
        edit_lines = []
        if editor:
            edit_lines.append(str(editor))
            ctext["editor"] = _text(str(editor), "sequences policy closing.credits.editor (per-show or ~/.config/ave/sequences.yaml)", approve=True)
        else:
            warnings.append("no editor name: set closing.credits.editor in <project>/policy/sequences.yaml or ~/.config/ave/sequences.yaml; the credit is skipped")
        if s["edited_with_ave"]:
            edit_lines.append("Edited with ave")
            ctext["edited_with_ave"] = _text("Edited with ave", "sequences policy closing.credits.edited_with_ave: true")
        if edit_lines:
            sections.append({"heading": s["editor_role"], "lines": edit_lines})
        # asset credits: everything the CUT uses (music, B-roll) plus what the sequences themselves use
        used = [(m["file"], "music") for m in bdoc["tracks"].get("music") or []]
        used += [(o["file"], "broll") for o in bdoc["tracks"].get("overlays") or [] if o.get("kind") == "broll"]
        cut_assets = []
        seen = {a["file"] for a in assets}
        for f, role in used:
            if f not in seen:
                seen.add(f)
                cut_assets.append(_asset(ep, f, role, ["credits"]))
        cmusic = _music_cfg(ep, cl.get("music"), "closing music", "credits", assets, errors, warnings)
        all_assets = cut_assets + [a for a in assets if a["status"] != "missing-file"]
        clines, seen_lines = [], set()
        for a in all_assets:
            if a["status"] == "unlicensed":
                errors.append(f"credits: {a['file']} is used but has no license-log entry")
                continue
            if a["status"] != "licensed" or (s["assets"] == "required" and not a["attribution_required"]):
                continue
            if a["attribution_required"] and not a["attribution_text"]:
                warnings.append(f"{a['file']}: the license requires attribution but the log has no exact credit line (ave assets log-license --attribution)")
            line = _clip_text(credit_line(a), 70)
            if line not in seen_lines:
                seen_lines.add(line)
                clines.append(line)
        assets.extend(a for a in cut_assets if a not in assets)
        if clines:
            sections.append({"heading": s["music_role"], "lines": clines[:12]})
            ctext["assets"] = _text(clines, "work/assets/licenses.jsonl (files this cut and its sequences use)")
        if s["links"] and links:
            sections.append({"heading": "Find us", "lines": [f"{l['label']}  {l['value']}".strip() for l in links][:12]})
            ctext["links"] = ident["links"]
        if not sections:
            warnings.append("nothing to put in the credits (no hosts, guests, editor or assets): the credits roll is skipped")
        else:
            props = {"heading": s["heading"] or ident["show"]["value"], "sections": sections[:14]}
            props = _validate_props(s["template"], props, errors, "credits")
            for sec in props["sections"]:
                for ln in sec["lines"]:
                    if len(ln) > 70:
                        errors.append(f"credits line too long ({len(ln)} > 70): {ln[:40]}...")
            citems.append({"id": "credits", "kind": "credits", "template": s["template"], "props": props, "text": ctext, "outro": False,
                           "kit": _kit_hash(s["template"]), "min_s": s["min_s"], "max_s": s["max_s"], "px_per_frame": s["px_per_frame"],
                           "lead_in_s": s["lead_in_s"], "duration": None, "frames": None, "o0": None, "o1": None,
                           "needs_approval": [f"{k}: {v['value']!r} ({v['source']})" for k, v in ctext.items() if v.get("approve")]})
    else:
        cmusic = _music_cfg(ep, cl.get("music"), "closing music", "credits", assets, errors, warnings)
    if cl["end_card"]["enabled"]:
        s = cl["end_card"]
        elines = (s["lines"] or [])[:3]
        it = card("end_card", s["template"], s["hold_s"], {"heading": s["heading"], "body": s["body"], "lines": elines, "layout": s["layout"]},
                  {"heading": _text(s["heading"], "sequences policy closing.end_card.heading"), "body": _text(s["body"], "sequences policy closing.end_card.body")},
                  0.0, outro=False)
        it["hold_s"] = s["hold_s"]
        citems.append(it)
    tail_frames = _frames(cl["tail_room_tone_s"], fps) if cl["tail_room_tone_s"] > 0 else 0
    if tail_frames:
        citems.append({"id": "tail", "kind": "tail_room_tone", "template": None, "frames": tail_frames, "duration": _secs(tail_frames, fps),
                       "o0": None, "o1": None, "kit": None, "text": {}, "needs_approval": []})
    if not bdoc["tracks"].get("room_tone") and pol["audio"]["room_tone"]:
        if room:
            warnings.append("the cut has no tracks.room_tone (ave cleanup fills it): the cards borrow the quietest steady stretch of the dialogue source "
                            f"(episode {room['e0']:.1f}-{room['e1']:.1f} s, {room.get('rms_db')} dB); listen to it under the cards")
        else:
            warnings.append("the cut has no tracks.room_tone (ave cleanup fills it) and none could be found: cards and the tail sit on digital silence")
    # credits length needs the canvas: measured at build time (the column height decides it); the estimate here is for the plan
    out["closing"] = {"enabled": bool(citems), "items": citems, "music": cmusic, "duration": None, "frames": None}
    out["assets"] = assets
    out["warnings"] = warnings
    out["errors"] = errors
    out["policy_sha"] = _sha(pol)
    _finalize_closing_times(out, fps, cv, estimate=True)
    if out["closing"]["duration"] and out["closing"]["duration"] > pol["critic"]["closing_max_s"]:
        warnings.append(f"the closing runs about {out['closing']['duration']:.0f} s (policy critic.closing_max_s {pol['critic']['closing_max_s']})")

    for which in WHICH:
        if not out[which]["enabled"]:
            out[which]["hash"] = None
            continue
        basis = {"items": [{k: i.get(k) for k in ("id", "kind", "template", "props", "frames", "source", "hook_s", "kit", "px_per_frame",
                                                  "lead_in_s", "hold_s", "min_s", "max_s")} for i in out[which]["items"]],
                 "music": out[which]["music"], "fps": fps, "canvas": cv, "policy": out["policy_sha"] if which == "closing" else None,
                 "room_tone": out["base"]["room_tone"],
                 "assets": sorted((a["file"], a.get("sha256") or "") for a in assets)}
        out[which]["hash"] = _sha(basis)
    needs = [{"item": i["id"], "sequence": w, "reasons": i["needs_approval"]} for w in WHICH for i in out[w]["items"] if i.get("needs_approval")]
    out["approval"] = {"required": needs, "hash": _sha({w: out[w]["hash"] for w in WHICH}), "approved": False, "approved_by": None, "approved_at": None}
    ap = read_approval(ep, name)
    if ap and ap.get("hash") == out["approval"]["hash"]:
        out["approval"].update(approved=True, approved_by=ap.get("by"), approved_at=ap.get("at"))
    elif ap:
        out["approval"]["stale_approval"] = "the plan changed since it was approved; approve it again after reviewing"
    out["_cold"] = cold                    # in memory only, for build()
    out["_bdoc"] = bdoc
    out["_music"] = {"co": music_co}
    if write:
        keep = {k: v for k, v in out.items() if not k.startswith("_")}
        prev = read_json(plan_path(ep, name)) if plan_path(ep, name).exists() else {}
        keep["built"] = {w: b for w, b in (prev.get("built") or {}).items() if out.get(w, {}).get("hash") == b.get("hash")}
        write_json(plan_path(ep, name), keep)
    return out


def _finalize_closing_times(pl: dict, fps: float, cv: dict, estimate: bool = False, credits_frames: int | None = None) -> None:
    """Lay the closing items end to end. The credits length comes from the column height at build time; the plan estimates it."""
    items = pl["closing"]["items"]
    t = 0.0
    for it in items:
        if it["kind"] == "credits":
            if credits_frames is None:
                lines = sum(len(s["lines"]) + 2 for s in it["props"]["sections"]) + 3
                px = lines * 0.035 * cv["h"]
                step = max(1, round(it["px_per_frame"] * min(cv["w"], cv["h"]) / 1080))
                credits_frames = int(math.ceil((cv["h"] + px) / step))
            fr = max(credits_frames, _frames(it["min_s"], fps))
            fr = min(fr, max(_frames(it["max_s"], fps), 1)) if not estimate else fr
            it["frames"], it["duration"] = fr, _secs(fr, fps)
            it["estimated"] = estimate
        it["o0"], it["o1"] = t, t + it["duration"]
        t = it["o1"]
    pl["closing"]["duration"] = t if items else 0.0
    pl["closing"]["frames"] = sum(i["frames"] for i in items) if items else 0


# ====================================================================================================================
# APPROVAL (a human act: the agent asks, the human runs `ave sequence approve`)
# ====================================================================================================================

def read_approval(ep: Episode, name: str | None = None) -> dict | None:
    p = approval_path(ep, name)
    return read_json(p) if p.exists() else None


def approve(ep: Episode, name: str | None = None, by: str | None = None, policy=None, home=None) -> dict:
    pl = plan(ep, name, policy, home, write=True)
    if pl["errors"]:
        raise SequenceError("cannot approve a plan with errors:\n  - " + "\n  - ".join(pl["errors"]))
    rec = {"hash": pl["approval"]["hash"], "by": by or "human", "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "approved_items": [f"{n['sequence']}/{n['item']}" for n in pl["approval"]["required"]]}
    write_json(approval_path(ep, name), rec)
    return rec


# ====================================================================================================================
# BUILD: plate, cards, credits column, the sequence EditDoc, render
# ====================================================================================================================

def body_gain_db(ep: Episode, bdoc: dict, pr: dict) -> float:
    """The linear gain `compile.render` applies to the body's audio to reach the preset's loudness target.

    WHY: a sequence must sit in the SAME gain domain as the body (speech of the cold open at the body's level, room tone under the cards at
    the body's noise floor, music at the same distance below speech). Normalising a card-only sequence on its own would boost its room tone to
    the speech target. So sequences are rendered with this fixed gain instead. Cached per cut (the audio compile is the expensive part)."""
    key = _sha({"tracks": bdoc["tracks"], "sources": bdoc["sources"], "duration": bdoc["duration"], "lufs": pr["lufs"], "tp": pr["tp"]})[:12]
    cache = ep.work / "sequence" / f"gain.{key}.json"
    if cache.exists():
        return float(read_json(cache)["gain_db"])
    wd = ep.work / "sequence" / f"gain.{key}.tmp"
    try:
        wav = comp.compile_audio(ep, bdoc, wd / "master.wav")
        m = ffutil.loudnorm_measure(wav, pr["lufs"], pr["tp"])
    finally:
        shutil.rmtree(wd, ignore_errors=True)
    gain = float(pr["lufs"] - m["input_i"]) if m else 0.0
    write_json(cache, {"gain_db": gain, "measured_lufs": m["input_i"] if m else None, "target": pr["lufs"]})
    return gain


def _hex(color_key: str) -> str:
    return motion.load_tokens()["colors"][color_key]


def make_plate(ep: Episode, canvas: dict, fps: float, seconds: float, color_key: str) -> str:
    """A flat brand-colour video source (bt709-tagged) at least `seconds` long. Returns its path relative to the episode root."""
    n = _frames(seconds + 1.0, fps)
    out = ep.work / "sequence" / f"plate.{color_key}.{canvas['w']}x{canvas['h']}.{_fpsf(fps):g}fps.{n}f.mp4"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.stem + ".partial.mp4")
        fr = ffutil.fps_fraction(fps)
        ffutil.run(["-y", "-f", "lavfi", "-i", f"color=c={_hex(color_key)}:s={canvas['w']}x{canvas['h']}:r={fr.numerator}/{fr.denominator}",
                    "-frames:v", str(n), "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p", "-c:v", "libx264",
                    "-preset", "veryfast", "-crf", "12", "-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709",
                    "-color_trc", "bt709", "-color_range", "tv", "-movflags", "+faststart", str(tmp)])
        tmp.replace(out)
    return ep.rel(out)


def make_silence(ep: Episode, seconds: float) -> str:
    out = ep.work / "sequence" / f"silence.{int(math.ceil(seconds)) + 1}s.wav"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(out), np.zeros((int((math.ceil(seconds) + 1) * SR), 2), dtype="float32"), SR, subtype="FLOAT")
    return ep.rel(out)


def _spec(item: dict, fps: float, canvas: dict, duration: float | None = None) -> dict:
    return {"template": item["template"], "props": item["props"], "duration": float(duration if duration is not None else item["duration"]),
            "fps": float(fps), "canvas": (canvas["w"], canvas["h"]), "outro": bool(item.get("outro", True))}


def _card_path(ep: Episode, item: dict, spec: dict) -> Path:
    blob = json.dumps({**spec, "canvas": list(spec["canvas"])}, sort_keys=True)
    h = hashlib.sha256((blob + item["kit"]).encode()).hexdigest()[:10]
    return ep.work / "sequence" / "cards" / f"{item['template']}-{h}.mov"


def render_card(ep: Episode, item: dict, fps: float, canvas: dict, duration: float | None = None) -> str:
    spec = _spec(item, fps, canvas, duration)
    target = _card_path(ep, item, spec)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        motion.render(spec, target, template_dir=SEQ_KIT / item["template"])
    return ep.rel(target)


def _edge_mask(w: int, h: int, frac: float = 0.13):
    from PIL import Image
    ramp = np.clip(np.linspace(0, 1, max(2, int(h * frac))), 0, 1)
    ramp = ramp * ramp * (3 - 2 * ramp)                       # smoothstep
    col = np.ones(h, dtype="float32")
    col[:len(ramp)] = ramp
    col[-len(ramp):] = np.minimum(col[-len(ramp):], ramp[::-1])
    return Image.fromarray((np.tile(col[:, None], (1, w)) * 255).astype("uint8"), "L")


def render_credits(ep: Episode, item: dict, fps: float, canvas: dict) -> tuple[str, int]:
    """The credits roll: the motion-kit column rendered once (Chromium, transparent), scrolled by ffmpeg at a whole number of pixels per
    frame (perfectly even), edge-faded, encoded to ProRes 4444 alpha. Returns (path relative to the episode root, frame count)."""
    W, H = canvas["w"], canvas["h"]
    scale = min(W, H) / 1080
    step = max(1, round(item["px_per_frame"] * scale))
    spec = _spec(item, fps, canvas, 1.0)
    h_key = hashlib.sha256((json.dumps({**spec, "canvas": [W, H]}, sort_keys=True) + item["kit"] + f"{step}|{item['min_s']}|{item['max_s']}|{item['lead_in_s']}").encode()).hexdigest()[:10]
    out = ep.work / "sequence" / "cards" / f"credits-roll-{h_key}.mov"
    meta_f = out.with_suffix(".json")
    if out.exists() and meta_f.exists():
        return ep.rel(out), int(read_json(meta_f)["frames"])
    out.parent.mkdir(parents=True, exist_ok=True)
    col = out.with_suffix(".column.png")
    with motion._page(spec, SEQ_KIT / item["template"]) as page:
        hc = int(math.ceil(page.evaluate("document.getElementById('roll').scrollHeight")))
        if hc > MAX_COLUMN_PX:
            raise SequenceError(f"the credits column is {hc}px tall (limit {MAX_COLUMN_PX}): shorten the credits")
        page.set_viewport_size({"width": W, "height": max(hc, H)})
        col.write_bytes(page.screenshot(type="png", omit_background=True, clip={"x": 0, "y": 0, "width": W, "height": hc}))
    lead = int(round(item["lead_in_s"] * _fpsf(fps)))
    travel = H + hc
    frames = lead + int(math.ceil(travel / step))
    min_f, max_f = _frames(item["min_s"], fps), _frames(item["max_s"], fps)
    if frames > max_f:
        step = int(math.ceil(travel / max(1, max_f - lead)))        # faster, still a whole number of pixels
        frames = lead + int(math.ceil(travel / step))
    frames = max(frames, min_f)
    mask = out.with_suffix(".mask.png")
    _edge_mask(W, H).save(mask)
    fr = ffutil.fps_fraction(fps)
    rate = f"{fr.numerator}/{fr.denominator}"
    fc = (f"[0:v][1:v]overlay=x=0:y='H-max(n-{lead},0)*{step}':eval=frame:format=auto[c];"
          f"[c]split[a][b];[a]alphaextract[al];[al][2:v]blend=all_mode=multiply[al2];[b][al2]alphamerge,"
          "scale=out_color_matrix=bt709:out_range=tv:flags=accurate_rnd+full_chroma_int,format=yuva444p10le,"
          "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709[v]")
    tmp = out.with_name(out.stem + ".partial.mov")
    ffutil.run(["-y", "-f", "lavfi", "-i", f"color=c=black@0.0:s={W}x{H}:r={rate},format=rgba",
                "-loop", "1", "-framerate", rate, "-i", str(col), "-loop", "1", "-framerate", rate, "-i", str(mask),
                "-filter_complex", fc, "-map", "[v]", "-frames:v", str(frames), "-c:v", "prores_ks", "-profile:v", "4444", "-vendor", "apl0",
                "-pix_fmt", "yuva444p10le", "-movflags", "+write_colr", "-r", rate, str(tmp)])
    tmp.replace(out)
    col.unlink(missing_ok=True)
    mask.unlink(missing_ok=True)
    write_json(meta_f, {"frames": frames, "step_px": step, "column_px": hc})
    return ep.rel(out), frames


def _music_item(mid: str, cfg: dict, o0: float, o1: float, duck: float | None = None, fade_out: float | None = None) -> dict:
    return {"id": mid, "file": cfg["file"], "o0": round(o0, 4), "o1": round(o1, 4), "in": float(cfg.get("in", 0.0)),
            "gain_db": float(cfg.get("gain_db", -20.0)), "duck_db": float(duck if duck is not None else cfg.get("duck_db", 0.0)),
            "fade_in_s": float(cfg.get("fade_in_s", 0.5)), "fade_out_s": float(fade_out if fade_out is not None else cfg.get("fade_out_s", 1.0))}


def _compose(ep: Episode, which: str, pl: dict, ref: dict, cold: dict | None, pol: dict, cards: dict[str, str], plate: str,
             silence: str | None, canvas: dict, fps: float) -> dict:
    """The sequence as one EditDoc: [cold open segments] + plate; dialogue = hook + room tone; overlays = cards; music items."""
    sec = pl[which]
    total = sum(i["frames"] for i in sec["items"])
    D = _secs(total, fps)
    sources = copy.deepcopy(ref["sources"])
    sources["plate"] = {"kind": "video", "file": plate, "start_offset": 0.0, "drift_ppm": 0.0}
    tracks = {"video": [], "dialogue": [], "room_tone": copy.deepcopy(ref["tracks"].get("room_tone")), "music": [], "overlays": []}
    t0 = 0.0
    if which == "opening" and cold:
        cd = cold["doc"]
        tracks["video"] += [{**s, "id": f"co-{s['id']}"} for s in cd["tracks"]["video"]]
        tracks["dialogue"] += [{**s, "id": f"co-{s['id']}"} for s in cd["tracks"]["dialogue"]]
        t0 = cd["duration"]
    if t0 < D - 1e-9:
        tracks["video"].append({"id": "plate", "src": "plate", "o0": t0, "o1": D, "e0": 0.0, "crop": None, "zoom": 1.0, "reason": "brand plate under the cards"})
        rt = tracks["room_tone"]
        if rt and pol["audio"]["room_tone"]:
            tracks["dialogue"].append({"id": "room", "src": rt["src"], "o0": t0, "o1": D, "e0": rt["e0"], "fill": "room_tone", "xfade_ms": 120, "gain_db": 0.0})
        else:
            sources["silence"] = {"kind": "audio", "file": silence, "start_offset": 0.0, "drift_ppm": 0.0}
            tracks["dialogue"].append({"id": "silence", "src": "silence", "o0": t0, "o1": D, "e0": 0.0, "xfade_ms": 0, "gain_db": 0.0})
    if tracks["video"]:
        tracks["video"][-1]["o1"] = D
        tracks["dialogue"][-1]["o1"] = D
    layer = 1
    for it in sec["items"]:
        if it["id"] in cards:
            tracks["overlays"].append({"id": it["id"], "file": cards[it["id"]], "o0": round(it["o0"], 4), "o1": round(it["o1"], 4), "x": 0, "y": 0,
                                       "w": canvas["w"], "h": canvas["h"], "layer": layer, "kind": "motion", "alpha": True, "template": it["template"]})
            layer += 1
    mus = sec["music"]
    if which == "opening":
        co = (cold and mus.get("cold_open")) or None
        bed = mus.get("bed")
        item_t = {i["id"]: i for i in sec["items"]}
        if co:
            tracks["music"].append(_music_item("m-cold", co, 0.0, item_t["cold_open"]["o1"], fade_out=1.0))
        if bed:
            start = item_t["title_card"]["o0"] if "title_card" in item_t else t0
            tracks["music"].append(_music_item("m-bed", bed, start, D, duck=0.0, fade_out=bed.get("fade_out_s", 1.5)))
        if mus.get("sting") and "title_card" in item_t:
            st = mus["sting"]
            p = Path(st["file"]) if Path(st["file"]).is_absolute() else ep.root / st["file"]
            dur = min(float(st.get("max_s", 6)), ffutil.probe(p)["duration"] or 6.0)
            s0 = item_t["title_card"]["o0"]
            tracks["music"].append(_music_item("m-sting", st, s0, min(D, s0 + dur), duck=0.0, fade_out=0.4) | {"fade_in_s": 0.0})
    elif mus:
        cr = next((i for i in sec["items"] if i["kind"] == "credits"), sec["items"][0])
        end_music = next((i["o0"] for i in sec["items"] if i["kind"] == "end_card"), D)
        tracks["music"].append(_music_item("m-close", mus, 0.0, max(end_music, 1.0), duck=0.0))
    doc = editdoc.new_doc("select", fps, (canvas["w"], canvas["h"]), sources, D)
    doc["tracks"] = tracks
    doc["notes"] = f"ave sequence {which} ({_tag(pl.get('name'))})"
    return doc


def _captions_for(ep: Episode, doc: dict, style_name: str = "podcast") -> tuple[list, dict]:
    style = captions.get_style(style_name, ep=ep)
    return captions.cues_from_edit(ep, doc, style), style


def build(ep: Episode, name: str | None = None, preset: str = "master", policy=None, home=None, which: tuple[str, ...] = WHICH,
          burn: bool = False, force: bool = False, pl: dict | None = None, style: str = "podcast", progress=None, record: bool = True) -> dict:
    """Render the opening / closing to <work>/sequence/<tag>/<which>.<preset>.mp4 (video+audio at the cut's spec) and record them in
    work/sequence.json. A sequence whose plan hash and preset are unchanged is reused unless `force`."""
    pl = pl or plan(ep, name, policy, home, write=True)
    if pl["errors"]:
        raise SequenceError("the sequence plan has errors; fix them first (ave sequence plan):\n  - " + "\n  - ".join(pl["errors"]))
    pol = resolve_policy(ep, policy, home)
    pr = presets.get(preset)
    if pr["kind"] != "video":
        raise SequenceError(f"preset {preset!r} is audio-only")
    bdoc = pl.get("_bdoc") or base_doc(ep, name)[1]
    ref = bdoc
    fps, canvas = float(bdoc["fps"]), bdoc["canvas"]
    gain = None
    d = seq_dir(ep, name)
    d.mkdir(parents=True, exist_ok=True)
    built: dict = dict(read_json(plan_path(ep, name)).get("built") or {}) if plan_path(ep, name).exists() else {}
    report: dict = {"preset": preset, "built": {}, "warnings": []}
    for w in which:
        sec = pl[w]
        if not sec["enabled"]:
            continue
        out = d / f"{w}.{preset}{'.burn' if burn else ''}.mp4"
        prev = built.get(w)
        if gain is None:
            gain = body_gain_db(ep, bdoc, pr)
        if (prev and not force and prev.get("hash") == sec["hash"] and prev.get("preset") == preset and prev.get("burn") == burn
                and abs(prev.get("gain_db", 1e9) - gain) < 0.01 and out.exists()):
            report["built"][w] = {**prev, "reused": True}
            continue
        if progress:
            progress(f"building {w}")
        cold = pl.get("_cold") if w == "opening" else None
        rd = copy.deepcopy(cold["doc"] if cold else ref)
        if not rd["tracks"].get("room_tone") and pl["base"].get("room_tone"):
            rd["tracks"]["room_tone"] = copy.deepcopy(pl["base"]["room_tone"])
        cards: dict[str, str] = {}
        for it in sec["items"]:
            if it["kind"] in ("title_card", "host_intro", "thanks", "cta", "end_card"):
                cards[it["id"]] = render_card(ep, it, fps, canvas)
            elif it["kind"] == "credits":
                cards[it["id"]], fr = render_credits(ep, it, fps, canvas)
                it["frames"], it["duration"] = fr, _secs(fr, fps)
        if w == "closing":
            _finalize_closing_times(pl, fps, canvas, credits_frames=next(i["frames"] for i in sec["items"] if i["kind"] == "credits")
                                    if any(i["kind"] == "credits" for i in sec["items"]) else None)
        else:
            t = 0.0
            for it in sec["items"]:
                it["o0"], it["o1"] = t, t + it["duration"]
                t = it["o1"]
        total_s = _secs(sum(i["frames"] for i in sec["items"]), fps)
        plate = make_plate(ep, canvas, fps, total_s, pol["plate"]["color"])
        silence = None if (rd["tracks"].get("room_tone") and pol["audio"]["room_tone"]) else make_silence(ep, total_s)
        doc = _compose(ep, w, pl, rd, cold, pol, cards, plate, silence, canvas, fps)
        cold_srt = None
        if w == "opening" and cold and any(i["kind"] == "cold_open" and i["captioned"] for i in sec["items"]):
            cues, st = _captions_for(ep, doc, style)
            if cues:
                srt = captions.write_srt(cues, d / "opening.cold.srt")
                doc["captions"] = {"srt": ep.rel(srt), "ass": None, "burn": False}
                cold_srt = ep.rel(srt)
                if burn:
                    info = captions.write_ass(cues, st, (canvas["w"], canvas["h"]), d / "opening.cold.ass")
                    doc["captions"] = {"srt": ep.rel(srt), "ass": ep.rel(info["path"]), "burn": True}
        problems = editdoc.validate(doc)
        if problems:
            raise SequenceError(f"internal: the {w} EditDoc is invalid:\n  - " + "\n  - ".join(problems))
        write_json(d / f"{w}.doc.json", doc)
        rr = comp.render(ep, doc, out, preset={**pr, "fixed_gain_db": gain}, burn_captions=burn, overwrite=True)
        info = {"file": ep.rel(out), "preset": preset, "burn": burn, "hash": sec["hash"], "duration": rr["duration"], "frames": sum(i["frames"] for i in sec["items"]),
                "fps": rr["fps"], "w": canvas["w"], "h": canvas["h"], "lufs": rr["lufs"], "true_peak_db": rr["true_peak_db"], "gain_db": round(gain, 3),
                "items": [{"id": i["id"], "o0": i["o0"], "o1": i["o1"]} for i in sec["items"]], "warnings": rr["warnings"],
                "cold_open_srt": cold_srt}
        built[w] = info
        report["built"][w] = info
        report["warnings"] += [f"{w}: {x}" for x in rr["warnings"]]
    if record:                  # a preview must not displace the master build recorded for `ave final`
        saved = {k: v for k, v in pl.items() if not k.startswith("_")}
        saved["built"] = built
        write_json(plan_path(ep, name), saved)
    report["plan"] = ep.rel(plan_path(ep, name))
    report["approved"] = pl["approval"]["approved"]
    return report


def preview(ep: Episode, name: str | None = None, which: tuple[str, ...] = WHICH, policy=None, home=None, out_dir: Path | None = None) -> dict:
    """A DRAFT: 540p, built into <work>/sequence/<tag>/ (separate from the master build) with a still at the middle of every item."""
    rep = build(ep, name, preset="proxy", policy=policy, home=home, which=which, record=False)
    stills = {}
    od = Path(out_dir) if out_dir else seq_dir(ep, name) / "stills"
    od.mkdir(parents=True, exist_ok=True)
    for w, b in rep["built"].items():
        for it in b["items"]:
            t = (it["o0"] + it["o1"]) / 2 if it["id"] not in ("credits",) else it["o0"] + (it["o1"] - it["o0"]) * 0.4
            png = od / f"{w}-{it['id']}.png"
            ffutil.run(["-y", "-ss", f"{t:.3f}", "-i", str(ep.root / b["file"]), "-frames:v", "1", str(png)])
            stills.setdefault(w, []).append({"item": it["id"], "t": round(t, 2), "png": str(png)})
    rep["stills"] = stills
    rep["draft"] = True
    return rep


# ====================================================================================================================
# PROGRAMME: opening + body + closing
# ====================================================================================================================

def _decode_wav(path: Path, out: Path, n: int) -> np.ndarray:
    ffutil.run(["-y", "-i", str(path), "-vn", "-ar", str(SR), "-ac", "2", "-c:a", "pcm_f32le", str(out)])
    x, _ = sf.read(str(out), dtype="float32", always_2d=True)
    out.unlink(missing_ok=True)
    if len(x) < n:
        x = np.concatenate([x, np.zeros((n - len(x), 2), dtype="float32")])
    return x[:n]


def join_audio(parts: list[np.ndarray], fade_s: float) -> tuple[np.ndarray, list[int]]:
    """Concatenate with an equal-power fade-out of each left part's last `fade_s/2` and fade-in of each right part's first `fade_s/2`.
    Returns (audio, join sample indices). Length is exactly the sum of the parts (nothing overlaps, so sync cannot move)."""
    h = int(round(fade_s / 2 * SR))
    parts = [p.copy() for p in parts]
    joins, pos = [], 0
    for i, p in enumerate(parts):
        pos += len(p)
        if i + 1 < len(parts):
            joins.append(pos)
    if h > 0:
        for i, p in enumerate(parts):
            if i + 1 < len(parts) and len(p) >= h:
                th = (np.arange(h) + 0.5) / h * (np.pi / 2)
                p[-h:] *= np.cos(th)[:, None].astype("float32")
            if i > 0 and len(p) >= h:
                th = (np.arange(h) + 0.5) / h * (np.pi / 2)
                p[:h] *= np.sin(th)[:, None].astype("float32")
    return np.concatenate(parts), joins


def srt_shift(src: Path | str, offset_s: float, out: Path | str) -> Path:
    """Copy an SRT with every cue moved `offset_s` later (the length of the opening)."""
    cues = captions.parse_srt(src)
    for c in cues:
        c.start += offset_s
        c.end += offset_s
        for w in c.words:
            w["start"] += offset_s
            w["end"] += offset_s
    return captions.write_srt(cues, out)


def programme_srt(ep: Episode, body_srt: Path | str | None, opening_cold_srt: Path | str | None, offset_s: float, out: Path | str) -> Path | None:
    """The planned SRT of the whole programme: the cold open's cues (they start at 0) then the body's cues shifted by the opening's length."""
    cues = []
    if opening_cold_srt and Path(opening_cold_srt).exists():
        cues += captions.parse_srt(opening_cold_srt)
    if body_srt and Path(body_srt).exists():
        for c in captions.parse_srt(body_srt):
            c.start += offset_s
            c.end += offset_s
            for w in c.words:
                w["start"] += offset_s
                w["end"] += offset_s
            cues.append(c)
    if not cues:
        return None
    cues.sort(key=lambda c: c.start)
    return captions.write_srt(cues, out)


def assemble(ep: Episode, body: Path | str, opening: Path | str | None, closing: Path | str | None, out: Path | str, preset: str = "master",
             fade_s: float = 0.2, tolerance_lu: float = 1.5, overwrite: bool = True) -> dict:
    """opening + body + closing -> one verified file. Picture: stream copy of the three (the body is not re-encoded). Audio: float join
    with equal-power fades at the joins, one loudness normalisation to the preset, measured on the encoded file."""
    body, out = Path(body), Path(out)
    pr = presets.get(preset)
    files = [("opening", opening), ("body", body), ("closing", closing)]
    files = [(k, Path(f)) for k, f in files if f]
    infos = {k: ffutil.probe(f) for k, f in files}
    ref = infos["body"]
    for k, i in infos.items():
        if (i["w"], i["h"]) != (ref["w"], ref["h"]) or abs(i["fps"] - ref["fps"]) > 0.01:
            raise SequenceError(f"{k} is {i['w']}x{i['h']} at {i['fps']:.3f} fps but the body is {ref['w']}x{ref['h']} at {ref['fps']:.3f}: rebuild the sequences (ave sequence build)")
        if not (i["has_video"] and i["has_audio"]):
            raise SequenceError(f"{k} ({f}) must have picture and sound")
    fps = ffutil.fps_fraction(ref["fps"])
    frames = {k: ffutil.count_video_packets(f) for k, f in files}
    wd = out.parent / (out.stem + ".assemble")
    wd.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    try:
        # The concat demuxer needs one time base for every part (a part that went through the overlay pass has a different one than
        # a plain body, and the joined picture then plays at the wrong speed): rewrite the track time scale by stream copy where it differs.
        ts = fps.numerator * max(1, math.ceil(10000 / fps.numerator))
        parts_v = []
        for k, f in files:
            tb = subprocess.run([ffutil._bin("ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=time_base", "-of", "csv=p=0", str(f)],
                                capture_output=True, text=True).stdout.strip()
            if tb == f"1/{ts}":
                parts_v.append(f)
                continue
            fixed = wd / f"{k}.v.mp4"
            ffutil.run(["-y", "-i", str(f), "-map", "0:v:0", "-c", "copy", "-an", "-video_track_timescale", str(ts), str(fixed)])
            parts_v.append(fixed)
        lst = wd / "concat.txt"
        lst.write_text("".join(f"file '{f.resolve()}'\n" for f in parts_v))
        pic = wd / "picture.mp4"
        ffutil.run(["-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-map", "0:v:0", "-c:v", "copy", "-an", "-movflags", "+faststart", str(pic)])
        total_f = sum(frames.values())
        got = ffutil.count_video_packets(pic)
        if got != total_f:
            raise SequenceError(f"joined picture has {got} frames, expected {total_f}")
        parts = []
        for k, f in files:
            n = int(round(frames[k] / float(fps) * SR))
            parts.append(_decode_wav(f, wd / f"{k}.wav", n))
        names = [k for k, _ in files]
        bi = names.index("body")
        if fade_s > 0:           # would the fade soften speech at the body's edges?
            h = int(round(fade_s / 2 * SR))
            for side, seg in (("start", parts[bi][:h]), ("end", parts[bi][-h:])):
                if (side == "start" and bi > 0) or (side == "end" and bi < len(parts) - 1):
                    rms = 20 * math.log10(float(np.sqrt((seg ** 2).mean())) + 1e-9)
                    if rms > TAIL_PROBE_DB:
                        warnings.append(f"the body {side}s with sound ({rms:.0f} dBFS in the first/last {fade_s / 2 * 1000:.0f} ms): the join fade will soften it; "
                                        "leave a little room at the cut's edge (ave trim) or lower sequences audio.join_fade_s")
        audio, joins = join_audio(parts, fade_s)
        master = wd / "master.wav"
        sf.write(str(master), audio, SR, subtype="FLOAT")
        n_samples = len(audio)
        limit_db = -2.0
        partial = out.with_name(out.stem + ".partial" + out.suffix)
        for _ in range(3):
            norm = comp._normalize(master, wd / "norm.wav", pr, n_samples, warnings, limit_db=limit_db)
            ffutil.run(["-y", "-i", str(pic), "-i", str(norm), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", *comp._encode_audio_args(pr),
                        "-ar", str(SR), "-movflags", "+faststart", str(partial)])
            loud = ffutil.measure_loudness(partial)
            if loud["true_peak_db"] <= comp.TRUE_PEAK_LIMIT_DB:
                break
            limit_db -= (loud["true_peak_db"] - comp.TRUE_PEAK_LIMIT_DB) + 0.5
        total_s = total_f / float(fps)
        comp._verify(partial, {"duration": total_s}, pr, fps, (ref["w"], ref["h"]))
        if out.exists() and not overwrite:
            raise FileExistsError(str(out))
        partial.replace(out)
        res = {"out": str(out), "duration": round(ffutil.probe(out)["duration"], 4), "expected_duration": round(total_s, 4), "fps": float(fps),
               "lufs": round(loud["lufs"], 2), "true_peak_db": round(loud["true_peak_db"], 2), "lra": round(loud["lra"], 2), "preset": preset,
               "frames": {**frames, "total": total_f}, "joins_s": [round(j / SR, 4) for j in joins], "fade_s": fade_s}
        # loudness consistency: each part measured on its own (decoded) against the body
        lufs = {}
        for (k, f) in files:
            try:
                lufs[k] = round(ffutil.measure_loudness(f)["lufs"], 1)
            except Exception:
                pass
        res["part_lufs"] = lufs
        for k, v in lufs.items():
            # parts share the body's gain, so a card-only part is far quieter by design; only a part with real programme content is compared
            if k != "body" and v > -30 and "body" in lufs and abs(v - lufs["body"]) > tolerance_lu:
                warnings.append(f"{k} measures {v} LUFS against the body's {lufs['body']} (more than {tolerance_lu} LU apart)")
        if loud["lufs"] > -70 and abs(loud["lufs"] - pr["lufs"]) > comp.LOUDNESS_TOLERANCE_LU:
            warnings.append(f"programme loudness {loud['lufs']:.1f} LUFS is outside {pr['lufs']} +/- {comp.LOUDNESS_TOLERANCE_LU}")
        res["warnings"] = warnings
        return res
    finally:
        partial = out.with_name(out.stem + ".partial" + out.suffix)
        partial.unlink(missing_ok=True)
        shutil.rmtree(wd, ignore_errors=True)


# ====================================================================================================================
# THE `ave final` HOOK (isolated: final.py calls only these two functions)
# ====================================================================================================================

def final_prepare(ep: Episode, doc: dict, *, name: str | None, opening: bool | None, closing: bool | None, preset: str, build_now: bool,
                  burn: bool, draft: bool = False, style: str = "podcast", allow_unlicensed: bool = False) -> dict | None:
    """Called by `build_final` before the final EditDoc is saved. Decides which sequences are wanted (final policy + flags), requires an
    approved, unchanged plan (or --draft-sequences), builds them (unless this is a no-render run) and records `doc["sequences"]`
    so the Resolve handoff can place them. Returns a context for `final_finish`, or None when no sequence is wanted."""
    want = {"opening": wanted(ep, "opening", opening), "closing": wanted(ep, "closing", closing)}
    if not any(want.values()):
        return None
    pl = plan(ep, name, doc=doc, write=True)
    which = tuple(w for w in WHICH if want[w])
    missing = [w for w in which if not pl[w]["enabled"]]
    warnings = [f"sequences.{w} is on but the policy defines nothing to build for the {w}" for w in missing]
    which = tuple(w for w in which if pl[w]["enabled"])
    errors = [e for e in pl["errors"] if not (allow_unlicensed and "license-log" in e)]
    pl["errors"] = errors
    if errors:
        raise SystemExit("The sequence plan has errors, so `ave final` cannot add the opening/closing:\n  - " + "\n  - ".join(errors) +
                         f"\nFix them (see {ep.rel(plan_path(ep, name))}) or run with --no-opening/--no-closing.")
    if build_now and not pl["approval"]["approved"] and not draft:
        need = "; ".join(f"{n['sequence']}/{n['item']}" for n in pl["approval"]["required"]) or "the plan"
        raise SystemExit("The opening/closing plan is not approved (or changed since it was). Review it with the human "
                         f"(work/sequence.json, `ave sequence preview`), then ask the human to run `ave sequence approve {ep.root}`. "
                         f"Needs approval: {need}.\nFor a clearly marked draft pass --draft-sequences.")
    tag = f"final.{name}" if name else "final"
    seq = {"planned": True, "which": list(which), "draft": bool(draft and not pl["approval"]["approved"]), "plan": ep.rel(plan_path(ep, name)),
           "plan_hash": pl["approval"]["hash"], "preset": preset, "srt": ep.rel(ep.captions / f"{tag}.programme.srt")}
    if draft and not pl["approval"]["approved"]:
        warnings.append("DRAFT sequences: the plan is not approved; do not publish this render")
    ctx = {"which": which, "plan": pl, "warnings": warnings, "name": name, "burn": burn, "style": style}
    if build_now and which:
        rep = build(ep, name, preset=preset, which=which, burn=burn, pl=pl, style=style)
        ctx["build"] = rep
        warnings += rep["warnings"]
        for w, b in rep["built"].items():
            seq[w] = {"file": b["file"], "frames": b["frames"], "duration": b["duration"], "fps": b["fps"]}
        seq["planned"] = False
        if rep["built"].get("opening", {}).get("cold_open_srt"):
            seq["cold_open_srt"] = rep["built"]["opening"]["cold_open_srt"]
    doc["sequences"] = seq
    ctx["seq"] = seq
    return ctx


def final_finish(ep: Episode, ctx: dict, final_doc: dict, body: Path, out: Path, preset: str, tag: str, report: dict) -> None:
    """Called after the body is rendered: join, verify, write the shifted planned SRT, and fold the programme's facts into `report`."""
    seq = ctx["seq"]
    opening = ep.root / seq["opening"]["file"] if seq.get("opening") else None
    closing = ep.root / seq["closing"]["file"] if seq.get("closing") else None
    pol = resolve_policy(ep)
    res = assemble(ep, body, opening, closing, out, preset, fade_s=pol["audio"]["join_fade_s"], tolerance_lu=pol["audio"]["loudness_tolerance_lu"])
    body.unlink(missing_ok=True)
    off = seq["opening"]["frames"] / _fpsf(final_doc["fps"]) if seq.get("opening") else 0.0
    cap = (final_doc.get("captions") or {})
    body_srt = ep.root / cap["srt"] if cap.get("srt") else None
    srt = programme_srt(ep, body_srt, ep.root / seq["cold_open_srt"] if seq.get("cold_open_srt") else None, off, ep.captions / f"{tag}.programme.srt")
    if not srt:
        seq["srt"] = None
    seq["opening_offset_s"] = off
    report["render"].update({k: res[k] for k in ("out", "duration", "lufs", "true_peak_db") if k in res})
    report["sequences"] = {"which": list(ctx["which"]), "draft": seq["draft"], "opening_offset_s": off, "joins_s": res["joins_s"], "frames": res["frames"],
                           "part_lufs": res["part_lufs"], "planned_srt": seq["srt"], "plan": seq["plan"]}
    report["warnings"] += ctx["warnings"] + res["warnings"]


# ====================================================================================================================
# THE RESOLVE HANDOFF HOOK (isolated: handoff.plan calls only this)
# ====================================================================================================================

def apply_to_handoff_plan(ep: Episode, plan_: dict, seq: dict) -> None:
    """Put the opening/closing on the timeline as clips: everything of the body moves later by the opening's frames, the opening starts at
    frame 0 (V1 + A1), the closing follows the body, the SRT is the shifted programme SRT. Files that are not built yet are warned about."""
    fps = plan_["fps"]
    built = {w: seq.get(w) for w in WHICH if seq.get(w) and (ep.root / seq[w]["file"]).exists()}
    for w in WHICH:
        if seq.get(w) and w not in built:
            plan_["warnings"].append(f"the {w} sequence file {seq[w]['file']} is missing: it is not on the timeline (ave sequence build)")
    if not built:
        return
    off = int(built["opening"]["frames"]) if "opening" in built else 0
    body_frames = plan_["timeline_frames"]
    lists = [plan_["video"], plan_["audio"], plan_["music"], plan_["overlays"]]
    layers = plan_.get("layers")
    if layers:
        lists += [layers["alt_video"], layers["audio_clips"]] + ([[layers["baked_master"]]] if layers.get("baked_master") else [])
    for lst in lists:
        for it in lst:
            it["record_frame"] += off

    def clip(w: str, rec: int):
        b = built[w]
        f = str((ep.root / b["file"]).resolve())
        v = {"id": f"seq-{w}", "file": f, "names": [Path(f).name], "start_frame": 0, "end_frame": b["frames"], "record_frame": rec, "frames": b["frames"],
             "track_index": 1, "media_type": 1, "zoom": 1.0, "crop": None, "reason": f"{w} sequence (ave sequence)"}
        a = {**v, "id": f"seq-{w}-audio", "media_type": 2}
        a.pop("names"), a.pop("zoom"), a.pop("crop")
        return v, a

    pre_v, pre_a, post_v, post_a = [], [], [], []
    if "opening" in built:
        v, a = clip("opening", 0)
        pre_v.append(v), pre_a.append(a)
    if "closing" in built:
        v, a = clip("closing", off + body_frames)
        post_v.append(v), post_a.append(a)
    plan_["video"] = pre_v + plan_["video"] + post_v
    if layers:
        layers["audio_clips"] = pre_a + layers["audio_clips"] + post_a
        layers["counts"]["video"]["1"] = layers["counts"]["video"].get("1", 0) + len(pre_v) + len(post_v)
        layers["counts"]["audio"]["1"] = layers["counts"]["audio"].get("1", 0) + len(pre_a) + len(post_a)
    else:
        plan_["audio"] = pre_a + plan_["audio"] + post_a
    plan_["timeline_frames"] = off + body_frames + (int(built["closing"]["frames"]) if "closing" in built else 0)
    plan_["import"] = sorted(set(plan_["import"]) | {x["file"] for x in pre_v + post_v})
    plan_["sequences"] = {w: {"file": b["file"], "frames": b["frames"]} for w, b in built.items()} | {"opening_offset_frames": off}
    srt = seq.get("srt")
    if srt and (ep.root / srt).exists():
        from . import handoff as _h
        sub = plan_.get("subtitles") or {"try": False, "copy": None}
        sub.update(srt=str((ep.root / srt).resolve()), cues=_h.srt_cue_count(ep.root / srt))
        plan_["subtitles"] = sub
