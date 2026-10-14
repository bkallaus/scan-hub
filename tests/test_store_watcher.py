import os
import time

from app import watcher as watcher_mod
from app.config import WATCH_DIR, settings
from app.store import store


def test_claim_next_is_one_shot_and_respects_next_try():
    a = store.add("/x/a.pdf", "a.pdf", "pdf", "scan", "Scans/PDF", "pending")
    b = store.add("/x/b.pdf", "b.pdf", "pdf", "scan", "Scans/PDF", "pending")
    store.update(a, next_try=time.time() + 60)
    got = store.claim_next()
    assert got["id"] == b
    assert store.get(b)["status"] == "uploading"
    assert store.claim_next() is None


def test_retry_only_for_failed_or_local():
    done = store.add("/x/a.pdf", "a.pdf", "pdf", "scan", "f", "done")
    err = store.add("/x/b.pdf", "b.pdf", "pdf", "scan", "f", "error")
    assert not store.retry(done)
    assert store.retry(err)
    assert store.get(err)["status"] == "pending" and store.get(err)["attempts"] == 0


def test_retry_all_errors_and_counts():
    store.add("/x/a", "a", "file", "watch", "f", "error")
    store.add("/x/b", "b", "file", "watch", "f", "done")
    store.retry_all_errors()
    assert store.counts() == {"pending": 1, "done": 1}


def _age(path, seconds):
    t = time.time() - seconds
    os.utime(path, (t, t))


def test_watcher_waits_for_stable_file_then_queues_once(monkeypatch):
    settings.update({"watch": {"stable_seconds": 1}})
    sub = WATCH_DIR / "Taxes" / "2025"
    sub.mkdir(parents=True, exist_ok=True)
    f = sub / "w2.pdf"
    f.write_bytes(b"%PDF-1.4 test")
    (WATCH_DIR / "partial.pdf.part").write_bytes(b"x")
    (WATCH_DIR / ".hidden").write_bytes(b"x")
    w = watcher_mod.Watcher()
    s = settings.get()
    clock = [1000.0]
    monkeypatch.setattr(watcher_mod.time, "time", lambda: clock[0])

    w.scan(s)                         # first sighting: just remember it
    assert store.recent() == []
    clock[0] += 0.5
    w.scan(s)                         # not stable long enough yet
    assert store.recent() == []
    clock[0] += 2
    w.scan(s)
    items = store.recent()
    assert [(i["name"], i["folder"], i["kind"], i["status"]) for i in items] == [
        ("w2.pdf", "Scans/Dropped/Taxes/2025", "pdf", "pending")]
    clock[0] += 5
    w.scan(s)                         # fingerprint dedupe
    assert len(store.recent()) == 1
    f.unlink()


def test_watcher_local_only_when_upload_off(monkeypatch):
    settings.update({"drive": {"upload_watch": False}, "watch": {"stable_seconds": 1}})
    f = WATCH_DIR / "photo.jpg"
    f.write_bytes(b"jpegdata")
    w = watcher_mod.Watcher()
    clock = [1000.0]
    monkeypatch.setattr(watcher_mod.time, "time", lambda: clock[0])
    w.scan(settings.get())
    clock[0] += 5
    w.scan(settings.get())
    assert store.recent()[0]["status"] == "local"
    f.unlink()


def test_watcher_skips_uploaded_folder():
    d = WATCH_DIR / "_uploaded"
    d.mkdir(parents=True, exist_ok=True)
    (d / "old.pdf").write_bytes(b"x")
    assert not any("_uploaded" in str(p) for p in watcher_mod.Watcher()._files())
