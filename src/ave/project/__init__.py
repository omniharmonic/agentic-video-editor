"""Project identity, per-device append-only logs, a hybrid logical clock, a deterministic reducer and a disposable index.

No sync, UI or MCP code lives here: those build on `Project`, `Log`, `reduce` and `Index`.
"""

from .hlc import Clock, Stamp
from .index import Index
from .log import Log, LogError, LogRead, read_log, read_logs
from .model import Device, Project, ProjectError, ReadOnlyProject, short_id
from .reducer import ACTORS, OPS, STAGES, Artifact, Conflict, State, reduce

__all__ = [
    "ACTORS", "OPS", "STAGES", "Artifact", "Clock", "Conflict", "Device", "Index", "Log", "LogError", "LogRead", "Project", "ProjectError", "ReadOnlyProject",
    "Stamp", "State", "read_log", "read_logs", "reduce", "short_id",
]
