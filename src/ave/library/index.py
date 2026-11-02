"""`library/index.sqlite`: a DERIVED full-text index over the registry (tags, alt text, source text, file name). Spec 2.4.

It is never a source of truth: `reindex` rebuilds it from `assets.jsonl` alone, and `search` rebuilds it by itself when the registry has
changed since the index was built (size + mtime stamp), so a deleted or stale index costs one rebuild, never a wrong answer.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from ..studio import model, store
from . import registry


def index_path(sp) -> Path:
    return sp.library_dir / "index.sqlite"


def _stamp(sp) -> str:
    p = registry.assets_path(sp)
    if not p.is_file():
        return "none"
    st = p.stat()
    return f"{st.st_size}:{st.st_mtime_ns}"


def _doc(rec: dict) -> tuple:
    src = rec.get("source") or {}
    source_text = " ".join(str(x) for x in (src.get("type"), src.get("provider"), src.get("asset_id"),
                                            (rec.get("license") or {}).get("summary")) if x)
    return (rec["id"], " ".join(rec.get("tags") or []), rec.get("alt") or "", source_text, Path(rec["file"]).stem + " " + rec["kind"])


def reindex(sp) -> int:
    """Rebuild the index from `assets.jsonl`. Returns the number of assets indexed."""
    recs = store.read_assets(sp)
    path = index_path(sp)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".sqlite.tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    try:
        con.execute("CREATE VIRTUAL TABLE fts USING fts5(id UNINDEXED, tags, alt, source, filename, tokenize='porter unicode61')")
        con.execute("CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)")
        con.executemany("INSERT INTO fts(id, tags, alt, source, filename) VALUES (?,?,?,?,?)", [_doc(r) for r in recs.values()])
        con.execute("INSERT INTO meta VALUES('stamp', ?)", (_stamp(sp),))
        con.commit()
    finally:
        con.close()
    tmp.replace(path)
    return len(recs)


def _fresh(sp) -> bool:
    path = index_path(sp)
    if not path.is_file():
        return False
    try:
        con = sqlite3.connect(path)
        try:
            row = con.execute("SELECT v FROM meta WHERE k='stamp'").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return bool(row) and row[0] == _stamp(sp)


def ensure(sp) -> None:
    if not _fresh(sp):
        reindex(sp)


def match_ids(sp, q: str) -> list[str]:
    """Asset ids matching every word of `q` (prefix match), best first."""
    words = re.findall(r"\w+", q.lower())
    if not words:
        return []
    ensure(sp)
    expr = " AND ".join(f'"{w}"*' for w in words)
    con = sqlite3.connect(index_path(sp))
    try:
        return [r[0] for r in con.execute("SELECT id FROM fts WHERE fts MATCH ? ORDER BY rank", (expr,))]
    finally:
        con.close()


def search(sp, q: str | None = None, *, kind: str | None = None, tag: str | None = None, unused: bool = False,
           limit: int = 50) -> list[dict]:
    """Registry lines, newest first (or best match first with `q`). `unused` keeps assets no post references."""
    recs = store.read_assets(sp)
    if q and q.strip():
        ids = match_ids(sp, q)
        rows = [recs[i] for i in ids if i in recs]
    else:
        rows = sorted(recs.values(), key=lambda r: (r.get("added") or "", r["id"]), reverse=True)
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    if tag:
        rows = [r for r in rows if tag in (r.get("tags") or [])]
    if unused:
        used = used_ids(sp)
        rows = [r for r in rows if r["id"] not in used]
    return rows[:max(0, limit)]


def used_ids(sp) -> set[str]:
    out: set[str] = set()
    for post in store.list_posts(sp):
        out.update(model.media_asset_ids(post))
    return out


def usage(sp, asset_id: str) -> list[dict]:
    """[{post, variant}] that reference the asset (variant None = post-level media)."""
    out = []
    for post in store.list_posts(sp):
        if any(m.get("asset") == asset_id for m in post.get("media") or []):
            out.append({"post": post["id"], "variant": None})
        for v in post.get("variants") or []:
            if any(m.get("asset") == asset_id for m in v.get("media") or []):
                out.append({"post": post["id"], "variant": v["id"]})
    return out
