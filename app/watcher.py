"""Watched folder: anything dropped into /data/watch is uploaded to Drive.

Polls instead of using inotify so it also works when the folder is an SMB/NFS
mount from a NAS. Sub-folders are mirrored under the Drive target folder.
"""
import logging
import os
import threading
import time
from pathlib import Path

from .config import WATCH_DIR, settings
from .store import store

log = logging.getLogger("watcher")

IGNORE_SUFFIXES = (".part", ".tmp", ".crdownload", ".download", ".partial", ".swp")
IGNORE_NAMES = ("Thumbs.db", "desktop.ini")
SKIP_DIRS = ("_uploaded", "@eaDir", "#recycle", "$RECYCLE.BIN", "lost+found")


def _kind(name):
    n = name.lower()
    if n.endswith(".pdf"):
        return "pdf"
    if n.endswith((".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".webp")):
        return "photo"
    return "file"


def _wanted(name):
    return not (name.startswith(".") or name.startswith("~$") or name in IGNORE_NAMES
                or name.lower().endswith(IGNORE_SUFFIXES))


class Watcher(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="watcher")
        self.seen = {}  # path -> (size, mtime, time the size/mtime were last seen to change)

    def run(self):
        while True:
            s = settings.get()
            try:
                if s["watch"]["enabled"]:
                    self.scan(s)
            except Exception as e:
                log.error("watch folder error: %s", e)
            time.sleep(max(2, s["watch"]["interval_seconds"]))

    def _files(self):
        # os.walk lets us prune _uploaded (which grows forever) and NAS system folders.
        for root, dirs, files in os.walk(WATCH_DIR):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS]
            for f in files:
                if _wanted(f):
                    yield Path(root) / f

    def scan(self, s):
        now = time.time()
        present = set()
        for p in self._files():
            try:
                st = p.stat()
                rel = p.relative_to(WATCH_DIR)
                key = str(p)
                present.add(key)
                sig = (st.st_size, st.st_mtime)
                prev = self.seen.get(key)
                if not prev or prev[:2] != sig:
                    self.seen[key] = (*sig, now)
                    continue
                if now - prev[2] < s["watch"]["stable_seconds"] or st.st_size == 0:
                    continue
                fp = f"watch:{rel}:{st.st_size}:{int(st.st_mtime)}"
                if store.has_fingerprint(fp):
                    continue
                folder = s["drive"]["watch_folder"]
                if len(rel.parts) > 1:
                    folder = folder + "/" + "/".join(rel.parts[:-1])
                status = "pending" if s["drive"]["upload_watch"] else "local"
                store.add(p, p.name, _kind(p.name), "watch", folder, status, size=st.st_size, fingerprint=fp)
                log.info("Watch folder: queued %s", rel)
            except FileNotFoundError:
                continue  # removed while we were looking at it
            except Exception as e:
                log.warning("Watch folder: skipping %s: %s", p.name, e)
        for k in list(self.seen):
            if k not in present:
                del self.seen[k]


watcher = Watcher()
