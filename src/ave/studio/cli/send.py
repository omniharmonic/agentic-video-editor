"""Send group: `send enable|disable|status`. The human's standing permission for an UNATTENDED sender (studio/sendgate.py).

`ave studio tick --send` needs a human terminal; a launchd job has none. `send enable` (a human act: a terminal, and the studio's name typed
back) writes `<studio>/send.enabled`, a one-line signed file the tick accepts instead of a terminal. `send disable` writes a signed "off".
Neither is open to an agent; `send status` is read-only.
"""

from __future__ import annotations

import getpass

from .. import sendgate
from . import Blocked, add_dir, guarded, human_gate, out, project


@guarded
def cmd_enable(a) -> int:
    human_gate(a, "allowing an unattended sender", "ave studio send enable")
    sp = project(a)
    name = sp.root.name
    typed = input(f"Allow a background job to SEND approved, live-attested posts from {sp.root} with no one at the keyboard. "
                  f"Type the studio folder name ({name}) to confirm: ").strip()
    if typed != name:
        raise Blocked(f"typed {typed!r}, not {name!r}; nothing changed")
    rec = sendgate.write(sp, a.by or getpass.getuser())
    out({"ok": True, "enabled": True, "by": rec["by"], "at": rec["at"], "file": str(sendgate.path(sp)),
         "next": "Install the job with `ave studio daemon-plist --send` (it runs `tick --send`). `ave studio send disable` revokes this."})
    return 0


@guarded
def cmd_disable(a) -> int:
    human_gate(a, "revoking the unattended sender", "ave studio send disable")
    sp = project(a)
    rec = sendgate.disable(sp, a.by or getpass.getuser())
    out({"ok": True, "enabled": False, "by": rec["by"], "at": rec["at"]})
    return 0


@guarded
def cmd_status(a) -> int:
    sp = project(a)
    st = sendgate.verify(sp)
    out({"ok": True, "enabled": st.ok, "by": st.by, "at": st.at, "reason": st.reason})
    return 0


def add(ss) -> None:
    g = ss.add_parser("send", help="HUMAN: allow or revoke an unattended sender (a signed send.enabled the launchd job's tick accepts)")
    sub = g.add_subparsers(dest="send_cmd", required=True)
    p = sub.add_parser("enable", help="HUMAN: write the signed permission (type the studio name to confirm)")
    p.add_argument("--by")
    add_dir(p)
    p.set_defaults(fn=cmd_enable)
    p = sub.add_parser("disable", help="HUMAN: revoke it (writes a signed off; a saved copy of the old file does not work)")
    p.add_argument("--by")
    add_dir(p)
    p.set_defaults(fn=cmd_disable)
    p = sub.add_parser("status", help="is an unattended sender allowed? (read-only)")
    add_dir(p)
    p.set_defaults(fn=cmd_status)
