"""`.ave/local/index.sqlite`: a disposable cache of reduce(). Deleting it is always safe.

`refresh()` rebuilds it from the logs into a new file and swaps it in, so a reader never sees half an index. The readers
rebuild on their own when the logs have changed since the last build, or when the file is missing or unreadable.

The `hashes` table is readiness's sha256 cache (see readiness.py). It is not derived from the logs, so a rebuild carries it over
from the old file; losing it only costs hashing the files again.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

from .log import log_files

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE artifacts (id TEXT PRIMARY KEY, kind TEXT, title TEXT, stage TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE conflicts (id TEXT PRIMARY KEY, artifact TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE decisions (id TEXT PRIMARY KEY, status TEXT, body TEXT NOT NULL);
CREATE TABLE tasks (id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE warnings (n INTEGER PRIMARY KEY, text TEXT NOT NULL);
"""
from .readiness import SCHEMA as HASHES_SCHEMA
INDEX_VERSION = "1"


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


class Index:
    def __init__(self, project):
        self.project = project

    @property
    def path(self) -> Path:
        return self.project.local_dir / "index.sqlite"

    def fingerprint(self) -> str:
        parts = []
        for p in log_files(self.project.log_dir):
            try:
                st = p.stat()
            except FileNotFoundError:
                continue
            parts.append([p.name, st.st_size, st.st_mtime_ns])
        return _dumps({"v": INDEX_VERSION, "logs": parts})

    def refresh(self) -> None:
        fp = self.fingerprint()
        state = self.project.state()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".index.", suffix=".sqlite.tmp", dir=self.path.parent)
        os.close(fd)
        tmp = Path(name)
        con = sqlite3.connect(tmp)
        try:
            con.executescript(SCHEMA)
            con.execute(HASHES_SCHEMA)
            self._carry_hashes(con)
            con.execute("INSERT INTO meta VALUES ('fingerprint', ?)", (fp,))
            con.executemany("INSERT INTO artifacts VALUES (?,?,?,?,?)",
                            [(a.id, a.kind, a.title, a.stage, _dumps(a.to_dict())) for a in state.artifacts.values()])
            con.executemany("INSERT INTO conflicts VALUES (?,?,?)",
                            [(c.id, c.artifact, _dumps(c.to_dict())) for c in state.conflicts])
            con.executemany("INSERT INTO decisions VALUES (?,?,?)",
                            [(d["id"], d.get("status"), _dumps(d)) for d in state.decisions.values()])
            con.executemany("INSERT INTO tasks VALUES (?,?)", [(t["id"], _dumps(t)) for t in state.tasks.values()])
            con.executemany("INSERT INTO warnings VALUES (?,?)", list(enumerate(state.warnings)))
            con.commit()
        except BaseException:
            con.close()
            tmp.unlink(missing_ok=True)
            raise
        con.close()
        os.replace(tmp, self.path)

    def _carry_hashes(self, con: sqlite3.Connection) -> None:
        """Copy the hash cache from the index being replaced (a damaged or missing one just starts empty)."""
        if not self.path.exists():
            return
        try:
            con.execute("ATTACH DATABASE ? AS old", (str(self.path),))
        except sqlite3.Error:
            return
        try:
            con.execute("INSERT OR IGNORE INTO hashes SELECT path, size, mtime_ns, sha256 FROM old.hashes")
        except sqlite3.Error:
            pass
        finally:
            try:
                con.commit()
                con.execute("DETACH DATABASE old")
            except sqlite3.Error:
                pass

    def _connect(self) -> sqlite3.Connection:
        """An up-to-date index, rebuilt when missing, stale or unreadable."""
        for attempt in range(2):
            if self.path.exists():
                try:
                    con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
                    row = con.execute("SELECT value FROM meta WHERE key='fingerprint'").fetchone()
                    if row and row[0] == self.fingerprint():
                        return con
                    con.close()
                except sqlite3.DatabaseError:
                    pass  # a damaged cache is just a cache: rebuild it
            if attempt == 0:
                self.refresh()
        raise RuntimeError(f"could not build {self.path}")

    def _rows(self, sql: str) -> list[dict]:
        con = self._connect()
        try:
            return [json.loads(r[0]) for r in con.execute(sql)]
        finally:
            con.close()

    def artifacts(self) -> list[dict]:
        return self._rows("SELECT body FROM artifacts ORDER BY id")

    def conflicts(self) -> list[dict]:
        return self._rows("SELECT body FROM conflicts ORDER BY id")

    def decisions(self) -> list[dict]:
        return self._rows("SELECT body FROM decisions ORDER BY id")

    def tasks(self) -> list[dict]:
        return self._rows("SELECT body FROM tasks ORDER BY id")

    def warnings(self) -> list[str]:
        con = self._connect()
        try:
            return [r[0] for r in con.execute("SELECT text FROM warnings ORDER BY n")]
        finally:
            con.close()
