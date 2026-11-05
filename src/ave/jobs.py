"""Background jobs for the app (the Workspace's Jobs panel): a fixed allowlist of `ave` subcommands with typed, validated arguments.

The UI never builds a shell string. A request is {"cmd": "clips prepare", "args": {"min_s": 20}}; the command must be in ALLOWED,
every argument must be declared for it and pass its type check, and the episode is always the first positional (never user
input). Nothing that publishes, approves, sends, spends money, or takes a file path is allowlisted. Flags that bypass a safety
check (`--allow-unapproved`, `--allow-unlicensed`) and any environment override are refused by name.
"""

from __future__ import annotations

import os
import signal
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .episode import Episode

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")

# type: name = id-like token; int/float = number (bool rejected); choice = one of `of`; flag = true/false switch
ALLOWED: dict[str, dict] = {
    "status": {"argv": ["status"], "args": {}},
    "resume": {"argv": ["resume"], "args": {}},
    "progress": {"argv": ["progress"], "args": {}},
    "trim suggest": {"argv": ["trim", "suggest"], "args": {"session": ("--session", "name"), "minutes": ("--minutes", "float")}},
    "assemble": {"argv": ["assemble"], "args": {"session": ("--session", "name"), "all_sessions": ("--all-sessions", "flag"),
                                                "combined": ("--combined", "flag")}},
    "cleanup": {"argv": ["cleanup"], "args": {"name": ("--name", "name"), "no_pacing": ("--no-pacing", "flag")}},
    "render": {"argv": ["render"], "args": {"stage": ("--stage", ("auto", "assembly", "rough", "final")), "name": ("--name", "name"),
                                            "version": ("--version", "int"), "proxy": ("--proxy", "flag"),
                                            "overwrite": ("--overwrite", "flag")}},
    "clips prepare": {"argv": ["clips", "prepare"], "args": {"stage": ("--stage", ("assembly", "rough", "final")), "version": ("--version", "int"),
                                                              "min_s": ("--min-s", "float"), "max_s": ("--max-s", "float"),
                                                              "max_candidates": ("--max-candidates", "int"), "name": ("--name", "name")}},
    "clips review": {"argv": ["clips", "review"], "args": {"drafts": ("--drafts", "flag"), "name": ("--name", "name")}},
    "clips previews": {"argv": ["clips", "previews"], "args": {"name": ("--name", "name"), "ids": ("--ids", "name"), "top": ("--top", "int"),
                                                                "pad": ("--pad", "float"), "force": ("--force", "flag")}},
    "clips build": {"argv": ["clips", "build"], "args": {"approved_only": ("--approved-only", "flag"), "order": ("--order", ("time", "score")),
                                                          "name": ("--name", "name")}},
    "clips list": {"argv": ["clips", "list"], "args": {"all": ("--all", "flag"), "name": ("--name", "name")}},
    "content status": {"argv": ["content", "status"], "args": {}},
    "content review": {"argv": ["content", "review"], "args": {}},
    "content render": {"argv": ["content", "render"], "args": {"item": ("--item", "name"), "force": ("--force", "flag")}},
    "publish list": {"argv": ["publish", "list"], "args": {}},
}
FORBIDDEN_WORDS = ("allow_unapproved", "allow_unlicensed", "allow-unapproved", "allow-unlicensed", "allow_incomplete", "allow-incomplete", "yes", "send", "confirm_sha", "out", "env", "path")


CHILD_CODE = "import sys; from ave.cli import main; sys.exit(main(sys.argv[1:]))"
GRACE_S = 5.0                         # how long a cancelled step's processes get to stop before SIGKILL


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _signal_group(pgid: int, sig) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _end_group(pgid: int, limit: float = 30.0) -> None:
    """Wait until no process is left in the group, SIGKILLing it after GRACE_S."""
    start = time.monotonic()
    killed = False
    while _group_alive(pgid) and time.monotonic() - start < limit:
        if not killed and time.monotonic() - start > GRACE_S:
            _signal_group(pgid, signal.SIGKILL)
            killed = True
        time.sleep(0.05)


class JobRefused(ValueError):
    pass


def build_argv(ep: Episode, cmd: str, args: dict | None) -> list[str]:
    """Validate a request and return the argv after `ave` (episode included). Raises JobRefused."""
    if not isinstance(cmd, str) or cmd not in ALLOWED:
        raise JobRefused(f"{cmd!r} is not an allowed job. Allowed: {', '.join(sorted(ALLOWED))}")
    spec = ALLOWED[cmd]
    args = args or {}
    if not isinstance(args, dict):
        raise JobRefused("args must be an object")
    argv = [*spec["argv"], str(ep.root)]
    for key, val in args.items():
        norm = str(key).lower().replace("-", "_")
        if norm in {w.replace("-", "_") for w in FORBIDDEN_WORDS} or norm not in spec["args"]:
            raise JobRefused(f"argument {key!r} is not allowed for `ave {cmd}`" + (f" (allowed: {', '.join(spec['args'])})" if spec["args"] else " (it takes none)"))
        flag, kind = spec["args"][norm]
        if kind == "flag":
            if val is True:
                argv.append(flag)
            elif val not in (False, None):
                raise JobRefused(f"{key} must be true or false")
            continue
        if kind == "name":
            ok = isinstance(val, str) and bool(_NAME.match(val)) and ".." not in val
        elif kind == "int":
            ok = isinstance(val, int) and not isinstance(val, bool) and 0 <= val <= 10**6
        elif kind == "float":
            ok = isinstance(val, (int, float)) and not isinstance(val, bool) and 0 <= val <= 10**6
        else:
            ok = isinstance(val, str) and val in kind
        if not ok:
            raise JobRefused(f"bad value for {key}: {val!r}")
        argv += [flag, str(val)]
    return argv


