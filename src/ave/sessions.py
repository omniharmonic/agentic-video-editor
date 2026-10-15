"""Sessions (takes) of an episode, and which cameras / speakers belong to which (field note N1).

A recording day can hold several sessions on ONE recorder timeline. `ave ingest` finds them (media.json `sessions`), the human
(or agent) assigns `session` to each camera in episode.json, and every later stage can run per session. Everything here is
derived from those two files, so there is no extra state to keep in step.

An episode with a single session (every pre-sessions episode, and every ordinary one) behaves exactly as before: no names, the
legacy single cutlist.json / assembly / rough / final.
"""

from __future__ import annotations

from .episode import Episode, read_json


def media_sessions(ep: Episode) -> list[dict]:
    p = ep.artifact("media.json")
    return list(read_json(p).get("sessions") or []) if p.exists() else []


def camera_sessions(ep: Episode, meta: dict | None = None) -> dict[str, str]:
    """camera key -> session id. episode.json `cameras[k].session` wins; else the session whose media.json files include the camera."""
    meta = meta if meta is not None else read_json(ep.meta_path)
    by_file = {f: s["id"] for s in media_sessions(ep) for f in s["files"]}
    out = {}
    for k, info in (meta.get("cameras") or {}).items():
        sid = info.get("session") or by_file.get(info.get("file"))
        if sid:
            out[k] = sid
    return out


def session_ids(ep: Episode, meta: dict | None = None) -> list[str]:
    """Session ids that actually have mapped cameras, in order. One or none means "not a multi-session episode"."""
    cs = camera_sessions(ep, meta)
    return sorted(set(cs.values()), key=lambda s: (len(s), s))


def is_multi(ep: Episode) -> bool:
    try:
        return len(session_ids(ep)) > 1
    except (OSError, ValueError):
        return False


def speaker_camera(meta: dict, speaker: str, sid: str | None, cams: dict[str, str]) -> str | None:
    """The camera that frames `speaker` in session `sid`: speakers[spk].cameras[sid], else .camera if that camera is in the session."""
    info = (meta.get("speakers") or {}).get(speaker) or {}
    if sid is not None:
        c = (info.get("cameras") or {}).get(sid)
        if c:
            return c
        c = info.get("camera")
        return c if c and cams.get(c, sid) == sid else None
    return info.get("camera")


def hint(ep: Episode, name: str | None) -> str:
    """Appended to "no such edit" errors: on a multi-session episode the unnamed edits do not exist, the named ones do."""
    if name is None and is_multi(ep):
        return f" This episode has sessions {', '.join(session_ids(ep))}: use --name <session> (each session is its own edit)."
    return ""


def cutlist_name(sid: str | None) -> str:
    return f"cutlist.{sid}.json" if sid else "cutlist.json"


def planned(ep: Episode, sid: str | None) -> bool:
    return ep.artifact(cutlist_name(sid)).exists()


def stage_status(ep: Episode) -> dict[str, dict]:
    """Per-session pipeline state from disk: {sid: {cutlist, assembly, rough, final: bool}}. Empty for single-session episodes."""
    from . import editdoc
    ids = session_ids(ep)
    if len(ids) < 2:
        return {}
    return {sid: {"cutlist": planned(ep, sid),
                  **{st: bool(editdoc.list_versions(ep, st, sid)) for st in ("assembly", "rough", "final")}} for sid in ids}
