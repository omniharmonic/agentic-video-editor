"""Run group: tick, run, resolve, alerts.

`tick` is a rehearsal unless `--send`; `--send` (and `run --send`) is the human's decision (spec 12 #6) and is refused without a human terminal, where
it prints the exact line. Underneath, `schedule.tick.tick` independently refuses to send when an agent marker is set. `resolve` settles an UNKNOWN
publish after the human checked the platform: a human act.
"""

from __future__ import annotations

import getpass
import signal
import threading
from datetime import datetime

from ... import humanpresence
from ...schedule import daemon
from ...schedule.tick import TickRefused
from .. import dispatch, ledger, sendgate, store
from ..events import Actor
from . import Blocked, add_dir, guarded, human_gate, out, project, say, studio


def _now(a, s):
    if not a.now:
        return None
    return datetime.fromisoformat(a.now.replace("Z", "+00:00"))


def _tick_exit(rep: dict) -> int:
    return 2 if (rep.get("paused") or rep.get("failed") or rep.get("uncertain") or rep.get("locked")) else 0


def _send_gate(a, action: str, line: str) -> None:
    """`--send` needs a human terminal, OR the human's signed `send.enabled` (so a launchd job with no tty can send). An agent marker refuses both."""
    if humanpresence.agent_markers_set() or not sendgate.verify(project(a)).ok:
        human_gate(a, action, line)


@guarded
def cmd_tick(a) -> int:
    if a.send:
        _send_gate(a, "sending posts", "ave studio tick --send" + (f" --now {a.now}" if a.now else ""))
    s = studio(a)
    try:
        rep = s.tick(send=a.send, now=_now(a, s))
    except TickRefused as e:
        raise Blocked(str(e), needs_human=True, run="ave studio tick --send") from None
    out({"ok": _tick_exit(rep) == 0, **rep,
         **({"note": "REHEARSAL: nothing was sent. `--send` is the human's decision."} if not a.send else {})})
    return _tick_exit(rep)


@guarded
def cmd_run(a) -> int:
    if a.send:
        _send_gate(a, "running the sending daemon", f"ave studio run --send --interval {a.interval}")
    s = studio(a)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except ValueError:                      # not the main thread (tests)
            pass
    reports = []
    try:
        n = daemon.run_loop(s.sp, clock=s.clock, send=a.send, interval=a.interval, publishers=s.publishers, host=s.host, stop=stop, max_ticks=a.max_ticks,
                            on_report=lambda r: (reports.append(r.as_dict()), say(f"tick {len(reports)}: posted {len(r.posted)}, blocked {len(r.blocked)}")))
    except TickRefused as e:
        raise Blocked(str(e), needs_human=True, run="ave studio run --send") from None
    out({"ok": True, "ticks": n, "sent": sum(len(r["posted"]) for r in reports), "sending": bool(a.send)})
    return 0


@guarded
def cmd_resolve(a) -> int:
    flag = f"--posted {a.posted}" if a.posted else "--send-again" if a.send_again else "--retry"
    human_gate(a, "sending a post again" if a.send_again else "settling an uncertain publish", f"ave studio resolve {a.post} {a.variant} {flag}")
    s = studio(a, adapters=False)
    name = a.by or getpass.getuser()
    if a.send_again:
        res = dispatch.send_again(s.sp, a.post, a.variant, by=name, actor=Actor("human", name), now=s.now())
    else:
        res = dispatch.resolve_uncertain(s.sp, a.post, a.variant, posted_url=a.posted, retry=bool(a.retry), by=name,
                                         actor=Actor("human", name), now=s.now())
    out({"ok": True, **res})
    return 0


@guarded
def cmd_alerts(a) -> int:
    s = studio(a, adapters=False)
    words = list(a.words or [])
    if words:
        if words[0] != "ack" or len(words) < 2:
            raise ValueError("usage: ave studio alerts [--all]   |   ave studio alerts ack ID [ID ...]")
        done = [ledger.ack_alert(s.sp, i, a.by or "claude", now=s.now())["alert"] for i in words[1:]]
        out({"ok": True, "acknowledged": done})
        return 0
    out({"ok": True, "alerts": s.alerts(include_acked=a.all)})
    return 0


def add(ss) -> None:
    p = ss.add_parser("tick", help="one scheduler pass: a rehearsal unless --send (a human decision)")
    p.add_argument("--send", action="store_true")
    p.add_argument("--now", help="ISO time to act as (rehearsals and tests)")
    add_dir(p)
    p.set_defaults(fn=cmd_tick)
    p = ss.add_parser("run", help="loop `tick` with a heartbeat; --send is a human decision")
    p.add_argument("--send", action="store_true")
    p.add_argument("--interval", type=float, default=60.0)
    p.add_argument("--max-ticks", type=int, default=None, help="stop after N ticks (default: run until stopped)")
    add_dir(p)
    p.set_defaults(fn=cmd_run)
    p = ss.add_parser("resolve", help="HUMAN: settle an uncertain publish (--posted URL or --retry), or --send-again a post already sent")
    p.add_argument("post")
    p.add_argument("variant")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--posted", metavar="URL")
    g.add_argument("--retry", action="store_true")
    g.add_argument("--send-again", action="store_true",
                   help="this computer already sent it, or a previous publisher reported it sent: send it again (needs a fresh approval)")
    p.add_argument("--by")
    add_dir(p)
    p.set_defaults(fn=cmd_resolve)
    p = ss.add_parser("alerts", help="list failures and what to do; `alerts ack ID` acknowledges")
    p.add_argument("words", nargs="*", metavar="ack ID", help="`ack ID [ID ...]` acknowledges alerts")
    p.add_argument("--all", action="store_true", help="include acknowledged")
    p.add_argument("--by")
    add_dir(p)
    p.set_defaults(fn=cmd_alerts)
