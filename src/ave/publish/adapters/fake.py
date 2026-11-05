"""A stand-in platform for acceptance runs on real computers: it never touches the network.

Turned on only by `AVE_PUBLISH_FAKE=<file>` in the environment of the process that sends (the person's own terminal). Every
"post" is one JSON line appended to that file, and `recover` reads the file back, so the whole send path (approval, the publisher
check, the dispatch record, recovery) runs for real while nothing leaves the computer. The two-Mac checklist uses it
(docs/development/2026-11-two-mac-acceptance.md). Receipts say `fake-…` and point at the file, so a fake post can never be
mistaken for a real one.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

from .base import Absent, Publisher

ENV = "AVE_PUBLISH_FAKE"


def fake_file() -> Path | None:
    v = os.environ.get(ENV, "").strip()
    return Path(v).expanduser() if v else None


class FakePublisher(Publisher):
    carries_images = True

    def __init__(self, name: str, path: Path, **kw):
        super().__init__(platforms_cfg=kw.get("platforms_cfg", {}), now=kw.get("now"))
        self.name, self.path = name, Path(path)

    def preflight(self, item, video):
        return []

    def ready_problems(self):
        return []

    def status(self):
        return {"platform": self.name, "ready": True, "fake": str(self.path)}

    def _rows(self) -> list[dict]:
        try:
            return [json.loads(x) for x in self.path.read_text().splitlines() if x.strip()]
        except (OSError, ValueError):
            return []

    def publish(self, item, video, progress):
        from ..receipts import result
        pid = f"fake-{self.name}-{secrets.token_hex(4)}"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps({"id": pid, "platform": self.name, "item": item.get("id"), "title": item.get("title"),
                                "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}) + "\n")
        return result(self.name, pid, f"file://{self.path}#{pid}")

    def recover(self, item, record):
        from ..receipts import result
        pid = ((record.get("receipt") or {}).get("object") or {}).get("id")
        if not pid:
            return None
        if any(r.get("id") == pid for r in self._rows()):
            return result(self.name, pid, f"file://{self.path}#{pid}", recovered=True)
        return Absent(f"{pid} is not in {self.path}")
