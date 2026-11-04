"""Project group: init, status, doctor, adopt, daemon-plist, ui."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from ... import workspace
from ..project import StudioProject
from ..service import Studio
from . import Blocked, add_dir, guarded, out, project, say, studio, with_dir


def _merge(a) -> None:
    """init/status/ui take the folder positionally (`ave studio status DIR`) and also as --studio DIR, like every other command."""
    if getattr(a, "studio_opt", None) and not getattr(a, "dir", None):
        a.dir = a.studio_opt


@guarded
def cmd_init(a) -> int:
    """Idempotent and non-destructive: creates only what is missing; an existing studio.json is never rewritten."""
    _merge(a)
    if a.dir:
        target: Path | str = a.dir
    else:
        ws = workspace.get()
        if ws is None:
            raise Blocked("no DIR given and no workspace set: `ave studio init <dir>` or `ave workspace <dir>` first")
        target = ws / "studio"
    sp = StudioProject(target)
    existed = sp.is_studio()
    sp.create(title=a.name, tz=a.tz)
    from ... import progress
    try:
        files = progress.adopt(sp)
    except Exception as e:     # the studio is valid without PROGRESS.md; say so rather than failing the init
        files = {"error": f"{type(e).__name__}: {e}"}
    out({"ok": True, "studio": str(sp.root), "created_studio_json": not existed, "title": sp.read_meta().get("title"),
         "tz": sp.read_meta().get("tz"), "project_files": files,
         "next": f"Add accounts (`ave studio account add`), then `ave studio status {sp.root}`"})
    return 0


@guarded
def cmd_status(a) -> int:
    _merge(a)
    s = studio(a, adapters=False)
    out({"ok": True, "studio": str(s.sp.root), **s.summary()})
    return 0


@guarded
def cmd_doctor(a) -> int:
    from .. import doctor
    s = studio(a, adapters=False)
    res = doctor.run(s)
    out(res)
    return 0 if res["ok"] else 2


@guarded
def cmd_adopt(a) -> int:
    from .. import ingest
    s = studio(a, adapters=False)
    res = ingest.adopt(s, a.episode, by=a.by or "claude")
    out({"ok": True, **res})
    return 0


PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
{args}
  </array>
  <key>WorkingDirectory</key><string>{cwd}</string>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>{interval}</integer>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""


@guarded
def cmd_daemon_plist(a) -> int:
    """PRINTS a launchd job that runs ONE `ave studio tick` every `--interval` seconds. Installing it is the human's act (and `--send` is their
    choice): this command writes nothing. A job has no terminal, so a SENDING job runs `tick --send` and relies on the human's signed
    `send.enabled` (`ave studio send enable`, a human act); without it the job exits 2 at every interval and sends nothing."""
    from .. import sendgate
    sp = project(a)
    exe = shutil.which("ave") or "ave"
    argv = [exe, "studio", "tick", "--studio", str(sp.root)] + (["--send"] if a.send else [])
    label = f"com.ave.studio.{sp.root.name}"
    plist = PLIST.format(label=label, args="\n".join(f"    <string>{x}</string>" for x in argv), cwd=sp.root, log=sp.work / "daemon.log",
                         interval=max(10, int(a.interval)))
    dest = Path("~/Library/LaunchAgents").expanduser() / f"{label}.plist"
    st = sendgate.verify(sp)
    steps = [f"save the plist above to {dest}", f"launchctl load {dest}"]
    if a.send and not st.ok:
        steps.insert(0, "ave studio send enable    (YOU, in a terminal: without this signed permission the job cannot send)")
    out({"ok": True, "label": label, "sends": bool(a.send), "plist": plist, "install_path": str(dest), "send_enabled": st.ok,
         "install": steps,
         "note": ("This job will SEND approved, live-attested posts at their slots, using your signed send.enabled." if a.send else
                  "This job only REHEARSES (no --send): nothing is posted. Regenerate with --send when you choose to let it post.")
                 + " Installing it is a human act; this command wrote nothing."})
    return 0


@guarded
def cmd_demo(a) -> int:
    """Build a synthetic, populated demo studio (see `ave.studio.demo`). Refuses a folder that is not empty. Posts nothing, needs no network."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from .. import demo
    target = Path(a.dir or a.studio_opt or "ave-studio-demo").expanduser()
    now = None
    if a.today:
        try:
            now = datetime.fromisoformat(a.today + "T12:00:00").replace(tzinfo=ZoneInfo(demo.TZ))
        except ValueError:
            raise ValueError(f"--today must be YYYY-MM-DD, got {a.today!r}") from None
    try:
        res = demo.build_demo(target, now=now, log=lambda m: say(f"  built {m}"))
    except demo.DemoError as e:
        raise Blocked(str(e)) from None
    ui = f"ave studio ui {res['studio']} --open"
    out({"ok": True, **res, "next": [ui, f"ave studio status {res['studio']}", f"ave studio doctor {res['studio']}"],
         "note": "Every account is dry-run and every picture, person and price is made up. Nothing can post."})
    say(f"Demo studio ready in {res['studio']} ({res['posts']} posts, {res['accounts']} accounts, {res['assets']} library assets, {res['seconds']} s).\n"
        "Nothing is real and nothing can post: every account is dry-run.\n"
        "Next, in your own terminal window (opening the app is a human act):\n"
        f"  {ui}\n"
        "Then try: the Workspace's Calendar (drag a post), Previews, Sources and their licences, and Settings › Alerts.")
    return 0


