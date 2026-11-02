"""Derivation closure: a render's licensing is the licensing of everything it was made from (spec 2.4)."""

from __future__ import annotations

from typing import Iterable, Mapping


def closure(assets: Mapping[str, Mapping], ids: Iterable[str]) -> tuple[list[str], list[str]]:
    """(ordered ids reachable through `derived_from`, starting ids included; ids that are not in the registry)."""
    order: list[str] = []
    unknown: list[str] = []
    seen: set[str] = set()
    todo = list(ids)[::-1]
    while todo:
        a = todo.pop()
        if a in seen:
            continue
        seen.add(a)
        rec = assets.get(a)
        if rec is None:
            unknown.append(a)
            continue
        order.append(a)
        todo.extend(reversed(rec.get("derived_from") or []))
    return order, unknown


def leaves(assets: Mapping[str, Mapping], ids: Iterable[str]) -> list[str]:
    """Closure members derived from nothing: where licenses actually attach."""
    order, _ = closure(assets, ids)
    return [a for a in order if not assets[a].get("derived_from")]