def child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env.pop("AVE_APPROVAL_KEY", None)      # allowlisted jobs never sign anything; do not hand the signing key to children
    env["AVE_AGENT"] = "1"                  # a UI job is never a human at a keyboard (ave.humanpresence)
    return env


class JobRunner:
    """One job at a time per episode (most commands write the same artifacts). Logs live in <work>/ui/jobs/<id>.log."""

    def __init__(self, ep: Episode):
        self.ep = ep
        self.lock = threading.Lock()
        self.jobs: dict[str, dict] = {}
        self.procs: dict[str, subprocess.Popen] = {}
        self.dir = ep.work / "ui" / "jobs"

    def _running(self) -> dict | None:
        return next((j for j in self.jobs.values() if j["status"] in ("running", "stopping")), None)

    def start(self, cmd: str, args: dict | None) -> dict:
        from .updates import refuse_if_draining
        refuse_if_draining()                     # the restart for an update was approved: nothing new starts
        argv = build_argv(self.ep, cmd, args)
        with self.lock:
            busy = self._running()
            if busy:
                raise JobRefused(f"job {busy['id']} (`ave {busy['cmd']}`) is still running; wait for it to finish")
            jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
            self.dir.mkdir(parents=True, exist_ok=True)
            log = self.dir / f"{jid}.log"
            job = {"id": jid, "cmd": cmd, "argv": ["ave", *argv], "status": "running", "started": time.strftime("%H:%M:%S"),
                   "returncode": None, "log": str(log)}
            fh = open(log, "wb")
            fh.write(("$ ave " + " ".join(argv) + "\n").encode())
            fh.flush()
            code = CHILD_CODE
            if getattr(sys, 'frozen', False):
                from .desktop_worker import enqueue
                command = [sys.executable, '--production-worker', enqueue(self.ep, cmd, args)]
            else:
                command = [sys.executable, "-c", code, *argv]
            # its own session and process group, so Cancel reaches every process the step starts (ffmpeg, workers), not only `ave`
            proc = subprocess.Popen(command, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    cwd=str(self.ep.root), env=child_env(), start_new_session=True)
            job["pgid"] = proc.pid
            self.jobs[jid] = job
            self.procs[jid] = proc
        threading.Thread(target=self._wait, args=(job, proc, fh), daemon=True).start()
        return self.public(job)

    def _wait(self, job: dict, proc: subprocess.Popen, fh) -> None:
        rc = proc.wait()
        fh.close()
        if job.get("cancelled"):                      # "Cancelled" only once nothing in the step's process group is left
            _end_group(job["pgid"])
        job["returncode"] = rc
        job["finished"] = time.strftime("%H:%M:%S")
        job["status"] = "cancelled" if job.get("cancelled") else "done" if rc == 0 else "failed"
        self.procs.pop(job["id"], None)

    def cancel(self, jid: str) -> dict:
        """Stop a running job and everything it started: SIGTERM to its process group, SIGKILL after GRACE_S. The row says
        "stopping" until the whole group is gone, and no other job starts meanwhile. Stages are resumable, so what it wrote is
        reused or redone."""
        with self.lock:
            job = self.jobs.get(jid)
            if not job:
                raise KeyError(jid)
            proc = self.procs.get(jid)
            if job["status"] != "running" or proc is None:
                raise JobRefused("that job is not running")
            job["cancelled"] = True
            job["status"] = "stopping"
            _signal_group(job["pgid"], signal.SIGTERM)
        threading.Thread(target=self._kill_late, args=(job,), daemon=True).start()
        return self.public(job)

    @staticmethod
    def _kill_late(job: dict) -> None:
        end = time.monotonic() + GRACE_S
        while time.monotonic() < end:
            if not _group_alive(job["pgid"]):
                return
            time.sleep(0.1)
        _signal_group(job["pgid"], signal.SIGKILL)

    @staticmethod
    def public(job: dict) -> dict:
        return {k: v for k, v in job.items() if k not in ("log", "pgid")}

    def list(self) -> list[dict]:
        return [self.public(j) for j in sorted(self.jobs.values(), key=lambda j: j["id"], reverse=True)]

    def tail(self, jid: str, max_bytes: int = 20000) -> dict:
        job = self.jobs.get(jid)
        if not job:
            raise KeyError(jid)
        p = Path(job["log"])
        data = p.read_bytes()[-max_bytes:] if p.exists() else b""
        return {**self.public(job), "tail": data.decode("utf-8", "replace")}
