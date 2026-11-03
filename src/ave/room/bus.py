"""An in-memory event log with sequence numbers: publishers append, followers resume from the last `seq` they saw."""

from __future__ import annotations

import threading
from collections import deque

KEEP = 500


class Bus:
    def __init__(self, keep: int = KEEP):
        self._events: deque[dict] = deque(maxlen=keep)
        self._seq = 0
        self._cond = threading.Condition()
        self.closed = False

    def publish(self, type: str, data: dict) -> int:
        """Append one event and return its seq (1, 2, 3, ...)."""
        with self._cond:
            self._seq += 1
            self._events.append({"seq": self._seq, "type": type, "data": data})
            self._cond.notify_all()
            return self._seq

    def since(self, seq: int) -> list[dict]:
        """Events newer than `seq` still held (the last KEEP)."""
        with self._cond:
            return [e for e in self._events if e["seq"] > seq]

    def wait(self, seq: int, timeout: float) -> list[dict]:
        """Like `since`, but blocks up to `timeout` s for something newer; [] on timeout or close."""
        with self._cond:
            if not self.closed and self._seq <= seq:
                self._cond.wait(timeout)
            return [e for e in self._events if e["seq"] > seq]

    @property
    def latest(self) -> int:
        return self._seq

    @property
    def oldest(self) -> int:
        """seq of the oldest event still held (0 when empty)."""
        with self._cond:
            return self._events[0]["seq"] if self._events else 0

    def close(self) -> None:
        with self._cond:
            self.closed = True
            self._cond.notify_all()
