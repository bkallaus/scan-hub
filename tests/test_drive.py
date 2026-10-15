import time

import pytest

from app import drive as drive_mod
from app.config import settings
from app.drive import AuthError, Drive, Uploader
from app.store import store


class Resp:
    def __init__(self, status=200, json=None, headers=None, text=""):
        self.status_code = status
        self._json = json
        self.headers = {"content-type": "application/json", **(headers or {})}
        self.text = text or (str(json) if json is not None else "")
        self.content = self.text.encode()

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHTTP:
    """Records calls; answers from a list of (method, url-substring, Resp)."""

    def __init__(self, routes):
        self.routes = list(routes)
        self.calls = []

    def _answer(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for i, (m, frag, resp) in enumerate(self.routes):
            if m == method and frag in url:
                if isinstance(resp, list):
                    return resp.pop(0) if len(resp) > 1 else resp[0]
                return resp
        raise AssertionError(f"unexpected {method} {url}")

    def get(self, url, **kw):
        return self._answer("GET", url, **kw)

    def post(self, url, **kw):
        return self._answer("POST", url, **kw)

    def put(self, url, **kw):
        return self._answer("PUT", url, **kw)


@pytest.fixture
def d(monkeypatch):
    settings.update({"drive": {"client_id": "cid", "client_secret": "sec"}})
    drv = Drive()
    drv.token = {"access_token": "at", "refresh_token": "rt", "expires_at": time.time() + 600}
    drv.folders = {}
    monkeypatch.setattr(drive_mod, "drive", drv)
    return drv


def test_access_token_cached(d):
    d.http = FakeHTTP([])
    assert d.access_token() == "at"


def test_access_token_refreshes(d):
    d.token["expires_at"] = 0
    d.http = FakeHTTP([("POST", "oauth2.googleapis.com/token", Resp(200, {"access_token": "new", "expires_in": 3600}))])
    assert d.access_token() == "new"
    assert d.token["expires_at"] > time.time()


def test_revoked_refresh_token_disconnects(d):
    d.token["expires_at"] = 0
    d.http = FakeHTTP([("POST", "/token", Resp(400, {"error": "invalid_grant"}))])
    with pytest.raises(AuthError):
        d.access_token()
    assert not d.connected and "expired or was revoked" in d.last_error


def test_refresh_with_non_json_error_raises_autherror(d):
    d.token["expires_at"] = 0
    d.http = FakeHTTP([("POST", "/token", Resp(502, None, {"content-type": "text/html"}, "<html>Bad Gateway</html>"))])
    with pytest.raises(AuthError):
        d.access_token()
    assert d.connected  # a Google outage must not throw away the refresh token


def test_ensure_folder_creates_and_caches(d):
    d.http = FakeHTTP([
        ("GET", "/drive/v3/files", Resp(200, {"files": []})),
        ("POST", "/drive/v3/files", [Resp(200, {"id": "F1"}), Resp(200, {"id": "F2"})]),
    ])
    assert d.ensure_folder("Scans/PDF") == "F2"
    assert d.folders == {"Scans": "F1", "Scans/PDF": "F2"}
    n = len(d.http.calls)
    assert d.ensure_folder("Scans/PDF") == "F2"
    assert len(d.http.calls) == n  # cached and recently verified


def test_find_folder_escapes_quotes(d):
    d.http = FakeHTTP([("GET", "/drive/v3/files", Resp(200, {"files": [{"id": "X"}]}))])
    assert d._find_folder("Bob's", "root") == "X"
    assert "name = 'Bob\\'s'" in d.http.calls[0][2]["params"]["q"]


def test_upload_resumable(d, tmp_path):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"%PDF data")
    d.folders = {"Scans": "F1"}
    d._verified = {"F1": time.time()}
    d.http = FakeHTTP([
        ("POST", "upload/drive/v3/files", Resp(200, {}, {"Location": "https://upload/session"})),
        ("PUT", "upload/session", Resp(200, {"id": "FILE", "webViewLink": "https://drive/x"})),
    ])
    assert d.upload(f, "Scans") == ("FILE", "https://drive/x")
    meta = d.http.calls[0][2]
    assert meta["json"] == {"name": "a.pdf", "parents": ["F1"]}
    assert meta["headers"]["X-Upload-Content-Type"] == "application/pdf"


def test_401_drops_access_token(d):
    with pytest.raises(AuthError):
        d._check(Resp(401, {}))
    assert "access_token" not in d.token and d.connected


def test_uploader_success_keeps_local_copy(d, monkeypatch, tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"x")
    iid = store.add(f, f.name, "pdf", "scan", "Scans/PDF", "pending")
    monkeypatch.setattr(d, "upload", lambda p, folder: ("ID", "LINK"))
    Uploader()._do(store.claim_next())
    item = store.get(iid)
    assert item["status"] == "done" and item["drive_link"] == "LINK"
    assert f.exists()


def test_uploader_removes_scan_when_not_keeping_local(d, monkeypatch, tmp_path):
    settings.update({"drive": {"keep_local_scans": False}})
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"x")
    store.add(f, f.name, "pdf", "scan", "Scans/PDF", "pending")
    monkeypatch.setattr(d, "upload", lambda p, folder: ("ID", "LINK"))
    Uploader()._do(store.claim_next())
    assert not f.exists()


def test_uploader_backoff_then_error(d, monkeypatch, tmp_path):
    f = tmp_path / "x.pdf"
    f.write_bytes(b"x")
    iid = store.add(f, f.name, "pdf", "scan", "Scans/PDF", "pending")

    def boom(*a):
        raise RuntimeError("network down")
    monkeypatch.setattr(d, "upload", boom)
    up = Uploader()
    up._do(store.get(iid))
    item = store.get(iid)
    assert item["status"] == "pending" and item["attempts"] == 1 and item["next_try"] > time.time() + 20
    store.update(iid, attempts=9)
    up._do(store.get(iid))
    assert store.get(iid)["status"] == "error"


def test_uploader_auth_error_does_not_count_attempt(d, monkeypatch, tmp_path):
    f = tmp_path / "x.pdf"
    f.write_bytes(b"x")
    iid = store.add(f, f.name, "pdf", "scan", "Scans/PDF", "pending")
    monkeypatch.setattr(d, "upload", lambda *a: (_ for _ in ()).throw(AuthError("expired")))
    Uploader()._do(store.get(iid))
    assert store.get(iid)["attempts"] == 0 and store.get(iid)["status"] == "pending"


def test_uploader_missing_file(d):
    iid = store.add("/nope/x.pdf", "x.pdf", "pdf", "scan", "Scans/PDF", "pending")
    Uploader()._do(store.get(iid))
    assert store.get(iid)["status"] == "error"


def test_scan_deleted_after_upload_when_not_kept(d, monkeypatch, tmp_path):
    settings.update({"drive": {"keep_local_scans": False}})
    f = tmp_path / "x.pdf"
    f.write_bytes(b"x")
    store.add(f, f.name, "pdf", "scan", "Scans/PDF", "pending")
    monkeypatch.setattr(d, "upload", lambda *a: ("ID", "L"))
    Uploader()._do(store.claim_next())
    assert not f.exists()


def test_jwt_email():
    import base64
    import json
    payload = base64.urlsafe_b64encode(json.dumps({"email": "me@example.com"}).encode()).rstrip(b"=").decode()
    assert drive_mod._jwt_email(f"h.{payload}.s") == "me@example.com"
    assert drive_mod._jwt_email("garbage") is None