def cmd_ui(a) -> int:
    _merge(a)
    from ..ui.server import run_cli
    try:
        return run_cli(a)
    except SystemExit as e:
        if isinstance(e.code, str) and "human act" in e.code:
            line = with_dir(a, "ave studio ui") + (" --open" if a.open else "")
            out({"ok": False, "blocked": True, "needs_human": True, "error": "ave studio ui is a human act (it acts as you in the studio)", "run": line,
                 "bang": "! " + line})
            say(e.code.splitlines()[0] + "\nAsk the human to run, in their own terminal window:\n  " + line)
            return 2
        raise


def _ui_args(p) -> None:
    p.add_argument("dir", nargs="?", help="studio folder (default: <workspace>/studio)")
    p.add_argument("--studio", dest="studio_opt", metavar="DIR")
    p.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default 0 = a free random port)")
    p.add_argument("--open", action="store_true", help="open the URL (with its token) in the default browser")
    p.add_argument("--by", help="default editor name shown in the UI (default: the OS user)")
    p.add_argument("--fixtures", action="store_true", help="serve the committed sample data (nothing real; no terminal needed)")


def add(ss) -> None:
    p = ss.add_parser("init", help="create a studio folder (idempotent)")
    p.add_argument("dir", nargs="?", help="studio folder (default: <workspace>/studio)")
    p.add_argument("--studio", dest="studio_opt", metavar="DIR")
    p.add_argument("--name", help="studio title (default: the folder name)")
    p.add_argument("--tz", default="UTC", help="IANA time zone for schedules, e.g. America/Denver")
    p.set_defaults(fn=cmd_init)
    p = ss.add_parser("status", help="counts, paused, open feedback and alerts (JSON)")
    p.add_argument("dir", nargs="?", help="studio folder (default: <workspace>/studio)")
    p.add_argument("--studio", dest="studio_opt", metavar="DIR")
    p.add_argument("--json", action="store_true", help="accepted for symmetry: the output is always JSON")
    p.set_defaults(fn=cmd_status)
    p = ss.add_parser("doctor", help="read-only checks: attestations, approvals, secrets in files, library, adapters (all unverified live)")
    add_dir(p)
    p.set_defaults(fn=cmd_doctor)
    p = ss.add_parser("adopt", help="turn an episode's queue items into posts that reference that queue")
    p.add_argument("episode")
    p.add_argument("--by", help="who adds the files to the library (default: claude)")
    add_dir(p)
    p.set_defaults(fn=cmd_adopt)
    p = ss.add_parser("daemon-plist", help="print a launchd job for `ave studio run` (the human installs it)")
    p.add_argument("--interval", type=int, default=60)
    p.add_argument("--send", action="store_true", help="a job that SENDS (otherwise it only rehearses)")
    add_dir(p)
    p.set_defaults(fn=cmd_daemon_plist)
    p = ss.add_parser("demo", help="build a synthetic, populated demo studio (two fictional brands, every status, no accounts, no network)")
    p.add_argument("dir", nargs="?", help="a new or empty folder (default: ./ave-studio-demo)")
    p.add_argument("--studio", dest="studio_opt", metavar="DIR")
    p.add_argument("--today", metavar="YYYY-MM-DD", help="lay the data out around this day instead of today (for repeatable screenshots)")
    p.set_defaults(fn=cmd_demo)
    p = ss.add_parser("ui", help="the app in a browser (human): the Room, the Workspace and Settings; approving needs AVE Studio itself")
    _ui_args(p)
    p.set_defaults(fn=cmd_ui)
