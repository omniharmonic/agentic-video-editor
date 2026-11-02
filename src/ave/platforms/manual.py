"""`platform: manual` (spec 4.2): every platform without an adapter. The scheduler cannot post, so a "send" prepares a ready-to-post
folder (the exact approved files, caption, alt text, link, first comment, a checksum list) and raises an alert; the HUMAN posts by hand
and records the URL with `ave studio resolve <post> <variant> --posted URL`.

To make that work with the queue's states, `publish` writes the folder and then raises `Uncertain`: the platform result stays `sending`
(= "waiting for the human"), which is exactly the state `queue.resolve(posted_url=...)` settles. Nothing is ever sent anywhere.

Folder: <studio>/exports/manual/<account>/<queue id>/   (01-<file>..., caption.txt, alt-texts.txt, manifest.json, README.txt)
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Callable

from ..publish import safety
from ..publish.adapters.base import PublishError, Uncertain
from .base import PlatformAdapter, compose_text, item_files, media_type


class ManualAdapter(PlatformAdapter):
    platform = "manual"
    capabilities = frozenset({"text", "image", "carousel", "video", "document"})
    required = ()

    def preflight(self, item: dict, video: Path) -> list[str]:
        return self.bound_problems(item) + self.alt_problems(item) + ([] if item.get("title") else ["manual: a title is required"])

    def folder(self, item: dict) -> Path | None:
        root = self.account.studio_root
        return (root / "exports" / "manual" / self.account.id / item["id"]) if root else None

    def publish(self, item: dict, video: Path, progress: Callable[[dict], None]) -> dict:
        out = self.folder(item)
        if out is None:
            raise PublishError("manual: not a studio account, so there is nowhere to prepare the post. Nothing was posted.")
        files = item_files(item, video)
        out.mkdir(parents=True, exist_ok=True)
        names = []
        for i, f in enumerate(files, 1):
            dst = out / f"{i:02d}-{Path(f).name}"
            shutil.copyfile(f, dst)
            names.append({"file": dst.name, "sha256": safety.sha256_file(dst)})
        text = compose_text(item)
        (out / "caption.txt").write_text(text + "\n")
        alts = item.get("alt_texts") or []
        if alts:
            (out / "alt-texts.txt").write_text("\n".join(f"{i}. {a}" for i, a in enumerate(alts, 1)) + "\n")
        manifest = {"item": item["id"], "account": self.account.id, "label": self.account.label, "type": media_type(item),
                    "post_type": item.get("post_type"), "schedule": item.get("schedule"), "first_comment": item.get("first_comment"),
                    "link": item.get("link_url"), "files": names}
        (out / "manifest.json").write_text(json.dumps(safety.redact_obj(manifest), indent=2) + "\n")
        (out / "README.txt").write_text(
            f"Post this by hand to {self.account.label or self.account.id}.\n"
            f"1. Upload the numbered files in order. 2. Paste caption.txt{' and the alt texts' if alts else ''}.\n"
            f"3. Then tell the studio where it went:\n   ave studio resolve <post> <variant> --posted <URL>\n")
        progress({"phase": "prepared", "folder": str(out)})
        self._alert(item, out)
        raise Uncertain(f"manual: ready-to-post folder written to {out}; post it by hand, then run `ave studio resolve --posted URL`")

    def _alert(self, item: dict, out: Path) -> None:
        try:
            from ..studio import ledger
            from ..studio.project import StudioProject
            ledger.append_alert(StudioProject(self.account.studio_root), severity="info", kind="manual_post_due", account=self.account.id,
                                message=f"post {item['id']} is ready to post by hand: {out}")
        except Exception as e:              # an alert is a courtesy; the folder and the `sending` state are the record
            safety.get_logger("ave.platforms.manual").warning("manual alert not written: %s", safety.redact(e))
