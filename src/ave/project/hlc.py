"""Hybrid logical clock and sortable event ids for the project log.

An HLC stamp is `<ISO UTC with ms>Z/<4-digit counter>/<device>`, for example `2026-11-02T21:04:11.120Z/0003/abcd`. The physical
part follows the wall clock when it can; the counter keeps order when it cannot. Stamps of the same width compare correctly as
strings, but code that orders stamps uses `parse()` so a malformed stamp is caught instead of sorted.

Clock skew: a clock always adopts every stamp it reads and never ticks below one, so causal order holds whatever any wall
clock says. A computer whose clock was reset to 2020 still writes after the 2026 event it just read. A stamp more than
`MAX_AHEAD_MS` (24 h) ahead of this computer's clock is still adopted, but leaves a loud warning naming the device, because one
computer set far ahead pulls every clock forward with it. Stamps beyond year 9000 are rejected by the reducer's validation, so a
tick can never overflow the format.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

MAX_AHEAD_MS = 24 * 3600 * 1000
COUNTER_MAX = 9999
_HLC_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3})Z/(\d{4,9})/([A-Za-z0-9_-]{1,64})$")
MAX_MS = 221845392000000  # 9000-01-01T00:00:00Z: later stamps are rejected so ticks cannot overflow the year field
CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"


@dataclass(frozen=True, order=True)
class Stamp:
    ms: int
    counter: int
    device: str

    def __str__(self) -> str:
        return format_hlc(self.ms, self.counter, self.device)


def wall_ms() -> int:
    return time.time_ns() // 1_000_000


def ms_to_iso(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}"


def iso_to_ms(text: str) -> int:
    """`2026-11-02T10:00:00Z`, with or without milliseconds, to epoch milliseconds."""
    t = text.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(t)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(round(dt.timestamp() * 1000))


def format_hlc(ms: int, counter: int, device: str) -> str:
    return f"{ms_to_iso(ms)}Z/{counter:04d}/{device}"


def parse(hlc: str) -> Stamp:
    """Raise ValueError for anything that is not a well-formed stamp."""
    m = _HLC_RE.match(hlc) if isinstance(hlc, str) else None
    if not m:
        raise ValueError(f"not an HLC stamp: {hlc!r}")
    return Stamp(iso_to_ms(m.group(1) + "Z"), int(m.group(2)), m.group(3))


def is_hlc(hlc: object) -> bool:
    try:
        parse(hlc)  # type: ignore[arg-type]
        return True
    except ValueError:
        return False


class Clock:
    """One device's HLC. `observe()` every stamp read from the logs, then `tick()` for the next local event."""

    def __init__(self, device: str, wall: Callable[[], int] = wall_ms, max_ahead_ms: int = MAX_AHEAD_MS):
        self.device = device
        self.wall = wall
        self.max_ahead_ms = max_ahead_ms
        self._ms = -1
        self._counter = 0
        self.warnings: list[str] = []

    @property
    def last(self) -> Stamp | None:
        return None if self._ms < 0 else Stamp(self._ms, self._counter, self.device)

    def _adopt(self, ms: int, counter: int) -> None:
        if (ms, counter) > (self._ms, self._counter):
            self._ms, self._counter = ms, counter

    def observe(self, hlc: str | Stamp) -> None:
        """Merge a stamp read from a log. Always adopted; one far ahead of this computer's clock is warned about by name."""
        r = hlc if isinstance(hlc, Stamp) else parse(hlc)
        if r.ms >= MAX_MS:
            self._warn(f"CLOCK WARNING: ignored a stamp from {r.device} beyond year 9000 ({format_hlc(r.ms, r.counter, r.device)}).")
            return
        now = self.wall()
        if r.ms > now + self.max_ahead_ms:
            who = "this computer's own log" if r.device == self.device else f"device {r.device}"
            self._warn(f"CLOCK WARNING: {who} has a time {_human(r.ms - now)} ahead of this computer's clock. Every new event "
                       f"now follows it. Check the date and time settings on {r.device} and on this computer.")
        self._adopt(r.ms, r.counter)

    def tick(self) -> str:
        now = self.wall()
        if now > self._ms:
            ms, counter = now, 0
        else:
            ms, counter = self._ms, self._counter + 1
        if counter > COUNTER_MAX:
            ms, counter = ms + 1, 0
        self._ms, self._counter = ms, counter
        return format_hlc(ms, counter, self.device)

    def _warn(self, text: str) -> None:
        if text not in self.warnings:
            self.warnings.append(text)


def after(prev: str, device: str, wall: int | str) -> str:
    """The next stamp on `device` after it has seen `prev`, given its wall clock (ms or ISO). Always greater than `prev`."""
    w = iso_to_ms(wall) if isinstance(wall, str) else int(wall)
    c = Clock(device, wall=lambda: w)
    c.observe(prev)
    return c.tick()


def at(device: str, wall: int | str) -> str:
    """The first stamp on `device` at the given wall time."""
    w = iso_to_ms(wall) if isinstance(wall, str) else int(wall)
    return Clock(device, wall=lambda: w).tick()


# ------------------------------------------------------------------------------------------------ ids

_id_lock = threading.Lock()
_id_last = (-1, 0)


def _b32(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(out)).upper()


def ulid(now_ms: int | None = None) -> str:
    """26-character sortable id: 48-bit milliseconds then 80 random bits, monotonic within this process."""
    global _id_last
    ms = wall_ms() if now_ms is None else now_ms
    with _id_lock:
        last_ms, last_rand = _id_last
        if ms <= last_ms:
            ms, rand = last_ms, (last_rand + 1) & ((1 << 80) - 1)
        else:
            rand = int.from_bytes(os.urandom(10), "big")
        _id_last = (ms, rand)
    return _b32(ms, 10) + _b32(rand, 16)


def _human(ms: int) -> str:
    h = ms / 3_600_000
    return f"{h / 24:.0f} days" if h >= 48 else f"{h:.0f} h"
