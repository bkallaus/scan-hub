"""SQLite-backed record of scanned files and their upload state."""
import logging
import sqlite3
import threading
import time
from collections import deque

from .config import STATE_DIR

# ---------------------------------------------------------------- logging
LOG_BUFFER = deque(maxlen=400)


class _BufferHandler(logging.Handler):
    def emit(self, record):
        LOG_BUFFER.append({
            "t": record.created,
            "level": record.levelname,
            "name": record.name.split(".")[-1],
            "msg": self.format(record),
        })


def setup_logging():
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    bh = _BufferHandler()
    bh.setFormatter(logging.Formatter("%(message)s"))
    root.handlers = [sh, bh]
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


# ---------------------------------------------------------------- items
SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,          -- pdf | photo | file
    source TEXT NOT NULL,        -- scan (older databases may also hold "watch")
    folder TEXT NOT NULL,        -- Drive folder path
    status TEXT NOT NULL,        -- pending | uploading | done | error | local
    attempts INTEGER DEFAULT 0,
    next_try REAL DEFAULT 0,
    drive_id TEXT,
    drive_link TEXT,
    error TEXT,
    size INTEGER,
    pages INTEGER,
    fingerprint TEXT,
    created REAL,
    updated REAL
);
CREATE INDEX IF NOT EXISTS items_status ON items(status, next_try);
CREATE INDEX IF NOT EXISTS items_fp ON items(fingerprint);
"""


class Store:
    def __init__(self):
        self._lock = threading.Lock()
        self.db = sqlite3.connect(STATE_DIR / "scanhub.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        # Anything mid-upload when we stopped goes back to pending.
        self.db.execute("UPDATE items SET status='pending' WHERE status='uploading'")
        self.db.commit()

    def add(self, path, name, kind, source, folder, status, size=None, pages=None, fingerprint=None):
        now = time.time()
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO items(path,name,kind,source,folder,status,size,pages,fingerprint,created,updated)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (str(path), name, kind, source, folder, status, size, pages, fingerprint, now, now),
            )
            self.db.commit()
            return cur.lastrowid

    def update(self, item_id, **fields):
        fields["updated"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self.db.execute(f"UPDATE items SET {cols} WHERE id=?", (*fields.values(), item_id))
            self.db.commit()

    def get(self, item_id):
        with self._lock:
            r = self.db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            return dict(r) if r else None

    def claim_next(self):
        """Atomically take the next pending item that is due."""
        with self._lock:
            r = self.db.execute(
                "SELECT * FROM items WHERE status='pending' AND next_try<=? ORDER BY id LIMIT 1",
                (time.time(),),
            ).fetchone()
            if not r:
                return None
            self.db.execute("UPDATE items SET status='uploading', updated=? WHERE id=?", (time.time(), r["id"]))
            self.db.commit()
            return dict(r)

    def has_fingerprint(self, fp):
        with self._lock:
            return self.db.execute("SELECT 1 FROM items WHERE fingerprint=? LIMIT 1", (fp,)).fetchone() is not None

    def recent(self, limit=50):
        with self._lock:
            rows = self.db.execute("SELECT * FROM items ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def counts(self):
        with self._lock:
            rows = self.db.execute("SELECT status, COUNT(*) c FROM items GROUP BY status").fetchall()
            return {r["status"]: r["c"] for r in rows}

    def retry(self, item_id):
        """Queue an item again. Only failed or local-only items: retrying one that
        is uploaded or uploading would put a duplicate copy in Drive."""
        with self._lock:
            cur = self.db.execute(
                "UPDATE items SET status='pending', next_try=0, attempts=0, error=NULL, updated=?"
                " WHERE id=? AND status IN ('error','local','pending')", (time.time(), item_id))
            self.db.commit()
            return cur.rowcount > 0

    def retry_all_errors(self):
        with self._lock:
            self.db.execute("UPDATE items SET status='pending', next_try=0, attempts=0, error=NULL, updated=?"
                            " WHERE status='error'", (time.time(),))
            self.db.commit()

    def queue_local(self):
        """Items kept local because Drive upload was off/not connected -> queue them."""
        with self._lock:
            self.db.execute("UPDATE items SET status='pending', next_try=0 WHERE status='local'")
            self.db.commit()


store = Store()
