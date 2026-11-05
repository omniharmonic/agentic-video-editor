"""The episode projects' own review work, for the Workspace: content items waiting for a decision (`ave content`), the episode's
publish queue (`ave publish`), Claude's trims, and background jobs (an allowlist of `ave` steps). Moved here from the retired
`ave ui`, which showed them one episode at a time; every act is the same library call the CLI makes, written as the person.

Gates are kept exactly as they were:
  * approving a content item is the device-owner's act: only the app's native sheet calls `content_decide(... "approve")`, after
    the macOS check (desktop_approvals). The HTTP route refuses an approval.
  * rejecting a content item or an episode publish item records the person, as `ave ui` did (no PIN, no check).
  * approving an episode publish item stays a terminal act (`ave publish approve`, which `require_human` guards). The sheet shows
    the exact line to run; nothing here approves one.
  * clearing a trim is the person's act through the app's native sheet, like `ave trim set`.
"""

from __future__ import annotations

import threading
from pathlib import Path

from ...episode import Episode
from ...jobs import ALLOWED, JobRefused, JobRunner
from ...publish import queue, safety
from .http import _servable


class EpisodeError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Episodes:
    def __init__(self, workspace, by: str):
        self.workspace = workspace
        self.by = by
        self.lock = threading.Lock()
        self.runners: dict[str, JobRunner] = {}

    # ---------------------------------------------------------------- which projects
    def _all(self) -> list[tuple[object, Episode]]:
        out = []
        for p in self.workspace.projects():
            if (p.root / "episode.json").is_file():
                out.append((p, Episode(p.root)))
        return out

    def _one(self, project_id: str):
        for p, ep in self._all():
            if p.project_id == project_id:
                return p, ep
        raise EpisodeError(404, "That project is not on this computer.")

    @staticmethod
    def _url(project_id: str, ep: Episode, path: Path) -> str | None:
        rel = _servable(ep, path)
        return f"/api/workspace/file/{project_id}/{rel[len('/files/'):]}" if rel else None

    # ---------------------------------------------------------------- content items (`ave content`)
    def content(self) -> list[dict]:
        """Every rendered content item that waits for a decision, across the episode projects on this computer."""
        from ...content import manifest as M, settings
        if not settings.enabled():
            return []
        rows = []
        for p, ep in self._all():
            if not M.content_dir(ep).is_dir():
                continue
            for did in M.doc_ids(ep):
                d = M.load_latest(ep, did)
                for it in d["items"]:
                    rv = it["review"]
                    if rv["status"] != "draft" or not it.get("outputs"):
                        continue
                    rows.append({"kind": "content", "project": p.project_id, "title": p.title or p.root.name, "doc": did, "id": it["id"],
                                 "template": it["template"], "style": it.get("style"), "format": it.get("format"),
                                 "critic": rv.get("critic"), "note": rv.get("note"), "platforms": it.get("platforms") or [],
                                 "slots": it.get("slots") or {},
                                 "outputs": [{"name": Path(o["file"]).name, "sha256": o["sha256"], "alt_text": o.get("alt_text"),
                                              "url": self._url(p.project_id, ep, ep.root / o["file"])} for o in it.get("outputs", [])]})
        return rows

    def content_decide(self, project_id: str, doc: str, item: str, decision: str, *, note: str = "", override: bool = False,
                       by: str | None = None) -> dict:
        from ...content import review, settings
        if not settings.enabled():
            raise EpisodeError(409, "Content is switched off on this computer (docs/content.md).")
        if decision not in ("approve", "reject"):
            raise EpisodeError(400, "Choose approve or reject.")
        _, ep = self._one(project_id)
        d = {"id": item, "decision": decision, "note": (note or "")[:2000]}
        if override is True:
            d["override"] = True
        try:
            with self.lock:
                r = review.decide(ep, [d], doc or "main", (by or self.by).strip()[:80])
        except SystemExit as e:
            raise EpisodeError(400, str(e)) from None
        if r["refused"]:
            raise EpisodeError(409, "; ".join(r["refused"][0]["problems"]))
        return {"ok": True, **{k: r.get(k) for k in ("applied", "version", "next")}}

    def approve_content(self, project_id: str, doc: str, item: str, *, note: str = "", override: bool = False,
                        shas: list[str] | None = None, by: str | None = None) -> dict:
        """The native sheet's approval: exactly the files it showed (refused if they changed since), with the person's note as
        the critic override when the critic flagged it, as the old "Approve despite the critic" did."""
        row = next((x for x in self.content() if (x["project"], x["doc"], x["id"]) == (project_id, doc, item)), None)
        if row is None:
            raise EpisodeError(409, "That content item is not waiting for a decision any more.")
        if shas is not None and sorted(o["sha256"] for o in row["outputs"]) != sorted(shas):
            raise EpisodeError(409, "The files changed since the sheet showed them. Nothing was approved. Look again.")
        return self.content_decide(project_id, doc, item, "approve", note=note, override=override, by=by)

    # ---------------------------------------------------------------- the episode publish queue (`ave publish`)
    def publish(self) -> list[dict]:
        """The episode publish items waiting for approval. Only the publishing computer has a queue; elsewhere, nothing."""
        rows = []
        for p, ep in self._all():
            try:
                view = queue.overview(ep)
            except (OSError, ValueError):
                continue
            if view["read_only"]:
                continue
            for it in queue.list_items(ep):
                if it["status"] != "awaiting_approval":
                    continue
                rows.append({"kind": "episode_post", "project": p.project_id, "title": p.title or p.root.name, "id": it["id"],
                             "name": it.get("title") or it["id"], "platforms": it.get("platforms") or [], "status": it["status"],
                             "schedule": it.get("schedule"), "folder": str(ep.root)})
        return rows

    def publish_item(self, project_id: str, item_id: str) -> dict:
        p, ep = self._one(project_id)
        try:
            item = queue.load(ep, item_id)
        except (FileNotFoundError, ValueError) as e:
            raise EpisodeError(404, str(e)) from None
        files = []
        for f, path in zip(item.get("files") or [item.get("video")], queue.resolve_files(ep, item)):
            files.append({"name": Path(str(f)).name, "exists": path.is_file(), "sha256": safety.sha256_file(path) if path.is_file() else None,
                          "url": self._url(project_id, ep, path)})
        try:
            now = queue.current_sha(ep, item)
        except OSError:
            now = None
        out = {k: item.get(k) for k in ("id", "status", "title", "platforms", "visibility", "schedule", "media_type", "sha256",
                                        "description", "caption", "alt_texts", "ai_disclosure", "made_for_kids")}
        out.update(files=files, current_sha256=now, preflight_problems=item.get("preflight_problems") or [],
                   events=(item.get("events") or [])[-12:], project=project_id, folder=str(ep.root),
                   approve_line=f"ave publish approve {ep.root} {item['id']}")
        return safety.redact_obj(out)

    def publish_reject(self, project_id: str, item_id: str, reason: str = "", by: str | None = None) -> dict:
        _, ep = self._one(project_id)
        try:
            with self.lock:
                item = queue.reject(ep, item_id, (by or self.by).strip()[:80], str(reason or "")[:500])
        except FileNotFoundError as e:
            raise EpisodeError(404, str(e)) from None
        except ValueError as e:
            raise EpisodeError(409, str(e)) from None
        return {"ok": True, "id": item["id"], "status": item["status"]}

    # ---------------------------------------------------------------- trims: clearing one (the native sheet calls this)
    def clear_trim(self, project_id: str, session: str) -> dict:
        from ... import trim
        _, ep = self._one(project_id)
        try:
            with self.lock:
                rec = trim.clear_trim(ep, session)
        except SystemExit as e:
            raise EpisodeError(400, str(e)) from None
        return {"ok": True, "session": session, "trim": rec}

    def trim_of(self, project_id: str, session: str) -> dict | None:
        from ...episode import read_json
        _, ep = self._one(project_id)
        return (read_json(ep.meta_path).get("trim") or {}).get(session)

    # ---------------------------------------------------------------- jobs
    def _runner(self, project_id: str) -> tuple[object, JobRunner]:
        p, ep = self._one(project_id)
        with self.lock:
            r = self.runners.get(project_id)
            if r is None:
                r = self.runners[project_id] = JobRunner(ep)
        return p, r

    def jobs(self) -> dict:
        rows = []
        titles = {p.project_id: p.title or p.root.name for p, _ in self._all()}
        for pid, r in list(self.runners.items()):
            for j in r.list():
                rows.append({**{k: v for k, v in j.items() if k != "argv"}, "project": pid, "title": titles.get(pid, pid),
                             "line": " ".join(j.get("argv") or [])})
        rows.sort(key=lambda j: j["id"], reverse=True)
        catalog = sorted(ALLOWED)
        return {"jobs": rows, "catalog": catalog, "projects": [{"id": k, "title": v} for k, v in titles.items()]}

    def job_start(self, project_id: str, cmd: str, args: dict | None = None) -> dict:
        _, r = self._runner(project_id)
        try:
            return r.start(cmd, args or {})
        except JobRefused as e:
            raise EpisodeError(400, str(e)) from None

    def job_tail(self, project_id: str, jid: str) -> dict:
        _, r = self._runner(project_id)
        try:
            return {k: v for k, v in r.tail(jid).items() if k != "argv"}
        except KeyError:
            raise EpisodeError(404, "No such job.") from None

    def job_cancel(self, project_id: str, jid: str) -> dict:
        _, r = self._runner(project_id)
        try:
            return r.cancel(jid)
        except KeyError:
            raise EpisodeError(404, "No such job.") from None
        except JobRefused as e:
            raise EpisodeError(409, str(e)) from None
