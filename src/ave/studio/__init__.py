"""Content studio core (`ave studio`): the project, the data model, the stores and the status derivation.

Spec: docs/superpowers/specs/2026-10-19-content-studio-design.md. This package is the frozen contract layer (workstream WS0);
the library, platforms, scheduler, server and UI build on it. A StudioProject IS an Episode, so the existing publish queue,
approval signatures and license log run on it unchanged.
"""
