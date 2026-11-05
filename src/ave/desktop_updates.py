"""Check for Updates in the desktop app: Sparkle 2's SPUStandardUpdaterController, driven through PyObjC.

Sparkle's own window does the work: it reads the appcast (`SUFeedURL`), checks the EdDSA signature against `SUPublicEDKey`,
downloads, verifies, installs and relaunches. AVE only decides WHEN it may relaunch: Sparkle asks
`updater:shouldPostponeRelaunchForUpdate:untilInvokingBlock:` and, while `updates.safe_to_restart` is false (a render, an
export, a Jobs panel job, a sync transfer or an import), AVE keeps the block and runs it from a timer once the work is done.

A build without the update key (any development build) has updates switched off, and the menu item says why.
Imports of AppKit and objc happen only in `DesktopUpdater.start`, so this module stays importable in tests.
"""
from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path

from . import updates

log = logging.getLogger(__name__)

MENU_TITLE = "Check for Updates…"
OFF_TITLE = "Check for Updates… (off in this build)"
POSTPONE = b"updater:shouldPostponeRelaunchForUpdate:untilInvokingBlock:"
_DELEGATE = None


def menu_title(state: dict) -> str:
    return MENU_TITLE if state.get("enabled") else OFF_TITLE


def framework_path() -> Path:
    return Path(sys.executable).resolve().parent.parent / "Frameworks" / "Sparkle.framework"


def _delegate_class():
    """The SPUUpdaterDelegate, made once per process (an Objective-C class name can only be registered once)."""
    global _DELEGATE
    if _DELEGATE is not None:
        return _DELEGATE
    import objc
    import Foundation as F
    # Sparkle ships no bridgesupport: describe the block argument so PyObjC hands Python a callable.
    objc.registerMetaDataForSelector(b"NSObject", POSTPONE, {
        "retval": {"type": objc._C_NSBOOL},
        "arguments": {4: {"callable": {"retval": {"type": objc._C_VOID}, "arguments": {0: {"type": b"^v"}}}}}})
    protocols = [p for p in (objc.protocolNamed("SPUUpdaterDelegate"),) if p is not None]

    class AVEUpdaterDelegate(F.NSObject, protocols=protocols):
        def updater_shouldPostponeRelaunchForUpdate_untilInvokingBlock_(self, updater, item, block):
            owner = self.owner
            postponed = owner.gate.postpone(lambda: block())
            if postponed:
                owner.waiting(owner.gate.last)
            return postponed

        def updater_didAbortWithError_(self, updater, error):
            updates.end_drain()                  # the update did not happen: work may start again
            log.warning("update stopped: %s", updates.redact(str(error.localizedDescription()) if error else "unknown error"))

    _DELEGATE = AVEUpdaterDelegate
    return _DELEGATE


class DesktopUpdater:
    """One per app run. `app` is the desktop controller (what safe_to_restart reads); `say(title, detail)` shows a sheet;
    `ask(title, detail)` a sheet with Continue/Cancel (True for Continue); `waiting(reasons)` is told when an install is held
    back, and is called with [] when it goes ahead. The timer's busy check (it asks Syncthing over HTTP) runs on `background`
    and its answer comes back through `on_main`, so a slow Syncthing never freezes the window."""

    def __init__(self, app, say, waiting=None, state: dict | None = None, *, ask=None, on_main=None, background=None):
        self.app, self.say = app, say
        updates.end_drain()
        self.ask = ask or (lambda title, detail: False)
        self.on_main = on_main or (lambda fn: fn())
        self.background = background or (lambda fn: threading.Thread(target=fn, daemon=True, name="ave-update-check").start())
        self._checking = False
        self.waiting = waiting or (lambda reasons: None)
        self.state = state if state is not None else updates.status()
        # Sync is left out of the gate's own look (made on the main thread, with the drain flag up): the timer's
        # background check covers it, and a sync is not something the drain flag could have let start.
        self.gate = updates.RelaunchGate(app, recheck=lambda a: updates.busy_reasons(a, sync=False))
        self.controller = None
        self.delegate = None
        self.problem: str | None = None

    @property
    def title(self) -> str:
        return menu_title(self.state) if not self.problem else OFF_TITLE

    def start(self) -> bool:
        """Load the bundled Sparkle and start its updater (scheduled checks follow Info.plist). False when off."""
        if not self.state.get("enabled"):
            return False
        try:
            import objc
            framework = framework_path()
            if not framework.is_dir():
                raise RuntimeError("Sparkle.framework is missing from this app")
            objc.loadBundle("Sparkle", globals(), bundle_path=str(framework))
            cls = objc.lookUpClass("SPUStandardUpdaterController")
            self.delegate = _delegate_class().alloc().init()
            self.delegate.owner = self
            self.controller = cls.alloc().initWithStartingUpdater_updaterDelegate_userDriverDelegate_(True, self.delegate, None)
            return True
        except Exception as exc:  # noqa: BLE001 - the app works without updates; the menu says so
            self.problem = f"Updates could not start: {updates.redact(str(exc))}"
            log.warning(self.problem)
            return False

    def check(self) -> None:
        """The menu item: Sparkle's own check window, or why updates are off."""
        if self.problem:
            self.say("Updates are off", self.problem)
        elif not self.state.get("enabled") or self.controller is None:
            self.say("Updates are off in this build", self.state.get("reason") or updates.DEV_BUILD)
        elif self.gate.waiting:
            why = updates.busy_reasons(self.app) or self.gate.last
            if self.ask("An update is ready", "It installs and AVE Studio restarts as soon as this finishes: " + "; ".join(why)
                        + ".\n\nContinue installs it now and stops that work. Cancel keeps waiting."):
                self.gate.force()
                self.waiting([])
        else:
            self.controller.checkForUpdates_(None)

    def poll(self) -> None:
        """Timer tick: let a held install go ahead once nothing would be cut short. The check runs off the main thread."""
        if not self.gate.waiting or self._checking:
            return
        self._checking = True

        def check():
            try:
                why = updates.busy_reasons(self.app)
            except Exception as exc:  # noqa: BLE001 - keep waiting; say why in the log
                why = [f"the busy check failed ({type(exc).__name__})"]
            self.on_main(lambda: self._decided(why))
        self.background(check)

    def _decided(self, why: list[str]) -> None:
        self._checking = False
        if why:
            self.gate.last = why
            self.waiting(why)
        elif self.gate.release(check=True):    # re-checks with the drain flag up (no Syncthing call: quick, on main)
            self.waiting([])
