"""Template registry: `content/templates/<id>/template.yaml` plus the style x format matrix.

A template declares its slots as JSON Schema (a small subset, validated here with messages an agent can act on), the formats
it supports, its styles, and a renderer entry per renderer (the default first). Styles are colour-role maps over
`motion-kit/brand/tokens.json` (`motion-kit/brand/styles.json`): a new style is a data change, never a new template.
Two schema extensions carry editorial rules so no code needs to know the templates:
  x-verbatim: true   the string is a QUOTE and must appear word for word in the item's source text
  x-asset: true      the string is a path to a media file that needs a license-log entry before rendering
  x-byline: true     the string names a person or role under a graphic; the show name is cut out of it (the show prints once)
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import yaml

from .. import config, motion
from . import settings

TEMPLATES_DIR = Path(os.environ.get("AVE_CONTENT_TEMPLATES", config.REPO_ROOT / "content" / "templates"))
STILLS_DIR = motion.KIT / "stills"
RENDERERS = ("motion-kit", "canva")


class RegistryError(ValueError):
    """A template.yaml is malformed. The message names the file and the fix."""


# ---- styles --------------------------------------------------------------------------------------

def load_styles() -> dict:
    """{'styles': {name: {role: token color name}}, 'type': {...}} straight from motion-kit/brand/styles.json."""
    return json.loads((motion.KIT / "brand" / "styles.json").read_text())


def style_names() -> list[str]:
    return list(load_styles()["styles"])


def resolve_style(name: str) -> dict:
    """Role -> hex colour for one style, resolved through tokens.json (the single source of truth for colour)."""
    st = load_styles()["styles"]
    if name not in st:
        raise ValueError(f"unknown style {name!r}. Available: {', '.join(st)}")
    colors = motion.load_tokens()["colors"]
    out = {}
    for role, token in st[name].items():
        if token not in colors:
            raise RegistryError(f"styles.json: style {name!r} role {role!r} names unknown color {token!r} (tokens.json has {', '.join(colors)})")
        out[role] = colors[token]
    return out


# ---- JSON-Schema subset ---------------------------------------------------------------------------

def _type_ok(kind: str, v: Any) -> bool:
    if kind == "string":
        return isinstance(v, str)
    if kind == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if kind == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if kind == "boolean":
        return isinstance(v, bool)
    if kind == "array":
        return isinstance(v, list)
    if kind == "object":
        return isinstance(v, dict)
    return True


def check_schema(schema: dict, value: Any, path: str = "slots") -> list[str]:
    """Problems of `value` against `schema` (type, enum, minLength, maxLength, minimum, maximum, minItems, maxItems, items,
    properties, required, additionalProperties). Reports every problem, each prefixed with the path to the offending slot."""
    p: list[str] = []
    kind = schema.get("type")
    if kind and not _type_ok(kind, value):
        return [f"{path}: must be {'an' if kind in ('array', 'object', 'integer') else 'a'} {kind}"]
    if "enum" in schema and value not in schema["enum"]:
        p.append(f"{path}: must be one of {', '.join(map(str, schema['enum']))} (got {value!r})")
    if isinstance(value, str):
        if len(value.strip()) < schema.get("minLength", 0):
            p.append(f"{path}: must not be empty" if schema["minLength"] <= 1 else f"{path}: at least {schema['minLength']} characters")
        if len(value) > schema.get("maxLength", 10 ** 9):
            p.append(f"{path}: {len(value)} characters, this template holds at most {schema['maxLength']}; choose a shorter passage (do not trim words out of a quote)")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            p.append(f"{path}: must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            p.append(f"{path}: must be <= {schema['maximum']}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            p.append(f"{path}: needs at least {schema['minItems']} items (has {len(value)})")
        if len(value) > schema.get("maxItems", 10 ** 9):
            p.append(f"{path}: at most {schema['maxItems']} items (has {len(value)})")
        for i, v in enumerate(value):
            p += check_schema(schema.get("items") or {}, v, f"{path}[{i}]")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in value:
                p.append(f"{path}.{k}: required")
        if schema.get("additionalProperties") is False:
            for k in value:
                if k not in props:
                    p.append(f"{path}.{k}: not a slot of this template (allowed: {', '.join(props) or 'none'})")
        for k, rule in props.items():
            if k in value:
                p += check_schema(rule, value[k], f"{path}.{k}")
    return p


def with_defaults(schema: dict, value: Any) -> Any:
    """`value` with schema defaults filled in (recursively); the input is not modified."""
    if isinstance(value, dict) and schema.get("type") == "object":
        out = copy.deepcopy(value)
        for k, rule in schema.get("properties", {}).items():
            if k in out:
                out[k] = with_defaults(rule, out[k])
            elif "default" in rule:
                out[k] = copy.deepcopy(rule["default"])
        return out
    if isinstance(value, list) and schema.get("items"):
        return [with_defaults(schema["items"], v) for v in value]
    return value


def _show_re(show: str):
    return re.compile(r"(?<!\w)" + re.escape(show.strip()) + r"(?!\w)", re.IGNORECASE)


def _strip_show(value: str, show: str) -> str:
    """`value` without the show name and the separators (comma, dash, dot, bar) left dangling around it."""
    out = _show_re(show).sub("", value)
    return re.sub(r"\s{2,}", " ", out).strip(" \t,;:|-\u2013\u2014\u00b7\u2022")


def _objects(schema: dict, value: Any, path: str = ""):
    """(path, schema object, dict value) for the slots object and every nested object / list item, parents first."""
    if isinstance(value, dict) and schema.get("type") == "object":
        yield path, schema, value
        for k, rule in schema.get("properties", {}).items():
            if k in value:
                yield from _objects(rule, value[k], f"{path}.{k}" if path else k)
    elif isinstance(value, list) and schema.get("items"):
        for i, v in enumerate(value):
            yield from _objects(schema["items"], v, f"{path}[{i}]")


def show_duplicates(slots: dict, schema: dict | None = None) -> list[str]:
    """Slots that print the show name a second time. The show has its own slot (set small at the top), so a role, attribution,
    title, subtitle or line that repeats it puts the name twice on one graphic.

    Without a schema: top-level text slots (other than `show`) equal to the show name. With the template's schema this also finds
    nested slots (a carousel slide's attribution) and, in slots marked `x-byline`, the show name inside a longer string
    ("Maren, The Long Table"). Quotes (`x-verbatim`) are never touched: they are the speaker's words. Returns slot paths."""
    if not isinstance(slots, dict):
        return []
    show = str(slots.get("show") or "").strip()
    if not show:
        return []
    if schema is None:
        return [k for k, v in slots.items() if k != "show" and isinstance(v, str) and v.strip().casefold() == show.casefold()]
    out = []
    rx = _show_re(show)
    for path, sch, obj in _objects(schema, slots):
        for k, v in obj.items():
            rule = sch.get("properties", {}).get(k) or {}
            if not isinstance(v, str) or rule.get("x-verbatim") or (k == "show" and not path):
                continue
            if v.strip().casefold() == show.casefold() or (rule.get("x-byline") and rx.search(v)):
                out.append(f"{path}.{k}" if path else k)
    return out


def effective_slots(schema: dict, slots: dict, speakers: list[dict] | None = None) -> dict:
    """Slots as a renderer should print them.

    - schema defaults filled in;
    - the legacy `role` slot becomes `title` (the line under the name) when `title` is empty;
    - the show name appears once: a slot that repeats it is blanked, and in `x-byline` slots the show name is cut out of a longer string;
    - `speakers` (people.load_speakers): an attribution naming an episode speaker prints their display name, and the speaker's
      title (CO-HOST for a host) goes beneath it unless the slot already has one."""
    from . import people
    out = with_defaults(schema, slots)
    for _, sch, obj in _objects(schema, out):
        props = sch.get("properties", {})
        if "role" in props and "title" in props and obj.get("role"):
            if not obj.get("title"):
                obj["title"] = obj["role"]
            obj["role"] = ""
    show = str(out.get("show") or "").strip() if isinstance(out, dict) else ""
    if show:
        for _, sch, obj in _objects(schema, out):
            for k, v in list(obj.items()):
                rule = sch.get("properties", {}).get(k) or {}
                if not isinstance(v, str) or rule.get("x-verbatim") or obj is out and k == "show":
                    continue
                if v.strip().casefold() == show.casefold():
                    obj[k] = ""
                elif rule.get("x-byline") and _show_re(show).search(v):
                    obj[k] = _strip_show(v, show)
    if speakers:
        for _, sch, obj in _objects(schema, out):
            props = sch.get("properties", {})
            if "attribution" in props and "title" in props and obj.get("attribution"):
                obj["attribution"], obj["title"] = people.byline(speakers, obj["attribution"], obj.get("title") or "")
    return out


def normalize_slots(schema: dict, slots: dict, speakers: list[dict] | None = None) -> dict:
    """The stored slots with the corrections `effective_slots` would print, written back: the legacy `role` moves to `title`, a slot that
    repeats the show name is emptied, an episode speaker's display name and title (CO-HOST) are filled in. Quotes are never touched and
    slots that were not set stay unset (defaults are not written in)."""
    eff = effective_slots(schema, slots, speakers)
    full = with_defaults(schema, slots)
    out = copy.deepcopy(slots)

    def merge(stored: dict, e: dict, f: dict) -> None:
        for k, v in e.items():
            if isinstance(v, dict) and isinstance(stored.get(k), dict):
                merge(stored[k], v, f.get(k) or {})
            elif isinstance(v, list) and isinstance(stored.get(k), list):
                for sv, ev, fv in zip(stored[k], v, f.get(k) or []):
                    if isinstance(sv, dict):
                        merge(sv, ev, fv)
            elif v != f.get(k):
                stored[k] = v

    merge(out, eff, full)
    for _, sch, obj in _objects(schema, out):
        if "role" in sch.get("properties", {}) and "title" in sch.get("properties", {}) and obj.get("role") == "":
            del obj["role"]
    return out


def walk_flagged(schema: dict, value: Any, flag: str, path: str = "slots") -> list[tuple[str, Any, dict]]:
    """Every (path, string value, containing object) whose schema carries `flag: true`. The containing object lets a carousel
    slide name its own `source` while an item-level `source` covers the rest."""
    out: list[tuple[str, Any, dict]] = []

    def go(sch: dict, val: Any, pth: str, parent: dict | None) -> None:
        if isinstance(val, dict) and sch.get("type") == "object":
            for k, rule in sch.get("properties", {}).items():
                if k in val:
                    go(rule, val[k], f"{pth}.{k}", val)
        elif isinstance(val, list) and sch.get("items"):
            for i, v in enumerate(val):
                go(sch["items"], v, f"{pth}[{i}]", parent)
        elif sch.get(flag) and isinstance(val, str) and val.strip():
            out.append((pth, val, parent or {}))

    go(schema, value, path, None)
    return out


# ---- templates ------------------------------------------------------------------------------------

class Template:
    def __init__(self, path: Path, data: dict, cfg: dict):
        self.path = path
        self.id = data["id"]
        self.title = data.get("title", self.id)
        self.description = data.get("description", "")
        self.slots = data["slots"]
        self.formats: list[str] = list(data["formats"])
        self.styles: list[str] = list(data["styles"])
        self.renderers: dict = data["renderers"]
        self.default_platforms: list[str] = list(data.get("default_platforms") or [])
        self.kind = data.get("kind", "image")          # image | carousel | card
        self.cfg = cfg
        self.data = data

    @property
    def default_renderer(self) -> str:
        return next(iter(self.renderers))

    def size(self, fmt: str) -> tuple[int, int]:
        return settings.format_size(self.cfg, fmt)

    def still_dir(self, renderer: str = "motion-kit") -> Path:
        return STILLS_DIR / self.renderers[renderer]["template"]

    def rev(self) -> str:
        """Short hash of everything that changes how this template looks: its yaml, its page, the styles and the tokens."""
        h = hashlib.sha256()
        files = [self.path, motion.KIT / "brand" / "styles.json", motion.KIT / "brand" / "tokens.json",
                 motion.KIT / "lib" / "timeline.js", motion.KIT / "lib" / "base.css"]
        if "motion-kit" in self.renderers:
            files.append(self.still_dir() / "template.html")
        for f in files:
            h.update(f.read_bytes())
        return h.hexdigest()[:12]

    def check_slots(self, slots: Any) -> list[str]:
        return check_schema(self.slots, slots)

    def summary(self) -> dict:
        keep = ("type", "maxLength", "enum", "minItems", "maxItems", "x-verbatim", "x-asset", "x-byline", "default")
        return {"id": self.id, "title": self.title, "kind": self.kind, "formats": self.formats, "styles": self.styles,
                "renderers": list(self.renderers), "default_platforms": self.default_platforms,
                "slots": {k: {kk: vv for kk, vv in v.items() if kk in keep} for k, v in self.slots.get("properties", {}).items()},
                "required": self.slots.get("required", [])}


def canva_configured(cfg: dict, template_id: str) -> bool:
    """True when this install enabled the Canva renderer AND mapped this template to a Brand Template (config/content.yaml override)."""
    c = (cfg.get("renderers") or {}).get("canva") or {}
    t = (c.get("templates") or {}).get(template_id) or {}
    return bool(c.get("enabled") and (t.get("brand_template_id") or t.get("brand_template_ids")) and t.get("field_map"))


class Registry:
    def __init__(self, templates: dict[str, Template], cfg: dict):
        self.templates = templates
        self.cfg = cfg

    def ids(self) -> list[str]:
        return sorted(self.templates)

    def get(self, tid: str) -> Template:
        if tid not in self.templates:
            raise KeyError(f"unknown template {tid!r}. Available: {', '.join(self.ids())}")
        return self.templates[tid]

    def matrix(self) -> list[dict]:
        """Every valid (template, style, format) with its pixel size."""
        out = []
        for t in self.templates.values():
            for fmt in t.formats:
                w, h = t.size(fmt)
                out += [{"template": t.id, "style": s, "format": fmt, "w": w, "h": h} for s in t.styles]
        return out

    def check_item_shape(self, item: dict) -> list[str]:
        """template/style/format/renderer/slots of one item, against its template. No file access."""
        tid = item.get("template")
        if tid not in self.templates:
            return [f"template {tid!r} is not in the registry (have: {', '.join(self.ids())})"]
        t = self.templates[tid]
        p: list[str] = []
        if item.get("style") not in t.styles:
            p.append(f"style {item.get('style')!r} is not available for {tid} (have: {', '.join(t.styles)})")
        if item.get("format") not in t.formats:
            p.append(f"format {item.get('format')!r} is not available for {tid} (have: {', '.join(t.formats)})")
        if item.get("renderer") not in t.renderers and not (item.get("renderer") == "canva" and canva_configured(self.cfg, tid)):
            p.append(f"renderer {item.get('renderer')!r} is not available for {tid} (have: {', '.join(t.renderers)})")
        p += t.check_slots(item.get("slots"))
        return p


def _load_template(path: Path, cfg: dict, style_ok: set[str]) -> Template:
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        raise RegistryError(f"{path}: cannot read ({e})")
    if not isinstance(data, dict):
        raise RegistryError(f"{path}: must be a mapping")
    for k in ("id", "slots", "formats", "styles", "renderers"):
        if k not in data:
            raise RegistryError(f"{path}: missing `{k}`")
    if data["id"] != path.parent.name:
        raise RegistryError(f"{path}: id {data['id']!r} must equal its folder name {path.parent.name!r}")
    bad = [f for f in data["formats"] if f not in cfg["formats"]]
    if bad:
        raise RegistryError(f"{path}: unknown format(s) {bad}; config/content.yaml defines {list(cfg['formats'])}")
    bad = [s for s in data["styles"] if s not in style_ok]
    if bad:
        raise RegistryError(f"{path}: unknown style(s) {bad}; motion-kit/brand/styles.json defines {sorted(style_ok)}")
    bad = [r for r in data["renderers"] if r not in RENDERERS]
    if bad:
        raise RegistryError(f"{path}: unknown renderer(s) {bad}; known: {', '.join(RENDERERS)}")
    if "motion-kit" in data["renderers"]:
        d = STILLS_DIR / data["renderers"]["motion-kit"]["template"]
        if not (d / "template.html").is_file():
            raise RegistryError(f"{path}: motion-kit page not found at {d / 'template.html'}")
    return Template(path, data, cfg)


def load_registry(templates_dir: Path | None = None, cfg: dict | None = None) -> Registry:
    cfg = cfg or settings.load()
    root = Path(templates_dir or TEMPLATES_DIR)
    style_ok = set(style_names())
    out: dict[str, Template] = {}
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        if (d / "template.yaml").is_file():
            t = _load_template(d / "template.yaml", cfg, style_ok)
            out[t.id] = t
    if not out:
        raise RegistryError(f"no templates found under {root}")
    return Registry(out, cfg)
