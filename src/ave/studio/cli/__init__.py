"""The `ave studio ...` command implementations (spec 8). `ave.commands.studio` registers them; this package holds the code.

Conventions (the `ave publish` ones): one JSON object on stdout (secrets redacted), human text on stderr, exit 0 = done, 2 = blocked or needs a human.
A command that is a human act calls `human_gate`, which refuses without a human terminal and prints the exact line for the human to run in their own
terminal window as JSON (`needs_human: true`, `run`). Nothing in this package ever approves a post, turns an account live, or sends without `--send`
from a human terminal.

Test seam: `OPEN_KW` is passed to `open_studio` (clock, publishers, host, providers); tests set it, nothing else does.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable

from ... import humanpresence
from ...publish import safety
from ..events import AGENT, HUMAN, Actor
from ..model import RevConflict
from ..project import StudioProject, find_studio

OPEN_KW: dict = {}


class Blocked(Exception):
    """A refusal with a machine-readable reason; the command exits 2."""

    def __init__(self, message: str, **extra):
        super().__init__(message)
        self.extra = extra


def out(obj) -> None:
    print(json.dumps(safety.redact_obj(obj), indent=2, ensure_ascii=False))


def say(msg: str) -> None:
    print(msg, file=sys.stderr)


def project(a, *, must_exist: bool = True) -> StudioProject:
    sp = find_studio(getattr(a, "dir", None))
    if must_exist:
        sp.require()
    return sp


def with_dir(a, line: str) -> str:
    """The exact command line to hand a human, carrying `--studio DIR` when the agent was pointed at one."""
    d = getattr(a, "dir", None)
    return f"{line} --studio {d}" if d else line


def account_publishers(sp, *, now=None, host=None) -> dict:
    """{account id: account-bound adapter} for every valid account. Built without network; a broken account is skipped (doctor reports it).
    `host` is the configured media host (Instagram and Threads need one); the adapters share it."""
    from ...platforms import registry
    from ...platforms.accounts import list_accounts
    out_: dict = {}
    kw = {**({"now": now} if now else {}), **({"host": host} if host is not None else {})}
    for acct in list_accounts(sp):
        try:
            out_[acct.id] = registry.adapter_for(acct, **kw)
        except Exception:
            continue
    return out_


def wired_kwargs(sp, kw: dict | None = None) -> dict:
    """The `open_studio` keyword arguments of a REAL studio: the configured media host and one account-bound adapter per account. Tests and
    demos pass their own through OPEN_KW / `kw`, which win."""
    from ...platforms import host as hostmod
    out_ = dict(kw or {})
    if "host" not in out_:
        out_["host"] = hostmod.load_host()
    if "publishers" not in out_:
        out_["publishers"] = account_publishers(sp, host=out_["host"])
    return out_


def real_studio(sp, **kw):
    """A fully wired `Studio` for `sp` (every hook registered by `open_studio`, adapters and host attached): what `ave studio ui` serves."""
    from ..service import open_studio
    return open_studio(sp.root, **wired_kwargs(sp, kw))


def studio(a, *, adapters: bool = True):
    from ..service import open_studio
    sp = project(a)
    kw = dict(OPEN_KW)
    if adapters:
        kw = wired_kwargs(sp, kw)
    return open_studio(sp.root, **kw)


def human_gate(a, action: str, line: str) -> None:
    """Return when a human terminal is present; else raise Blocked carrying the exact command for the human (exit 2)."""
    try:
        humanpresence.require_human(action, with_dir(a, line))
    except SystemExit as e:
        raise Blocked(f"{action} is a human act and was not run", needs_human=True, run=with_dir(a, line),
                      bang="! " + with_dir(a, line), why=str(e.code).splitlines()[0]) from None


def actor_for(human: bool = False, name: str | None = None) -> Actor:
    if human:
        return Actor("human", name or HUMAN.name)
    return Actor("agent", name or AGENT.name)


def guarded(fn: Callable) -> Callable:
    """Turn the expected failures of a command into JSON + exit 2 (the refusal is the product), leave bugs as tracebacks (exit 1)."""
    def run(a) -> int:
        try:
            return int(fn(a) or 0)
        except Blocked as e:
            out({"ok": False, "blocked": True, "error": str(e), **e.extra})
            say(str(e))
            if e.extra.get("run"):
                say("Ask the human to run, in their own terminal window:\n  " + e.extra["run"])
            return 2
        except RevConflict as e:
            out({"ok": False, "error": str(e), "code": "conflict.rev", "latest_rev": (e.latest or {}).get("rev"),
                 "hint": "re-read the post (`ave studio post show`) and retry with its current --expected-rev"})
            say(str(e))
            return 2
        except (ValueError, KeyError, FileNotFoundError, PermissionError, NotADirectoryError) as e:
            msg = str(e.args[0]) if isinstance(e, KeyError) and e.args else str(e)
            out({"ok": False, "error": msg, "code": getattr(e, "code", None) or type(e).__name__})
            say(msg)
            return 2
        except SystemExit as e:
            if e.code in (0, None):
                return 0
            if isinstance(e.code, int):
                return e.code
            out({"ok": False, "error": str(e.code)})
            say(str(e.code))
            return 2
    run.__wrapped__ = fn
    return run


def csv(s: str | None) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def parse_value(raw: str):
    """`--set k=v` values: JSON when they parse (numbers, true, null, lists, objects), else the string as typed."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def add_dir(p) -> None:
    p.add_argument("--studio", dest="dir", metavar="DIR", help="studio folder (default: <workspace>/studio)")


def read_json_arg(value: str | None, what: str):
    """A JSON literal or `@file` / a path to a JSON file."""
    if value is None:
        return None
    v = value.strip()
    if v.startswith(("{", "[", '"')):
        try:
            return json.loads(v)
        except ValueError as e:
            raise ValueError(f"{what} is not valid JSON: {e}") from e
    p = Path(v.lstrip("@")).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"{what}: no such file {p}")
    return json.loads(p.read_text())
