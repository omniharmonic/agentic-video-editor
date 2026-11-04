"""The app's native board bridge, for browser tests that act as the person.

In AVE Studio the web view sends the person's own board writes through `aveNative.boardWrite` (desktop_native's bridge), and
the native side runs them through desktop_approvals (`boardWrite`, `via="app"`). A plain browser has no bridge, so its writes go
over HTTP with the room token and are recorded as Claude's (final review I1). `install(page, srv)` gives a test page that bridge:
the same validation and the same Approvals code path the app runs, with nothing but the AppKit sheet left out. A write that
needs the device-owner check (an answer to a settings conflict) is refused here, as it is without its sheet.
"""

from __future__ import annotations

from ave.desktop_approvals import Approvals, approval_args

# Merged into whatever aveNative a test already stubbed (when that stub can still be replaced).
SCRIPT = """(() => {
  const d = Object.getOwnPropertyDescriptor(window, 'aveNative');
  if (d && d.configurable === false) return;
  const base = (d && (d.value || (d.get && d.get()))) || {};
  Object.defineProperty(window, 'aveNative', { configurable: true, writable: true,
    value: Object.assign({}, base, { boardWrite: async (m, p, b) => {
      const r = await window.__aveBoardWrite(m, p, b);           // as desktop_native's reply: an Error carrying the route's status
      if (r && r.__error) { const e = new Error(r.__error); e.status = r.status; throw e; }
      return r;
    } }) });
})();"""


def writer(srv):
    sheets = Approvals(srv.app, srv.workspace, getattr(srv, "episodes", None), server=srv)

    def write(method, path, body):
        try:
            req = sheets.board_request(approval_args("boardWrite", {"method": method, "path": path, "body": body}))
            if req["kind"] != "app":
                raise ValueError("This change needs its own sheet in the app. Cancelled.")
            return sheets.board_write(req)
        except Exception as e:  # noqa: BLE001 - what desktop_native replies: the words and, when it has one, the status
            return {"__error": str(e), "status": getattr(e, "status", None)}
    return write


def install(page, srv, *, now: bool = False) -> None:
    """Give `page` the app's bridge: for its next navigation (an init script), and also at once when `now` (a loaded page)."""
    page.expose_function("__aveBoardWrite", writer(srv))
    page.add_init_script(SCRIPT)
    if now:
        page.evaluate(SCRIPT)
