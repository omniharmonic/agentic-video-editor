"""Frozen boot only. All executable modes are explicitly dispatched."""
from ave.desktop_worker import dispatch
raise SystemExit(dispatch())
