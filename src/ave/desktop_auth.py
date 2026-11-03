"""Contextual macOS device-owner authentication. No HTTP/JS/CLI authority flag."""
from __future__ import annotations
import threading
from .humanpresence import agent_markers_set


class NativeAuthority:
    """Per-workspace, process-only capability. A fresh OS callback is its only grant."""
    def __init__(self):
        self.active = False
        self.closed = False
        self._generation = 0
        self._context = None
        self._timer = None

    def request(self, reason, complete):
        from Foundation import NSOperationQueue
        from LocalAuthentication import LAContext, LAPolicyDeviceOwnerAuthentication
        if self.closed or agent_markers_set():
            complete(False); return
        self.cancel()
        generation = self._generation
        context = LAContext.alloc().init()
        self._context = context
        ok, error = context.canEvaluatePolicy_error_(LAPolicyDeviceOwnerAuthentication, None)
        if not ok:
            self.cancel(); complete(False); return
        def finish(success):
            def deliver():
                if self.closed or generation != self._generation: return
                if self._timer: self._timer.cancel()
                self._generation += 1
                self._context = None
                context.invalidate()
                self.active = bool(success) and not agent_markers_set()
                complete(self.active)
            NSOperationQueue.mainQueue().addOperationWithBlock_(deliver)
        self._timer = threading.Timer(60, lambda: finish(False))
        self._timer.daemon = True
        self._timer.start()
        context.evaluatePolicy_localizedReason_reply_(LAPolicyDeviceOwnerAuthentication, reason, lambda success, error: finish(success))

    def cancel(self):
        self._generation += 1
        if self._timer: self._timer.cancel(); self._timer = None
        if self._context: self._context.invalidate(); self._context = None
        self.active = False

    def close(self):
        self.cancel(); self.closed = True

    def require(self):
        if self.closed or not self.active or agent_markers_set():
            from .studio.ui.api import StudioError
            raise StudioError(403, 'Use the native Review menu to authenticate for this workspace.', 'native.authentication_required')
