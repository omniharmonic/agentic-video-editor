"""Data and actions behind the UI. Every write goes through the same functions the CLI calls; nothing here shells out except jobs.py.

Methods return plain JSON-able dicts and raise ApiError(status, message) for anything the caller should see.
"""

from __future__ import annotations

import getpass
import threading
from pathlib import Path

from .. import clips as C
from .. import editdoc, progress, state, trim
from ..episode import Episode, read_json, write_json
from ..publish import queue, safety
from . import ledger
from .jobs import JobRefused, JobRunner


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def resolve_inside(ep: Episode, rel: str) -> Path:
    """A file under the episode root, or ApiError. Refuses traversal, symlink escapes, dot-files/dirs and the raw footage dirs."""
    if not rel or "\x00" in rel or rel.startswith(("/", "\\")):
        raise ApiError(404, "not found")
    parts = Path(rel).parts
    if any(p in ("..", "") or p.startswith(".") for p in parts):
        raise ApiError(404, "not found")
    root = ep.root.resolve()
    p = (root / rel).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        raise ApiError(404, "not found") from None
    for raw in ep.raw_dirs:
        try:
            p.relative_to(raw.resolve())
            raise ApiError(403, "raw footage is not served")
        except ValueError:
            pass
    if not p.is_file():
        raise ApiError(404, "not found")
    return p


def _servable(ep: Episode, p: Path) -> str | None:
    """`/files/...` URL for a path if the file server would serve it, else None."""
    try:
        rel = p.resolve().relative_to(ep.root.resolve()).as_posix()
        resolve_inside(ep, rel)
    except (ValueError, ApiError, OSError):
        return None
    return "/files/" + rel


def _need(body: dict, key: str, kind=str):
    v = body.get(key)
    if not isinstance(v, kind) or (kind is str and not v.strip()):
        raise ApiError(400, f"{key} is required")
    return v


