import base64

import pytest
from starlette.testclient import TestClient

from app import main
from app.config import DATA_DIR
from app.store import store


@pytest.fixture
def client():
    return TestClient(main.app)


def test_healthz_and_index(client):
    assert client.get("/healthz").json()["ok"] is True
    r = client.get("/")
    assert r.status_code == 200 and "ScanHub" in r.text
    js = client.get("/static/app.js")
    assert js.status_code == 200 and js.headers["cache-control"] == "no-cache"
    # unchanged files revalidate cheaply
    assert client.get("/static/app.js", headers={"If-None-Match": js.headers["etag"]}).status_code == 304


def test_status_shape(client):
    st = client.get("/api/status").json()
    assert {"mode", "paused", "simulate", "scanner", "drive", "queue"} <= st.keys()
    assert st["simulate"] is True


def test_settings_roundtrip_and_validation(client):
    r = client.put("/api/settings", json={"mode": "photo"})
    assert r.status_code == 200 and r.json()["mode"] == "photo"
    assert client.get("/api/settings").json()["mode"] == "photo"
    r = client.put("/api/settings", json={"mode": "fax"})
    assert r.status_code == 400 and "mode" in r.json()["error"]
    assert client.put("/api/settings", content=b"not json", headers={"content-type": "application/json"}).status_code == 400
    assert client.put("/api/settings", json=[1, 2]).status_code == 400


def test_settings_ip_change_requests_rediscover(client):
    main.scanner.rediscover_request.clear()
    client.put("/api/settings", json={"scanner": {"ip": "10.0.0.9"}})
    assert main.scanner.rediscover_request.is_set()


def test_items_and_retry(client):
    done = store.add("/x/a.pdf", "a.pdf", "pdf", "scan", "f", "done")
    bad = store.add("/x/b.pdf", "b.pdf", "pdf", "scan", "f", "error")
    items = client.get("/api/items?limit=abc").json()
    assert [i["id"] for i in items] == [bad, done]
    assert "path" not in items[0]  # local paths stay server-side
    assert client.post(f"/api/items/{done}/retry").status_code == 409
    assert client.post(f"/api/items/{bad}/retry").status_code == 200
    assert client.post("/api/items/99999/retry").status_code == 404


def test_file_download_restricted_to_data_dir(client, tmp_path):
    inside = DATA_DIR / "scans" / "x.pdf"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_bytes(b"%PDF")
    ok = store.add(inside, "x.pdf", "pdf", "scan", "f", "done")
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    bad = store.add(outside, "secret.txt", "file", "scan", "f", "done")
    assert client.get(f"/files/{ok}").content == b"%PDF"
    assert client.get(f"/files/{bad}").status_code == 404


def test_only_scans_open_inline(client):
    folder = DATA_DIR / "scans"
    folder.mkdir(parents=True, exist_ok=True)
    pdf, page = folder / "a.pdf", folder / "evil.html"
    pdf.write_bytes(b"%PDF")
    page.write_text("<script>alert(1)</script>")
    r = client.get(f"/files/{store.add(pdf, 'a.pdf', 'pdf', 'scan', 'f', 'done')}")
    assert r.headers["content-disposition"].startswith("inline")
    assert r.headers["x-content-type-options"] == "nosniff"
    r = client.get(f"/files/{store.add(page, 'evil.html', 'file', 'scan', 'f', 'done')}")
    assert r.headers["content-disposition"].startswith("attachment")


def test_sim_load_validation(client):
    assert client.post("/api/simulate/load", json={"pages": "x"}).status_code == 400
    assert client.post("/api/simulate/load", json={"pages": 2}).status_code == 200
    main.scanner.backend.load(0)


def test_drive_connect_without_client_is_400(client):
    r = client.post("/api/drive/connect")
    assert r.status_code == 400 and "client ID" in r.json()["error"]


def test_logs(client):
    assert isinstance(client.get("/api/logs").json(), list)


# ------------------------------------------------------------- auth / CSRF
def _basic(pw, user="x"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_basic_auth(client, monkeypatch):
    monkeypatch.setattr(main, "WEB_PASSWORD", "pässword")
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers=_basic("wrong")).status_code == 401
    assert client.get("/api/status", headers=_basic("pässword")).status_code == 200
    assert client.get("/api/status", headers={"Authorization": "Basic !!!"}).status_code == 401
    assert client.get("/healthz").status_code == 200  # Docker healthcheck needs no password


def test_cross_site_post_blocked(client):
    # A page on another site can submit a plain form POST to a LAN address
    # without any CORS preflight; it must not be able to start scans etc.
    r = client.post("/api/scan", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/drive/disconnect", headers={"Origin": "null"})
    assert r.status_code == 403


def test_same_origin_post_allowed(client):
    assert client.post("/api/scan", headers={"Origin": "http://testserver"}).status_code == 200
    assert client.post("/api/scan").status_code == 200  # curl / scripts send no Origin
    main.scanner.scan_request.clear()
