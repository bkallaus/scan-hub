import os
import time

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
    store.add("/x/a.pdf", "a.pdf", "pdf", "scan", "f", "error")
    store.add("/x/b.pdf", "b.pdf", "pdf", "scan", "f", "done")
    store.retry_all_errors()
    assert store.counts() == {"pending": 1, "done": 1}

