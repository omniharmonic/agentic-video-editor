"""Command plugins. Each module in this package may define `register(sub)` to add `ave` subcommands.

This keeps modules independent: adding a feature means adding one file here, never editing cli.py.
"""

from __future__ import annotations

import importlib
import pkgutil


def discover(sub) -> None:
    for mod in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
        module = importlib.import_module(f"{__name__}.{mod.name}")
        register = getattr(module, "register", None)
        if register:
            register(sub)
