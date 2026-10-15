"""`ave ui <episode>`: the localhost management interface (docs/ui.md). Human surface: run it yourself, agents must not."""

from __future__ import annotations

import getpass
import webbrowser

from ..episode import Episode


def cmd_ui(a) -> int:
    from ..ui.server import UiServer
    ep = Episode(a.episode)
    ep.require()
    srv = UiServer(ep, port=a.port, by=a.by or getpass.getuser())
    url = srv.url()
    print(f"ave ui for {ep.root}\n  open: {url}\n  bound to 127.0.0.1:{srv.port} only; the token above is required on every request.\n"
          "  This is a human surface: keep the URL to yourself. Ctrl-C stops the server.", flush=True)
    if a.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        srv.server_close()
    return 0


def register(sub) -> None:
    s = sub.add_parser("ui", help="localhost management UI: dashboard, clips (keep/maybe/skip), trim, content, publish approval, jobs")
    s.add_argument("episode")
    s.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default 0 = a free random port)")
    s.add_argument("--open", action="store_true", help="open the URL (with its token) in the default browser")
    s.add_argument("--by", help="default decider name shown in the UI (default: the OS user)")
    s.set_defaults(fn=cmd_ui)
