"""A project's sync status in plain words, for the Workspace pill and its popover.

    say(manager.status(project)) -> {"state", "tone", "text", "detail", "next", "members": [{"name", "state", "text"}]}

People never see Syncthing, device ids or error strings from it: every member is named, every folder error becomes a
sentence, and where there is something to do, `next` says the one thing.
"""

from __future__ import annotations

from . import SOMEONE, SyncStatus

# tone -> the pill colour in the Workspace: ok green, busy blue, wait grey, warn amber, bad red, quiet no fill
TONES = {"up_to_date": "ok", "syncing": "busy", "waiting": "wait", "low_disk": "warn", "drive_missing": "warn",
         "tailscale_off": "warn", "sync_off": "wait", "not_shared": "quiet", "error": "bad"}
DRIVE_NEXT = "Connect the drive that holds this project. Nothing was deleted, and sync carries on when it is back."
TAILSCALE_NEXT = "Open Tailscale and sign in. Sync starts again by itself."


def size(n: int | float | None) -> str:
    """Bytes as people say them: 0 bytes, 840 KB, 3.2 GB, 1.1 TB (decimal units, as Finder shows them)."""
    n = float(n or 0)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            if unit == "bytes":
                return f"{int(n)} {'byte' if int(n) == 1 else 'bytes'}"
            return f"{n:.1f} {unit}" if n < 10 else f"{n:.0f} {unit}"
        n /= 1000
    return ""


def files(n: int) -> str:
    return f"{n} {'file' if n == 1 else 'files'}"


def names(who: list[str]) -> str:
    who = [w for w in who if w]
    if len(who) <= 1:
        return who[0] if who else "the others"
    return ", ".join(who[:-1]) + " and " + who[-1]


def _cap(s: str) -> str:
    """A person's name as written in a sentence; "another computer" stays lower case mid-sentence."""
    return s if s == SOMEONE else s[:1].upper() + s[1:]


def _start(s: str) -> str:
    return s[:1].upper() + s[1:]


def member_text(m) -> str:
    n = _cap(m.name)
    if m.state == "up_to_date":
        return f"{_start(n)} is up to date"
    return {
        "offline": f"Waiting for {n} (offline)",
        "not_joined": f"Waiting for {n} to join",
        "paused": f"Waiting for {n} (paused on their computer)",
        "checking": f"Checking with {n}",
        "sending": f"Sending {files(m.need_items)} to {n}" if m.need_items else f"Sending changes to {n}",
    }.get(m.state, _start(n))


def _error_words(err: str) -> tuple[str, str, str]:
    """(text, detail, next) for a folder error Syncthing reported, in plain words."""
    e = err.lower()
    if "permission denied" in e or "operation not permitted" in e:
        return ("Sync stopped: this computer cannot write to the project folder",
                "The folder or something in it is read-only for you.",
                "In Finder, choose Get Info on the project folder and give yourself Read & Write.")
    if "not a directory" in e:
        return ("Sync stopped: the project is not a folder any more",
                "Something replaced the project folder with a file.", "Put the project folder back where it was.")
    if "read-only file system" in e:
        return ("Sync stopped: the drive is read-only",
                "This computer can read the drive but not write to it.",
                "Use a drive formatted as APFS or exFAT, or one that is not locked.")
    if "looks like a copy" in e:
        return "Sync stopped: this is a copy of a shared project", err, "Open the original, or make the copy a new project."
    if "project.json" in e:
        return "This folder is not a project", err, ""
    if "not started" in e or "not running" in e:
        return "Sync is not running", "", "Quit and reopen AVE Studio."
    if "too many open files" in e:
        return "Sync stopped: too many files open", "", "Quit some apps, then reopen AVE Studio."
    return "Sync stopped: something went wrong", err[:300], "If it does not clear by itself, quit and reopen AVE Studio."


