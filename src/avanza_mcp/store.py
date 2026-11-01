"""Small key/value store for the Avanza session and the holdings snapshot.

The session holds live Avanza web cookies, so the directory is created 0700 and the database 0600.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, data_dir: str | os.PathLike[str]):
        self.dir = Path(data_dir).expanduser()
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        self.path = self.dir / "avanza-mcp.sqlite3"
        self.path.touch(mode=0o600, exist_ok=True)
        os.chmod(self.path, 0o600)
        with self._db() as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL)")

    def _db(self) -> sqlite3.Connection:
        return closing(sqlite3.connect(self.path))  # type: ignore[return-value]

    def get(self, key: str) -> Any:
        with self._db() as db:
            row = db.execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, value: Any) -> None:
        with self._db() as db, db:
            if value is None:
                db.execute("DELETE FROM kv WHERE k = ?", (key,))
            else:
                db.execute("REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value)))