class App:
    def __init__(self, ep: Episode, by: str | None = None, publishers=None):
        self.ep = ep
        self.by = by or getpass.getuser()
        self.publishers = publishers          # tests inject fakes; None = the real adapters
        self.jobs = JobRunner(ep)
        self.write_lock = threading.Lock()
        self._sent_cache: dict = {}

    # ------------------------------------------------------------------ dashboard
    def dashboard(self) -> dict:
        ep = self.ep
        st = state.status(ep)
        meta = read_json(ep.meta_path)
        waiting = []
        pend = (meta.get("review") or {}).get("pending")
        if pend:
            waiting.append({"kind": "review", "text": pend})
        for q in progress.open_questions(ep) + progress.derived_questions(ep):
            waiting.append({"kind": "question", "text": q})
        data = C.load_clips(ep)
        n_prop = sum(1 for c in (data or {}).get("clips", []) if c["status"] == "proposed" and not c.get("stale"))
        if n_prop:
            waiting.append({"kind": "clips", "text": f"{n_prop} clip proposal(s) await a keep/maybe/skip decision", "page": "clips"})
        n_pub = sum(1 for i in queue.list_items(ep) if i["status"] == "awaiting_approval")
        if n_pub:
            waiting.append({"kind": "publish", "text": f"{n_pub} publish item(s) await approval", "page": "publish"})
        return {"title": meta.get("title"), "path": str(ep.root), "stages": st["stages"], "next": st["next"], "edits": st["edits"],
                "sessions": st["sessions"], "waiting": waiting, "recent_log": progress.recent_log(ep, 12),
                "progress_file": str(progress.path(ep)), "ledger_backend": ledger.backend(),
                "clip_counts": self._clip_counts(data), "actions": self._safe_actions(st, data), "speakers": self._speaker_names()}

    def _clip_counts(self, data: dict | None) -> dict:
        """Per-session clip tallies for the board: how many proposed / kept / maybe / skipped, and how many have a rendered preview."""
        previews = self._previews()
        out: dict = {}
        for c in (data or {}).get("clips", []):
            row = out.setdefault(c.get("session") or "main", {"total": 0, "proposed": 0, "approved": 0, "maybe": 0, "rejected": 0, "previews": 0})
            row["total"] += 1
            key = "maybe" if c["status"] == "proposed" and c.get("maybe") else c["status"]
            row[key] = row.get(key, 0) + 1
            row["previews"] += 1 if self._preview_for(c["id"], previews) else 0
        return out

    def _safe_actions(self, st: dict, data: dict | None) -> list[dict]:
        """One-click jobs worth offering now. Every one is on the jobs allowlist (read-only or derived files; nothing sends or approves)."""
        acts: list[dict] = []
        sess = sorted({c.get("session") for c in (data or {}).get("clips", []) if c.get("session")})
        counts = self._clip_counts(data)
        for sid in sess:
            row = counts.get(sid) or {}
            if row.get("previews", 0) < row.get("total", 0):
                acts.append({"cmd": "clips previews", "args": {"name": sid, "top": 20}, "label": f"Render previews for the best 20 of {sid}",
                             "why": f"{row.get('previews', 0)} of {row.get('total', 0)} clips have a preview so far"})
        if not data and any(s["stage"] == "selects" for s in st["stages"]) and not any(s["stage"] == "transcribe" and not s["done"] for s in st["stages"]):
            acts.append({"cmd": "clips prepare", "args": {}, "label": "Find clip candidates", "why": "no work/clips.json yet"})
        acts.append({"cmd": "trim suggest", "args": {}, "label": "Suggest where each session really starts and ends", "why": "proposes only; you set the trim"})
        acts.append({"cmd": "status", "args": {}, "label": "Refresh pipeline status", "why": "reads disk, changes nothing"})
        return acts

    # ------------------------------------------------------------------ clips
    def _clip_media(self, c: dict, data: dict) -> dict | None:
        ep = self.ep
        rv = ep.exports / "review"
        drafts = [rv / "drafts" / f"{c['id']}.mp4"] + ([rv / c["session"] / "drafts" / f"{c['id']}.mp4"] if c.get("session") else [])
        for d in drafts:
            url = _servable(ep, d) if d.is_file() else None
            if url:
                return {"url": url, "kind": "draft", "t0": 0.0, "t1": None}
        src = c.get("source") or data.get("source") or {}
        if src.get("stage") and src.get("version"):
            stem = editdoc.path_for(ep, src["stage"], src["version"], src.get("name")).name[:-len(".json")]
            found = sorted(ep.exports.glob(f"{stem}.*"), key=lambda p: (".proxy." not in p.name, p.name)) if ep.exports.is_dir() else []
            for f in found:
                if f.suffix.lower() in (".mp4", ".mov", ".m4v", ".webm"):
                    url = _servable(ep, f)
                    if url:
                        return {"url": url, "kind": "edit proxy" if ".proxy." in f.name else "edit render", "t0": c["start"], "t1": c["end"]}
        return None

    # ---- clip data helpers
    def _sentences(self, session: str | None) -> list[dict]:
        """The session's sentence table from `ave clips prepare` (edit-output time), cached by mtime. [] when not prepared."""
        p = self.ep.artifact(C.candidates_file(session))
        try:
            mt = p.stat().st_mtime
        except OSError:
            return []
        hit = self._sent_cache.get(session)
        if hit and hit[0] == mt:
            return hit[1]
        try:
            sents = read_json(p).get("sentences") or []
        except (OSError, ValueError):
            sents = []
        self._sent_cache[session] = (mt, sents)
        return sents

    def _previews(self) -> dict:
        p = self.ep.work / "clips" / "previews" / "index.json"
        try:
            data = read_json(p)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _preview_for(self, clip_id: str, index: dict) -> dict | None:
        """The G2 preview contract: an mp4 with clip_in_s/clip_out_s inside it. None unless the file is really servable."""
        e = index.get(clip_id)
        if not isinstance(e, dict) or not e.get("file"):
            return None
        base = self.ep.work / "clips" / "previews"
        url = _servable(self.ep, base / str(e["file"]))
        if not url:
            return None
        tight = _servable(self.ep, base / str(e["tight_file"])) if e.get("tight_file") else None

        def num(k, default=0.0):
            v = e.get(k)
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default

        return {"url": url, "tight_url": tight, "pad_before_s": num("pad_before_s"), "pad_after_s": num("pad_after_s"),
                "clip_in_s": num("clip_in_s"), "clip_out_s": num("clip_out_s", None), "edit": e.get("edit"), "rendered_at": e.get("rendered_at")}

    def _speaker_names(self) -> dict:
        try:
            sp = read_json(self.ep.meta_path).get("speakers") or {}
        except (OSError, ValueError):
            return {}
        return {k: (v.get("name") if isinstance(v, dict) else None) or k for k, v in sp.items()}

    @staticmethod
    def _ranks(all_clips: list[dict]) -> dict[str, int]:
        """clips.json `rank` (1 = best in its session) when K wrote it, else computed the same way from the score."""
        out: dict[str, int] = {}
        by_s: dict = {}
        for c in all_clips:
            by_s.setdefault(c.get("session"), []).append(c)
        for rows in by_s.values():
            for n, c in enumerate(sorted(rows, key=lambda c: -(c.get("total") or 0)), 1):
                out[c["id"]] = c["rank"] if isinstance(c.get("rank"), int) else n
        return out

    def _completeness(self, c: dict) -> dict:
        """start_complete / end_complete from K's fields, else derived from the sentence table (starts_mid_thought, ends_complete)."""
        start, end = c.get("start_complete"), c.get("end_complete")
        note = c.get("completeness_note")
        if start is None or end is None:
            by_id = {s["id"]: s for s in self._sentences(c.get("session"))}
            ids = c.get("sentence_ids") or []
            f0 = (by_id.get(ids[0]) or {}).get("features") if ids else None
            f1 = (by_id.get(ids[-1]) or {}).get("features") if ids else None
            if start is None and f0 is not None:
                start = not f0.get("starts_mid_thought", False)
            if end is None:
                eos = c.get("ends_on_sentence")
                if f1 is not None:
                    end = eos is not False and bool(f1.get("ends_complete", True))
                elif eos is not None:
                    end = bool(eos)
            if note is None:
                bits = ([] if start is not False else ["starts mid-thought"]) + ([] if end is not False else ["may end before the thought is finished"])
                note = "; ".join(bits) or None
        return {"start_complete": start, "end_complete": end, "completeness_note": note}

    def _clip_row(self, c: dict, latest: dict, ranks: dict, previews: dict) -> dict:
        d = latest.get(c["id"]) or {}
        status = "maybe" if c["status"] == "proposed" and c.get("maybe") else c["status"]
        comp = self._completeness(c)
        tighten = c.get("tighten") or []
        return {"id": c["id"], "title": c.get("title"), "session": c.get("session"), "status": status, "score": c.get("total"),
                "rank": ranks.get(c["id"]), "start": c["start"], "end": c["end"], "duration": c.get("duration"), "hook": c.get("hook"),
                "tags": c.get("tags") or [], "warnings": c.get("warnings") or [], "stale": bool(c.get("stale")),
                "speakers": c.get("speakers") or [], "start_complete": comp["start_complete"], "end_complete": comp["end_complete"],
                "tight_saves_s": round(sum(float(t.get("saves_s") or 0) for t in tighten), 2) if tighten else 0,
                "has_preview": self._preview_for(c["id"], previews) is not None,
                "verdict": {"verdict": d.get("verdict"), "reasons": d.get("reasons") or [], "note": d.get("note"), "by": d.get("by"),
                            "source": d.get("source")} if d else None}

    @staticmethod
    def _num_param(qs: dict, key: str, kind=float, lo: float = 0.0):
        raw = (qs.get(key) or [None])[0]
        if raw in (None, ""):
            return None
        try:
            v = kind(raw)
        except ValueError:
            raise ApiError(400, f"{key} must be a number") from None
        if v < lo:
            raise ApiError(400, f"{key} must be at least {lo:g}")
        return v

    SORTS = ("score", "time", "duration", "rank")
    STATUSES = ("proposed", "maybe", "approved", "rejected")

    def clips(self, qs: dict | None = None) -> dict:
        """The review list. Query: session, tag, q (text), top (best N by score), min_score, status (comma list), unreviewed=1, sort.

        Order of operations: scope (session, tag, q) -> `of`; min_score then top N by score -> the selection;
        status / unreviewed narrow the selection for display -> `shown`. `progress` counts decided clips in the selection.
        """
        qs = qs or {}

        def one(k):
            return (qs.get(k) or [None])[0] or None

        top = self._num_param(qs, "top", int, 0)
        min_score = self._num_param(qs, "min_score", float, 0.0)
        sort = one("sort") or "score"
        if sort not in self.SORTS:
            raise ApiError(400, f"sort must be one of {', '.join(self.SORTS)}")
        statuses = [x for x in (one("status") or "").split(",") if x]
        if any(x not in self.STATUSES for x in statuses):
            raise ApiError(400, f"status must be drawn from {', '.join(self.STATUSES)}")
        data = C.load_clips(self.ep) or {"clips": []}
        latest = ledger.latest_decisions(self.ep)
        allc = data.get("clips", [])
        if one("stale") != "1":      # a re-prepare marks superseded clips stale; they are history, not review work
            allc = [c for c in allc if not c.get("stale") or c.get("status") != "proposed"]
        ranks, previews = self._ranks(allc), self._previews()
        rows_all = [self._clip_row(c, latest, ranks, previews) for c in allc]
        sessions = [{"id": sid, "total": sum(1 for r in rows_all if r["session"] == sid),
                     "decided": sum(1 for r in rows_all if r["session"] == sid and r["status"] != "proposed")}
                    for sid in sorted({r["session"] for r in rows_all if r["session"]})]
        sess, tag, text = one("session"), one("tag"), (one("q") or "").lower()
        texts = {c["id"]: ((c.get("title") or "") + " " + (c.get("text") or "")).lower() for c in allc} if text else {}
        scope = [r for r in rows_all if (not sess or r["session"] == sess) and (not tag or tag in r["tags"])
                 and (not text or text in texts[r["id"]])]
        tags: dict[str, int] = {}
        for r in scope:
            for t in r["tags"]:
                tags[t] = tags.get(t, 0) + 1
        sel = sorted(scope, key=lambda r: (r["score"] is None, -(r["score"] or 0), r["start"]))
        if min_score is not None:
            sel = [r for r in sel if (r["score"] or 0) >= min_score]
        if top:
            sel = sel[:top]
        view = sel
        if one("unreviewed") in ("1", "true"):
            view = [r for r in view if r["status"] == "proposed"]
        if statuses:
            view = [r for r in view if r["status"] in statuses]
        keyf = {"score": lambda r: (r["score"] is None, -(r["score"] or 0)), "rank": lambda r: (r["rank"] or 10**9, r["session"] or ""),
                "time": lambda r: (r["session"] or "", r["start"]), "duration": lambda r: -(r["duration"] or 0)}[sort]
        view = sorted(view, key=keyf)
        scores = [r["score"] for r in scope if r["score"] is not None]
        return {"clips": view, "of": len(scope), "shown": len(view), "selected": len(sel),
                "progress": {"done": sum(1 for r in sel if r["status"] != "proposed"), "total": len(sel),
                             "keep": sum(1 for r in sel if r["status"] == "approved"), "maybe": sum(1 for r in sel if r["status"] == "maybe"),
                             "skip": sum(1 for r in sel if r["status"] == "rejected")},
                "score_range": {"min": min(scores) if scores else 0, "max": max(scores) if scores else 0},
                "sessions": sessions, "tags": [{"tag": t, "n": n} for t, n in sorted(tags.items(), key=lambda kv: (-kv[1], kv[0]))],
                "reasons": ledger.reasons_vocabulary(), "reason_labels": ledger.reason_labels(), "ledger": ledger.backend(), "by": self.by,
                "previews_rendered": sum(1 for r in rows_all if r["has_preview"]), "clips_total": len(rows_all)}

    def clip_detail(self, clip_id: str) -> dict:
        """One clip with everything the review desk shows: media (preview contract + fallback), transcript window, tighten ranges."""
        data = C.load_clips(self.ep) or {"clips": []}
        c = next((x for x in data["clips"] if x["id"] == clip_id), None)
        if c is None:
            raise ApiError(404, f"no clip {clip_id!r}")
        previews = self._previews()
        row = self._clip_row(c, ledger.latest_decisions(self.ep), self._ranks(data["clips"]), previews)
        sents = self._sentences(c.get("session"))
        names = self._speaker_names()
        ids = set(c.get("sentence_ids") or [])
        pad = 40.0
        window = []
        for s in sents:
            if s["end"] < c["start"] - pad:
                continue
            if s["start"] > c["end"] + pad:
                break
            if s["id"] in ids:
                where = "in"
            else:
                where = "before" if s["end"] <= c["start"] + 0.01 else "after" if s["start"] >= c["end"] - 0.01 else "in"
            window.append({"id": s["id"], "start": s["start"], "end": s["end"], "speaker": s.get("speaker"),
                           "who": names.get(s.get("speaker"), s.get("speaker")), "text": s["text"], "where": where})
        before = [w for w in window if w["where"] == "before"][-3:]
        after = [w for w in window if w["where"] == "after"][:3]
        inside = [w for w in window if w["where"] == "in"]
        # tighten ranges are EPISODE seconds; the player runs on edit-output time, so map through the sentence table's e0
        in_s = [s for s in sents if s["id"] in ids]
        dur = c["end"] - c["start"]
        tighten = []
        for t in c.get("tighten") or []:
            try:
                ts, te = float(t["start"]), float(t["end"])
            except (KeyError, TypeError, ValueError):
                continue
            anchor = None
            for s in in_s:
                if s.get("e0") is not None and s["e0"] <= ts + 1e-6:
                    anchor = s
            rel = None
            hit: list[str] = []
            if anchor is not None:
                a0 = anchor["start"] + (ts - anchor["e0"]) - c["start"]
                rel = [max(0.0, a0), min(dur, a0 + (te - ts))]
                for w in inside:
                    ws, we = w["start"] - c["start"], w["end"] - c["start"]
                    if min(we, rel[1]) - max(ws, rel[0]) >= 0.5 * (we - ws):
                        hit.append(w["id"])
            tighten.append({"start": ts, "end": te, "kind": t.get("kind"), "text": t.get("text"), "saves_s": float(t.get("saves_s") or (te - ts)),
                            "reason": t.get("reason"), "confidence": t.get("confidence"), "rel": rel, "sentence_ids": hit})
        comp = self._completeness(c)
        fallback = self._clip_media(c, data)
        pv = self._preview_for(clip_id, previews)
        if pv:      # a nudge after rendering changes the clip's length: the preview still plays, but its dimming marks the old range
            pv["stale"] = pv["clip_out_s"] is not None and abs((pv["clip_out_s"] - pv["clip_in_s"]) - (c["end"] - c["start"])) > 0.3
        return {**row, "text": c.get("text"), "rationale": c.get("rationale"), "hook_quote": c.get("hook_quote"), "notes": c.get("notes") or [],
                "scores": (c.get("inputs") or {}).get("scores") or {}, "weighted": c.get("weighted") or {}, "penalties": c.get("penalties") or [],
                "complete_statement": c.get("complete_statement"), "completeness_note": comp["completeness_note"],
                "context_before": c.get("context_before") or " ".join(w["text"] for w in before[-2:]),
                "context_after": c.get("context_after") or " ".join(w["text"] for w in after[:2]),
                "tight_duration": c.get("tight_duration"), "tighten": tighten,
                "transcript": {"before": before, "inside": inside, "after": after, "available": bool(sents)},
                "media": {"preview": pv, "fallback": fallback, "state": "preview" if pv else (fallback["kind"] if fallback else "none")},
                "alternates": c.get("alternates") or []}

    def decide_clip(self, clip_id: str, body: dict) -> dict:
        verdict = body.get("verdict")
        if verdict not in (*ledger.VERDICTS, "pending", "nudge"):
            raise ApiError(400, f"verdict must be one of {', '.join(ledger.VERDICTS)} (pending undoes a first decision; nudge moves a boundary)")
        nudge = None
        if verdict == "nudge":
            nudge = body.get("nudge")
            ok = isinstance(nudge, dict) and set(nudge) <= {"start", "end"} and nudge and all(
                isinstance(v, int) and not isinstance(v, bool) and -6 <= v <= 6 for v in nudge.values())
            if not ok:
                raise ApiError(400, "nudge must be {start and/or end: whole sentences between -6 and 6}")
        reasons = [] if verdict in ("pending", "nudge") else (body.get("reasons") or [])
        vocab = set(ledger.reasons_vocabulary())
        if not isinstance(reasons, list) or any(r not in vocab for r in reasons):
            raise ApiError(400, f"reasons must be a list drawn from: {', '.join(sorted(vocab))}")
        note = body.get("note")
        if note is not None and (not isinstance(note, str) or len(note) > 2000):
            raise ApiError(400, "note must be text under 2000 characters")
        by = (body.get("by") or self.by).strip()[:80]
        with self.write_lock:
            data = C.load_clips(self.ep)
            clip = next((c for c in (data or {}).get("clips", []) if c["id"] == clip_id), None)
            if clip is None:
                raise ApiError(404, f"no clip {clip_id!r}")
            try:
                if verdict == "pending":
                    item = {"id": clip_id, "decision": "pending"}
                elif verdict == "nudge":
                    item = {"id": clip_id, "decision": "nudge", "nudge": nudge}
                else:
                    item = {"id": clip_id, "verdict": verdict, "reasons": reasons, "note": note or None}
                res = C.decide(self.ep, [item], name=clip.get("session"), by=by, source="ui")
            except SystemExit as e:
                raise ApiError(409, str(e)) from None
            if res["errors"]:
                raise ApiError(409, res["errors"][0]["error"])
            clip = next(c for c in C.load_clips(self.ep)["clips"] if c["id"] == clip_id)
        return {"ok": True, "id": clip_id, "verdict": verdict, "status": clip["status"], "start": clip["start"], "end": clip["end"],
                "duration": clip.get("duration"), "score": clip.get("total")}

    # ------------------------------------------------------------------ trim
    def trim(self) -> dict:
        ep = self.ep
        meta = read_json(ep.meta_path)
        p = ep.artifact(trim.SUGGESTIONS)
        from .. import sessions
        return {"trim": meta.get("trim") or {}, "pending_questions": trim.pending_questions(ep),
                "suggestions": read_json(p) if p.exists() else {}, "sessions": sessions.session_ids(ep, meta), "main": trim.MAIN}

    def trim_set(self, body: dict) -> dict:
        session = _need(body, "session")
        i, o = body.get("in"), body.get("out")
        for v in (i, o):
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                raise ApiError(400, "in/out must be numbers (episode seconds)")
        try:
            with self.write_lock:
                rec = trim.set_trim(self.ep, session, in_s=i, out_s=o, by=(body.get("by") or self.by).strip()[:80])
        except SystemExit as e:
            raise ApiError(400, str(e)) from None
        return {"ok": True, "trim": rec}

    def trim_clear(self, body: dict) -> dict:
        try:
            with self.write_lock:
                rec = trim.clear_trim(self.ep, _need(body, "session"))
        except SystemExit as e:
            raise ApiError(400, str(e)) from None
        return {"ok": True, "trim": rec}

    # ------------------------------------------------------------------ content
    def content(self) -> dict:
        from ..content import manifest as M
        from ..content import settings
        ep = self.ep
        if not M.content_dir(ep).is_dir():
            return {"enabled": settings.enabled(), "docs": []}
        docs = []
        for did in M.doc_ids(ep):
            d = M.load_latest(ep, did)
            items = []
            for it in d["items"]:
                rv = it["review"]
                items.append({"id": it["id"], "template": it["template"], "style": it.get("style"), "format": it.get("format"),
                              "status": rv["status"], "by": rv.get("by"), "note": rv.get("note"), "critic": rv.get("critic"),
                              "slots": it.get("slots"), "platforms": it.get("platforms"),
                              "outputs": [{"file": o["file"], "sha256": o["sha256"], "alt_text": o.get("alt_text"),
                                           "url": _servable(ep, ep.root / o["file"])} for o in it.get("outputs", [])]})
            docs.append({"id": did, "version": M.list_versions(ep, did)[-1], "items": items})
        return {"enabled": settings.enabled(), "docs": docs}

    def content_decide(self, body: dict) -> dict:
        from ..content import review, settings
        if not settings.enabled():
            raise ApiError(409, "`ave content` is switched off on this install (docs/content.md)")
        dec = body.get("decision")
        if dec not in ("approve", "reject"):
            raise ApiError(400, "decision must be approve or reject (nudging stays a CLI/agent step)")
        d = {"id": _need(body, "id"), "decision": dec, "note": body.get("note") or ""}
        if body.get("override") is True:
            d["override"] = True
        try:
            with self.write_lock:
                r = review.decide(self.ep, [d], body.get("doc") or "main", (body.get("by") or self.by).strip()[:80])
        except SystemExit as e:
            raise ApiError(400, str(e)) from None
        if r["refused"]:
            raise ApiError(409, "; ".join(r["refused"][0]["problems"]))
        return {"ok": True, **{k: r.get(k) for k in ("applied", "version", "next")}}

    # ------------------------------------------------------------------ publish
    def _pub_view(self, item: dict, detail: bool = False) -> dict:
        ep = self.ep
        out = {k: item.get(k) for k in ("id", "status", "title", "platforms", "visibility", "schedule", "media_type", "sha256")}
        out["media_type"] = queue.media_type(item)
        if not detail:
            return out
        files = []
        for f, p in zip(item.get("files") or [item.get("video")], queue.resolve_files(ep, item)):
            files.append({"file": f, "exists": p.is_file(), "sha256": safety.sha256_file(p) if p.is_file() else None, "url": _servable(ep, p)})
        try:
            now = queue.current_sha(ep, item)
        except OSError:
            now = None
        out.update(files=files, current_sha256=now, description=item.get("description"), caption=item.get("caption"),
                   alt_texts=item.get("alt_texts"), ai_disclosure=item.get("ai_disclosure"), made_for_kids=item.get("made_for_kids"),
                   preflight_problems=item.get("preflight_problems") or [], approvals=item.get("approvals") or [],
                   events=(item.get("events") or [])[-12:], results=safety.redact_obj(item.get("results") or {}),
                   send_blockers=queue.send_blockers(ep, item) if item["status"] in ("approved", "publishing", "failed") else [])
        out["metadata"] = {k: item.get(k) for k in safety.META_FIELDS}
        return safety.redact_obj(out)

    def publish(self) -> dict:
        return {"items": [self._pub_view(i) for i in queue.list_items(self.ep)], "by": self.by}

    def publish_item(self, item_id: str) -> dict:
        try:
            return self._pub_view(queue.load(self.ep, item_id), detail=True)
        except (FileNotFoundError, ValueError) as e:
            raise ApiError(404, str(e)) from None

    def publish_approve(self, item_id: str, body: dict) -> dict:
        sha = _need(body, "confirm_sha").strip().lower()
        by = _need(body, "by").strip()
        if len(sha) < 8:
            raise ApiError(400, "type at least 8 characters of the file sha256")
        try:
            with self.write_lock:
                done = queue.approve(self.ep, item_id, by[:80], sha, publishers=self.publishers, source="ui")
        except FileNotFoundError as e:
            raise ApiError(404, str(e)) from None
        except ValueError as e:
            raise ApiError(409, str(e)) from None
        return {"ok": True, "id": done["id"], "status": done["status"], "approval": done["approvals"][-1],
                "note": "Approved. Nothing was sent: sending is a separate step (`ave publish run --yes`)."}

    def publish_reject(self, item_id: str, body: dict) -> dict:
        by = _need(body, "by").strip()
        try:
            with self.write_lock:
                item = queue.reject(self.ep, item_id, by[:80], str(body.get("reason") or "")[:500])
        except FileNotFoundError as e:
            raise ApiError(404, str(e)) from None
        except ValueError as e:
            raise ApiError(409, str(e)) from None
        return {"ok": True, "id": item["id"], "status": item["status"]}

    # ------------------------------------------------------------------ jobs
    def job_start(self, body: dict) -> dict:
        try:
            return self.jobs.start(body.get("cmd"), body.get("args"))
        except JobRefused as e:
            raise ApiError(400, str(e)) from None

    def job(self, jid: str) -> dict:
        try:
            return self.jobs.tail(jid)
        except KeyError:
            raise ApiError(404, "no such job") from None

    def job_catalog(self) -> dict:
        from .jobs import ALLOWED
        return {"allowed": {k: {a: (v[1] if isinstance(v[1], str) else list(v[1])) for a, v in spec["args"].items()} for k, spec in ALLOWED.items()},
                "jobs": self.jobs.list()}