def say(s: SyncStatus) -> dict:
    members = [{"name": _start(_cap(m.name)) if m.name != SOMEONE else "", "state": m.state, "text": member_text(m)}
               for m in s.members]
    out = {"state": s.state, "tone": TONES.get(s.state, "bad"), "text": "", "detail": "", "next": "", "members": members}
    away = [m for m in s.members if m.state in ("offline", "not_joined", "paused")]
    if s.state == "up_to_date":
        out["text"] = "Up to date"
        if len(s.members) == 1 and s.members[0].name != SOMEONE:
            out["detail"] = f"Everything here is on {_cap(s.members[0].name)}'s computer too."
        elif s.members:
            out["detail"] = "Everything here is on every member's computer too."
    elif s.state == "syncing":
        sending = [m for m in s.members if m.state == "sending"]
        connected = [m for m in s.members if m.state not in ("offline", "not_joined", "paused")]
        if s.need_items:
            src = f" from {_cap(connected[0].name)}" if len(connected) == 1 else ""
            out["text"] = f"Receiving {files(s.need_items)}{src}"
            if s.need_bytes:
                out["detail"] = f"{size(s.need_bytes)} to go."
        elif len(sending) == 1:
            out["text"] = member_text(sending[0])
            if sending[0].need_bytes:
                out["detail"] = f"{size(sending[0].need_bytes)} to go."
        elif sending:
            n = sum(m.need_items for m in sending)
            out["text"] = f"Sending {files(n)} to {names([_cap(m.name) for m in sending])}" if n else \
                f"Sending changes to {names([_cap(m.name) for m in sending])}"
        elif any(m.state == "checking" for m in s.members):
            out["text"] = f"Checking with {names([_cap(m.name) for m in s.members if m.state == 'checking'])}"
        else:
            out["text"] = "Checking for changes"
    elif s.state == "waiting":
        offline = [m for m in away if m.state == "offline"]
        if offline and len(offline) == len(away):
            out["text"] = f"Waiting for {names([_cap(m.name) for m in offline])} (offline)"
            out["detail"] = "Changes go across when you are both online."
        elif len(away) == 1:
            out["text"] = member_text(away[0])
        else:
            out["text"] = f"Waiting for {names(s.waiting_for and [_cap(w) for w in s.waiting_for] or [_cap(m.name) for m in away])}"
    elif s.state == "low_disk":
        free = f"{size(s.free_bytes)} free" if s.free_bytes is not None else "Almost no space is left"
        if "database" in s.error.lower():       # Syncthing's own index, on this Mac's disk, not the project drive
            out["text"] = "Paused: this Mac is low on space"
            out["detail"] = f"{free} on this Mac's own disk. Nothing new arrives until there is room."
            out["next"] = "Free up space on this Mac's disk. Sync carries on by itself."
        else:
            out["text"] = "Paused: the drive is low on space"
            floor = f", and sync needs at least {size(s.min_free_bytes)}" if s.min_free_bytes else ""
            out["detail"] = f"{free}{floor}. Nothing new arrives until there is room."
            out["next"] = "Free up space on the drive that holds this project. Sync carries on by itself."
    elif s.state == "drive_missing":
        out["text"] = "Drive not connected"
        out["detail"] = "The project folder is not there, so sync is paused. The other computers keep every file."
        out["next"] = DRIVE_NEXT
    elif s.state == "tailscale_off":
        out["text"] = "Tailscale is off"
        out["detail"] = "Projects sync only over Tailscale, so nothing goes across until it is on."
        out["next"] = TAILSCALE_NEXT
    elif s.state == "sync_off":
        why = s.error.lower()
        if why == "starting":
            out["text"] = "Sync is starting"
            out["detail"] = "Changes go across in a moment."
        elif why in ("restarting", "start") or "not running" in why or "timed out" in why or "not answering" in why:
            out["text"] = "Sync is restarting"
            out["detail"] = "Nothing is lost. Changes go across once it is back, usually within a minute."
        elif why == "stopped":
            out["text"] = "Sync is off"
            out["detail"] = "Changes stay on this computer until sync is on again."
            out["next"] = "Quit and reopen AVE Studio."
        else:
            out["text"] = "Sync could not start"
            out["detail"] = "Tailscale did not give this computer an address sync can use."
            out["next"] = "Check that Tailscale is on and signed in. Sync tries again by itself."
    elif s.state == "not_shared":
        out["text"] = "Not shared"
        out["detail"] = "Only on this computer."
    else:
        out["text"], out["detail"], out["next"] = _error_words(s.error)
    return out
