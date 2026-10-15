"""The OPT-IN Canva renderer: a two-phase handshake, because the Canva MCP lives in the agent harness and the CLI cannot call it.

    phase 1  `ave content render --renderer canva`   writes `<work>/content/canva_jobs/<job>.json`: Brand Template id, field values,
                                                     export format, target size, and the ordered MCP steps. Nothing is sent anywhere.
             HUMAN:  `! ave content canva-approve <ep> <job>`   (typed at a terminal, like `ave publish approve`)
             AGENT:  makes the MCP calls the job lists (skills/ave-canva-render), downloads the export AT ONCE (URLs expire in 24 h),
                     logging each call with `ave content canva-log`
    phase 2  `ave content canva-import <ep> <job> <file>`   freezes the exported file into the ContentDoc (hash, brand and license
                                                     provenance) as a NEW doc version in `draft`, so it goes through the normal review
                                                     and publish approval. A failed run is recorded with `canva-fail`, never silently
                                                     swapped for the in-house render.

`CanvaClient` is the protocol a live runner implements, one method per MCP operation; `run_job` drives any client through a job
(rate-limit aware). Tests use a fake client and fixture job files. No code here makes a network call.

Canva's policy: Brand Kit data and templates are never exported for non-Canva workflows. Sync is ONE WAY, from our tokens into Canva
(`brand_brief` prints what to enter in the Brand Kit by hand). Nothing is read out of Canva beyond a template's field names.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

from .. import motion
from ..episode import Episode, read_json, write_json
from . import manifest as M
from . import people
from . import registry as R
from . import render_motionkit as RM

RENDERER = "canva"
RENDERER_VERSION = "canva-mcp/1"
JOB_VERSION = 1
EXPORT_EXT = {"png": ".png", "jpg": ".jpg", "mp4": ".mp4"}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")

# MCP tool names as listed in the proposal (docs/proposals/agentic-content-pipeline.md section 9). UNVERIFIED against the live connector:
# the session that built this was not signed in to Canva (docs/field-notes/2026-10-14-canva-discovery.md). The agent confirms each
# name and argument schema with ToolSearch before the first call and records any difference with `canva-log`.
TOOLS = {"dataset": "get-brand-template-dataset", "autofill": "autofill-design", "resize": "resize-design", "export": "export-design"}
WRITE_STEPS = ("autofill", "resize", "export")


class CanvaError(RuntimeError):
    """A Canva step failed or a job cannot be built/imported. The message says what to do."""


class RateLimited(CanvaError):
    def __init__(self, wait_s: float, which: str):
        super().__init__(f"Canva {which} limit reached; wait {wait_s:.0f}s (or until tomorrow for the daily cap)")
        self.wait_s, self.which = wait_s, which


# ---- the client protocol: one method per MCP operation --------------------------------------------

@runtime_checkable
class CanvaClient(Protocol):
    def dataset(self, brand_template_id: str) -> dict[str, str]:
        """READ. Field name -> type ("text", "image", "chart") of a Brand Template (get-brand-template-dataset)."""

    def autofill(self, brand_template_id: str, fields: dict[str, dict], title: str) -> dict:
        """WRITE. Create a design from the template with these fields (autofill-design). Returns {"design_id", "edit_url"?}."""

    def resize(self, design_id: str, width: int, height: int) -> dict:
        """WRITE. A resized copy (resize-design). Returns {"design_id"}."""

    def export(self, design_id: str, fmt: str) -> dict:
        """WRITE. Export (export-design). Returns {"url"}; the URL expires in 24 h, so the caller downloads it immediately."""

    def download(self, url: str, dest: Path) -> Path:
        """Fetch the export to `dest` (a local file the caller then hashes and imports)."""


class RateLimiter:
    """Sliding windows for exports (per minute and per day) and autofills (per minute). `clock` is injectable for tests."""

    def __init__(self, limits: dict, clock: Callable[[], float] = time.time):
        self.limits, self.clock = limits, clock
        self.events: dict[str, list[float]] = {"export": [], "autofill": []}

    def _caps(self, kind: str) -> list[tuple[str, int, float]]:
        L = self.limits
        if kind == "export":
            return [("export per minute", int(L.get("export_per_minute", 20)), 60.0), ("export per day", int(L.get("export_per_day", 500)), 86400.0)]
        return [("autofill per minute", int(L.get("autofill_per_minute", 60)), 60.0)]

    def check(self, kind: str) -> None:
        now = self.clock()
        for name, cap, win in self._caps(kind):
            recent = [t for t in self.events[kind] if now - t < win]
            if len(recent) >= cap:
                raise RateLimited(win - (now - min(recent)), name)

    def record(self, kind: str) -> None:
        self.events[kind].append(self.clock())


# ---- config ---------------------------------------------------------------------------------------

def canva_cfg(cfg: dict) -> dict:
    return (cfg.get("renderers") or {}).get("canva") or {}


def template_cfg(cfg: dict, template_id: str) -> dict:
    c = canva_cfg(cfg)
    if not c.get("enabled"):
        raise CanvaError("The Canva renderer is off. It is opt-in: set renderers.canva.enabled: true and map your Brand Templates in "
                         "~/.config/ave/content.yaml (see config/content.yaml). The in-house renderer needs none of this.")
    t = (c.get("templates") or {}).get(template_id)
    if not t or not (t.get("brand_template_id") or t.get("brand_template_ids")) or not t.get("field_map"):
        raise CanvaError(f"Template {template_id!r} has no Canva mapping. Add renderers.canva.templates.{template_id}: "
                         "{brand_template_id (or brand_template_ids per format), field_map: {slot: CANVA_FIELD}} to ~/.config/ave/content.yaml.")
    return t


def _brand_template_id(t: dict, fmt: str, template_id: str) -> str:
    if t.get("brand_template_ids"):
        bid = t["brand_template_ids"].get(fmt)
        if not bid:
            raise CanvaError(f"{template_id}: no Brand Template mapped for format {fmt} (have: {', '.join(t['brand_template_ids'])})")
        return bid
    return t["brand_template_id"]


# ---- jobs -----------------------------------------------------------------------------------------

def jobs_dir(ep: Episode) -> Path:
    return M.content_dir(ep) / "canva_jobs"


def job_path(ep: Episode, job_id: str) -> Path:
    if not _ID.match(job_id):
        raise CanvaError(f"bad job id {job_id!r}")
    return jobs_dir(ep) / f"{job_id}.json"


def load_job(ep: Episode, job_id: str) -> dict:
    p = job_path(ep, job_id)
    if not p.is_file():
        have = ", ".join(sorted(x.stem for x in jobs_dir(ep).glob("*.json"))) if jobs_dir(ep).is_dir() else "none"
        raise CanvaError(f"no Canva job {job_id!r} (have: {have})")
    return read_json(p)


def save_job(ep: Episode, job: dict) -> Path:
    p = job_path(ep, job["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    write_json(p, job)
    return p


def list_jobs(ep: Episode) -> list[dict]:
    if not jobs_dir(ep).is_dir():
        return []
    return [read_json(p) for p in sorted(jobs_dir(ep).glob("*.json"))]


def _slot_text(slots: dict, slot: str) -> str:
    """The text a mapped slot fills in. `role` is the old name of `title` (the line under the name), so a field mapped to either gets it."""
    if slot in ("role", "title"):
        return slots.get("title") or slots.get("role") or ""
    return slots.get(slot, "")


def _fields_sha(fields: dict) -> str:
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def build_job(ep: Episode, doc: dict, item: dict, reg: R.Registry, cfg: dict, *, export_format: str = "png") -> dict:
    """The job file for one item. Pure: reads the doc and config, writes nothing."""
    tpl = reg.get(item["template"])
    if tpl.kind == "carousel" or any(isinstance(v, list) for v in item["slots"].values()):
        raise CanvaError(f"{item['id']}: carousels are not supported by the Canva renderer yet (multi-page autofill is unverified); use the in-house renderer")
    c = canva_cfg(cfg)
    t = template_cfg(cfg, item["template"])
    if export_format not in (c.get("export_formats") or list(EXPORT_EXT)):
        raise CanvaError(f"export format {export_format!r} is not allowed (config renderers.canva.export_formats: {c.get('export_formats')})")
    if export_format == "mp4" and item["template"] != "reel-card":
        raise CanvaError("mp4 export is for animated reel cards; stills export as png or jpg")
    slots = R.effective_slots(tpl.slots, item["slots"], people.load_speakers(ep))
    fmap, props = t["field_map"], tpl.slots.get("properties", {})
    unknown = [s for s in fmap if s not in props]
    if unknown:
        raise CanvaError(f"{item['template']}: field_map names slots the template does not have: {unknown}")
    fields, warnings = {}, []
    for slot, cfield in fmap.items():
        rule = props[slot]
        if rule.get("x-asset") or rule.get("type") != "string":
            raise CanvaError(f"{item['template']}.{slot}: only text slots can be autofilled in v1 (image fields need an asset upload, which is a separate approved write)")
        fields[cfield] = {"type": "text", "text": _slot_text(slots, slot)}
    mapped = set(fmap) | ({"title", "role"} if fmap.keys() & {"title", "role"} else set())
    for slot, val in slots.items():
        if slot not in mapped and val and not isinstance(val, list):
            warnings.append(f"slot {slot!r} has a value but no field_map entry: it will NOT appear in the Canva design")
    w, h = tpl.size(item["format"])
    native = t.get("native_format") or item["format"]
    if t.get("brand_template_ids"):
        native = item["format"]
    bid = _brand_template_id(t, item["format"], item["template"])
    steps = [{"n": 1, "op": "dataset", "tool": TOOLS["dataset"], "writes": False,
              "args": {"brand_template_id": bid}, "check": f"every autofill field name exists in the dataset with type text: {sorted(fields)}"},
             {"n": 2, "op": "autofill", "tool": TOOLS["autofill"], "writes": True,
              "args": {"brand_template_id": bid, "data": fields, "title": f"ave {ep.root.name} {item['id']}"},
              "note": "creates a NEW design in the creator's Canva; the template itself is never edited"}]
    if native != item["format"]:
        cw, ch = tpl.size(native) if native in tpl.formats else (None, None)
        steps.append({"n": len(steps) + 1, "op": "resize", "tool": TOOLS["resize"], "writes": True,
                      "args": {"design_id": "<from step 2>", "width": w, "height": h},
                      "note": f"the Brand Template is authored at {native}{f' ({cw}x{ch})' if cw else ''}; the item needs {item['format']} ({w}x{h})"})
    steps.append({"n": len(steps) + 1, "op": "export", "tool": TOOLS["export"], "writes": True,
                  "args": {"design_id": "<from the previous write step>", "format": export_format},
                  "note": "download the returned URL IMMEDIATELY (it expires in 24 h) to the job's `download_to` path; never store the URL"})
    fields_sha = _fields_sha({"fields": fields, "template": bid, "format": item["format"], "export": export_format})
    job_id = f"{item['id']}-{fields_sha[:8]}"
    return {
        "version": JOB_VERSION, "kind": "canva-job", "id": job_id, "status": "pending-approval",
        "episode": doc["episode"], "doc": {"id": doc["id"], "version": (doc.get("saved") or {}).get("version")},
        "item": item["id"], "template": item["template"], "format": item["format"], "target": {"w": w, "h": h},
        "brand_template_id": bid, "kit_id": c.get("kit_id"), "field_map": dict(fmap), "fields": fields, "fields_sha256": fields_sha,
        "export": {"format": export_format}, "steps": steps, "warnings": warnings,
        "download_to": ep.rel(M.content_dir(ep) / "canva_exports" / f"{job_id}{EXPORT_EXT[export_format]}"),
        "limits": c.get("limits") or {}, "tools_verified": False,
        "approval": {"required": True, "by": None, "at": None},
        "log": [], "created_at": M.now(),
        "rules": ["Make NO Canva write before the human has approved this job (`! ave content canva-approve`).",
                  "Never edit, delete or publish an existing design or a Brand Template; only the steps above.",
                  "Log every call with `ave content canva-log`. On any failure stop and run `ave content canva-fail`."],
    }


def write_jobs(ep: Episode, doc: dict, cfg: dict, *, item_ids: list[str] | None = None, export_format: str = "png",
               reg: R.Registry | None = None) -> dict:
    """Phase 1. Job files for the draft items (or the named ones). Items the Canva renderer cannot take are reported, not guessed."""
    reg = reg or R.load_registry(cfg=cfg)
    report = {"jobs": [], "skipped": [], "failed": []}
    for it in doc["items"]:
        if item_ids is not None and it["id"] not in item_ids:
            continue
        if it["review"]["status"] == "rejected":
            report["skipped"].append({"id": it["id"], "why": "rejected"})
            continue
        if it["review"]["status"] == "approved" and not item_ids:
            report["skipped"].append({"id": it["id"], "why": "approved (name it to redo it in Canva; that voids the approval)"})
            continue
        if item_ids is None and not R.canva_configured(cfg, it["template"]):
            report["skipped"].append({"id": it["id"], "why": "template has no Canva mapping (in-house renderer stays)"})
            continue
        try:
            job = build_job(ep, doc, it, reg, cfg, export_format=export_format)
        except CanvaError as e:
            report["failed"].append({"id": it["id"], "error": str(e)})
            continue
        p = job_path(ep, job["id"])
        if p.is_file():
            old = read_json(p)
            report["jobs"].append({"id": old["id"], "item": old["item"], "status": old["status"], "file": ep.rel(p), "reused": True})
            continue
        save_job(ep, job)
        report["jobs"].append({"id": job["id"], "item": job["item"], "status": job["status"], "file": ep.rel(p), "warnings": job["warnings"],
                               "writes": [s["tool"] for s in job["steps"] if s["writes"]]})
    return report


def approve_job(ep: Episode, job_id: str, by: str, confirm: str) -> dict:
    """Record the human's approval of the writes in a job. `confirm` is the first 8 characters of the job's fields hash."""
    job = load_job(ep, job_id)
    if job["status"] != "pending-approval":
        raise CanvaError(f"job {job_id} is {job['status']}, not pending approval")
    if not confirm or not job["fields_sha256"].startswith(confirm.strip().lower()) or len(confirm.strip()) < 8:
        raise CanvaError("confirmation does not match: type the first 8 characters of fields_sha256 shown above")
    job["status"] = "approved"
    job["approval"] = {"required": True, "by": by, "at": M.now()}
    save_job(ep, job)
    return job


def log_call(ep: Episode, job_id: str, tool: str, ok: bool, summary: str = "", design_id: str | None = None) -> dict:
    """Append a call record. A WRITE tool is refused in the log unless the job was approved first."""
    job = load_job(ep, job_id)
    if tool in {TOOLS[s] for s in WRITE_STEPS} and job["status"] not in ("approved", "running"):
        raise CanvaError(f"{tool} is a Canva write and job {job_id} is {job['status']}: the human has not approved it")
    job["log"].append({"at": M.now(), "tool": tool, "ok": bool(ok), "summary": summary[:500], **({"design_id": design_id} if design_id else {})})
    if job["status"] == "approved":
        job["status"] = "running"
    if design_id:
        job["design_id"] = design_id
    save_job(ep, job)
    return job


def fail_job(ep: Episode, doc: dict, job_id: str, reason: str) -> tuple[dict, dict]:
    """Record a failed run on the item (provenance.canva.fallback) so the fall back to the in-house renderer is never silent.
    Returns (new_doc, job). The item keeps its renderer; the next step is `ave content render --item <id>`."""
    job = load_job(ep, job_id)
    if job["status"] == "imported":
        raise CanvaError(f"job {job_id} already imported; nothing to fail")
    job["status"], job["failure"] = "failed", {"reason": reason, "at": M.now()}
    save_job(ep, job)
    new = M.clone(doc)
    it = M.find_item(new, job["item"])
    it["provenance"]["canva"] = {**(it["provenance"].get("canva") or {}), "fallback": {"job": job_id, "reason": reason, "to": RM.RENDERER, "at": M.now()}}
    return new, job


def import_export(ep: Episode, doc: dict, job_id: str, file: Path, reg: R.Registry, *, design_id: str | None = None) -> tuple[dict, dict]:
    """Phase 2. Freeze the exported file into a NEW doc version as a draft item. Returns (new_doc, report); the caller saves the doc."""
    from ..publish import safety
    job = load_job(ep, job_id)
    if job["status"] == "imported":
        raise CanvaError(f"job {job_id} was already imported ({job['import']['file']})")
    if job["status"] not in ("approved", "running"):
        raise CanvaError(f"job {job_id} is {job['status']}: import needs an approved job that the agent ran")
    file = Path(file).expanduser()
    if not file.is_file():
        raise CanvaError(f"exported file not found: {file}")
    fmt = job["export"]["format"]
    if file.suffix.lower() not in (EXPORT_EXT[fmt], ".jpeg" if fmt == "jpg" else EXPORT_EXT[fmt]):
        raise CanvaError(f"job exports {fmt}, but {file.name} has the extension {file.suffix}")
    try:
        item = M.find_item(doc, job["item"])
    except KeyError as e:
        raise CanvaError(str(e))
    tpl = reg.get(item["template"])
    live = _fields_sha({"fields": {cf: {"type": "text", "text": _slot_text(R.effective_slots(tpl.slots, item["slots"], people.load_speakers(ep)), sl)}
                                   for sl, cf in job["field_map"].items()},
                        "template": job["brand_template_id"], "format": item["format"], "export": job["export"]["format"]})
    if live != job["fields_sha256"]:
        raise CanvaError(f"the item's words/format changed since job {job_id} was made; the export no longer matches the plan. Make a new job.")
    w, h = job["target"]["w"], job["target"]["h"]
    if fmt in ("png", "jpg"):
        from PIL import Image
        with Image.open(file) as im:
            if im.size != (w, h):
                raise CanvaError(f"export is {im.size[0]}x{im.size[1]} but the item needs {w}x{h}: the resize step did not happen or used the wrong size")
    dest_dir = M.content_dir(ep) / "renders"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{item['id']}.canva{EXPORT_EXT[fmt]}"
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    shutil.copyfile(file, tmp)
    tmp.replace(dest)
    sha = safety.sha256_file(dest)
    new = M.clone(doc)
    it = M.find_item(new, item["id"])
    alts = it.get("alt_text")
    alt = alts if isinstance(alts, str) and alts else (alts[0] if isinstance(alts, list) and alts else RM.default_alt_text(it, R.effective_slots(tpl.slots, it["slots"], people.load_speakers(ep)))[0])
    platforms = it.get("platforms") or tpl.default_platforms or ["any"]
    it["renderer"] = RENDERER
    it["outputs"] = [{"platform": p, "w": w, "h": h, "file": ep.rel(dest), "sha256": sha, "alt_text": alt} for p in platforms]
    it["review"] = {"status": "draft", "by": None, "at": None, "note": it["review"].get("note", ""),
                    "critic": {"measured": False, "ok": True, "fits": None, "contrast": None, "contrast_ok": None, "text_size_ok": None,
                               "notes": ["Canva render: fit, contrast and text size are NOT measured (no DOM to measure). Look at the image at phone size before approving."]}}
    did = design_id or job.get("design_id")
    it["provenance"] = {**it["provenance"], "renderer_version": RENDERER_VERSION, "template_rev": tpl.rev(), "tokens_sha256": M.tokens_sha(),
                        "canva": {"design_id": did, "template_id": job["brand_template_id"], "job": job_id, "kit_id": job.get("kit_id"),
                                  "export_format": fmt, "sha256": sha, "imported_at": M.now(), "exported_from": file.name,
                                  "calls": [{k: c[k] for k in ("tool", "ok", "at")} for c in job["log"]],
                                  "license": {"source": "canva-brand-template", "terms": "Canva Content License; elements are the creator's own Brand Template, "
                                              "but any stock element inside it carries Canva's licence: UNVERIFIED for commercial social use",
                                              "ai_generated": False}}}
    job["status"] = "imported"
    job["import"] = {"file": ep.rel(dest), "sha256": sha, "at": M.now(), "design_id": did}
    save_job(ep, job)
    return new, {"item": item["id"], "file": ep.rel(dest), "sha256": sha, "w": w, "h": h, "design_id": did,
                 "next": "LOOK at the image (Read the file), then: ave content review <project>"}


# ---- driving a client (used by a live runner and by the tests' fake) ----------------------------------

def run_job(client: CanvaClient, ep: Episode, job_id: str, *, limiter: RateLimiter | None = None) -> dict:
    """Execute an APPROVED job through a client: dataset check, autofill, optional resize, export, download. Logs each call.
    Raises CanvaError (and logs the failure) on the first problem; never retries a write on its own."""
    job = load_job(ep, job_id)
    if job["status"] not in ("approved", "running"):
        raise CanvaError(f"job {job_id} is {job['status']}; the human must approve it first (`! ave content canva-approve`)")
    limiter = limiter or RateLimiter(job.get("limits") or {})
    design_id = job.get("design_id")
    dest = ep.root / job["download_to"]
    for st in job["steps"]:
        op = st["op"]
        try:
            if op == "dataset":
                ds = client.dataset(st["args"]["brand_template_id"])
                missing = [f for f, v in job["fields"].items() if f not in ds or ds[f] != v["type"]]
                if missing:
                    raise CanvaError(f"Brand Template {st['args']['brand_template_id']} lacks text field(s) {missing} (it has {sorted(ds)})")
                log_call(ep, job_id, st["tool"], True, f"fields ok: {sorted(job['fields'])}")
            elif op == "autofill":
                limiter.check("autofill")
                r = client.autofill(st["args"]["brand_template_id"], job["fields"], st["args"]["title"])
                limiter.record("autofill")
                design_id = r["design_id"]
                log_call(ep, job_id, st["tool"], True, "design created", design_id)
            elif op == "resize":
                r = client.resize(design_id, st["args"]["width"], st["args"]["height"])
                design_id = r["design_id"]
                log_call(ep, job_id, st["tool"], True, f"resized to {st['args']['width']}x{st['args']['height']}", design_id)
            elif op == "export":
                limiter.check("export")
                r = client.export(design_id, st["args"]["format"])
                limiter.record("export")
                dest.parent.mkdir(parents=True, exist_ok=True)
                client.download(r["url"], dest)                       # immediately: the URL is never stored
                log_call(ep, job_id, st["tool"], True, f"exported and downloaded to {job['download_to']}", design_id)
        except CanvaError as e:
            log_call(ep, job_id, st["tool"], False, str(e), design_id)
            raise
        except Exception as e:                                       # a client failure is a Canva failure, recorded, not swallowed
            log_call(ep, job_id, st["tool"], False, f"{type(e).__name__}: {e}", design_id)
            raise CanvaError(f"{st['tool']} failed: {type(e).__name__}: {e}") from e
    return {"job": job_id, "design_id": design_id, "file": str(dest)}


# ---- brand brief (one way: our tokens -> what to enter in Canva's Brand Kit by hand) ------------------

def _font_entry(role: str, f: dict) -> dict:
    """One brand font as the Brand Kit needs it: family, the bundled file(s) to upload, license, source."""
    d = motion.KIT / "brand" / "fonts"
    files = []
    for e in f.get("files", []):
        files.append({"file": e["file"], "path": str(d / e["file"]) if (d / e["file"]).exists() else None, "weight": e.get("weight", 400),
                      "bundled": (d / e["file"]).exists()})
    lic = [p.name for p in sorted(d.glob("OFL-*.txt")) if p.stem.split("-", 1)[1].lower() in f["family"].lower()]
    return {"family": f["family"], "role": role, "files": files, "license": "SIL Open Font License 1.1" if lic else "unknown",
            "license_file": lic[0] if lic else None, "source": "motion-kit/brand/fonts/SOURCES.md", "fallback": f["fallback"][:2]}


def brand_brief(ep: Episode | None = None) -> dict:
    """What to enter by hand in Canva's Brand Kit: the final palette and fonts, the names a card prints, and the card's layout rules."""
    tokens = json.loads((motion.KIT / "brand" / "tokens.json").read_text())
    styles = R.load_styles()
    speakers = people.load_speakers(ep)
    show = None
    if ep is not None:
        meta = read_json(ep.meta_path)
        show = meta.get("show")
        if not show and M.latest_path(ep):
            shows = [it["slots"].get("show") for it in M.load_latest(ep)["items"] if it["slots"].get("show")]
            show = max(set(shows), key=shows.count) if shows else None
    return {
        "direction": "ONE WAY: enter these in Canva's Brand Kit yourself. Nothing is read back out of Canva (its policy forbids exporting Brand Kit data).",
        "palette": tokens["colors"],
        "styles": {n: {role: f"{tokens['colors'][tok]} ({tok})" for role, tok in roles.items()} for n, roles in styles["styles"].items()},
        "fonts": {k: _font_entry(k, v) for k, v in tokens["fonts"].items()},
        "type_scale_px_at_1080": tokens["type"], "tracking_em": tokens["tracking"],
        "names": {"show": show, "speakers": [{"id": s["id"], "prints_as": s["display_name"], "title": s["title"] or None} for s in speakers],
                  "source": "episode.json speakers (display_name, else name; title, default CO-HOST for a host)" if speakers else
                            "no episode given: pass the project path to list its speakers"},
        "card_layout": {"quote_post": ["show name ONCE, small caps label at the top", "short rule", "the quote (serif)", "speaker name (sans)",
                                       "title beneath the name (small caps label): CO-HOST"],
                        "rule": "The show name appears once per graphic. Do not repeat it as a role, subtitle or attribution."},
        "canva_fields": "map QUOTE, BY (name), TITLE (the line under the name; the old name ROLE still works in field_map) and SHOW in config/content.yaml renderers.canva",
        "note": "Upload the bundled font files (they are SIL OFL, free to embed) as custom fonts in the Brand Kit; Canva Pro accepts font uploads. "
                "Variable fonts: if Canva rejects them, install the named instances you need. More Brand Kit assets will be added by the creator later."}
