"""Web UI + JSON API. Run with: python3 -m app.main"""
import base64
import logging
import secrets
import time
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .config import DATA_DIR, SIMULATE, WEB_PASSWORD, WEB_PORT, settings
from .store import LOG_BUFFER, setup_logging, store

setup_logging()
log = logging.getLogger("web")

from .drive import AuthError, drive, uploader  # noqa: E402
from .scanner import processor, scanner  # noqa: E402
from .watcher import watcher  # noqa: E402

STATIC = Path(__file__).parent / "static"


class BasicAuth(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if WEB_PASSWORD and request.url.path != "/healthz":
            hdr = request.headers.get("authorization", "")
            ok = False
            if hdr.lower().startswith("basic "):
                try:
                    _, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
                    ok = secrets.compare_digest(pw, WEB_PASSWORD)
                except Exception:
                    ok = False
            if not ok:
                return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="ScanHub"'})
        return await call_next(request)


def err(msg, code=400):
    return JSONResponse({"error": msg}, status_code=code)


async def json_body(request):
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def healthz(_):
    return JSONResponse({"ok": True, "scanner": scanner.state})


async def index(_):
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


def status(_):
    s = settings.get()
    return JSONResponse({
        "time": time.time(),
        "mode": s["mode"],
        "paused": s["scanner"]["paused"],
        "simulate": SIMULATE,
        "scanner": scanner.status(),
        "drive": drive.status(),
        "queue": store.counts(),
    })


def get_settings(_):
    return JSONResponse(settings.public())


async def put_settings(request: Request):
    patch = await json_body(request)
    if patch is None:
        return err("Expected a JSON object")
    return await run_in_threadpool(_apply_settings, patch)


def _apply_settings(patch):
    before = settings.get()
    try:
        new = settings.update(patch)
    except ValueError as e:
        return err(str(e))
    sc_b, sc_a = before["scanner"], new["scanner"]
    if (sc_b["ip"], sc_b["device"], sc_b["detection"]) != (sc_a["ip"], sc_a["device"], sc_a["detection"]):
        scanner.request_rediscover()
    elif before["mode"] != new["mode"] or sc_b["paused"] != sc_a["paused"]:
        scanner.wake.set()
    if before["drive"]["client_id"] != new["drive"]["client_id"] and drive.connected:
        drive.disconnect()
    return JSONResponse(settings.public())


def scan_now(_):
    scanner.request_scan()
    return JSONResponse({"ok": True})


def finish_doc(_):
    scanner.finish_document()
    return JSONResponse({"ok": True})


def rediscover(_):
    scanner.request_rediscover()
    return JSONResponse({"ok": True})


def devices(_):
    return JSONResponse({
        "selected": scanner.device,
        "devices": list(scanner.devices),
        "options": [o.as_dict() for o in scanner.opts.values()],
    })


def drive_connect(_):
    try:
        return JSONResponse(drive.start_device_flow())
    except AuthError as e:
        return err(str(e))
    except Exception as e:
        return err(f"Could not reach Google: {e}", 502)


def drive_cancel(_):
    drive.cancel_device_flow()
    return JSONResponse(drive.status())


def drive_disconnect(_):
    drive.disconnect()
    return JSONResponse(drive.status())


def _item_out(i):
    return {k: i[k] for k in ("id", "name", "kind", "source", "folder", "status", "attempts",
                              "drive_link", "error", "size", "pages", "created", "updated")}


def items(request: Request):
    try:
        limit = max(1, min(200, int(request.query_params.get("limit", 40))))
    except ValueError:
        limit = 40
    return JSONResponse([_item_out(i) for i in store.recent(limit)])


def item_retry(request: Request):
    iid = int(request.path_params["id"])
    if not store.get(iid):
        return err("not found", 404)
    if not store.retry(iid):
        return err("Already uploaded or uploading", 409)
    uploader.wake.set()
    return JSONResponse({"ok": True})


def retry_errors(_):
    store.retry_all_errors()
    uploader.wake.set()
    return JSONResponse({"ok": True})


def item_file(request: Request):
    i = store.get(int(request.path_params["id"]))
    if not i:
        return err("not found", 404)
    p = Path(i["path"]).resolve()
    if not p.is_file() or DATA_DIR.resolve() not in p.parents:
        return err("file no longer on disk", 404)
    return FileResponse(p, filename=p.name, content_disposition_type="inline")


def logs(_):
    return JSONResponse(list(LOG_BUFFER)[-200:])


async def sim_load(request: Request):
    if not SIMULATE:
        return err("only available with SIMULATE_SCANNER=1", 404)
    body = await json_body(request) or {}
    try:
        scanner.backend.load(min(50, int(body.get("pages", 3))))
    except (TypeError, ValueError):
        return err("pages must be a number")
    scanner.wake.set()
    return JSONResponse({"ok": True})


routes = [
    Route("/", index),
    Route("/healthz", healthz),
    Route("/api/status", status),
    Route("/api/settings", get_settings, methods=["GET"]),
    Route("/api/settings", put_settings, methods=["PUT"]),
    Route("/api/scan", scan_now, methods=["POST"]),
    Route("/api/document/finish", finish_doc, methods=["POST"]),
    Route("/api/scanner/rediscover", rediscover, methods=["POST"]),
    Route("/api/scanner/devices", devices),
    Route("/api/drive/connect", drive_connect, methods=["POST"]),
    Route("/api/drive/cancel", drive_cancel, methods=["POST"]),
    Route("/api/drive/disconnect", drive_disconnect, methods=["POST"]),
    Route("/api/items", items),
    Route("/api/items/retry-errors", retry_errors, methods=["POST"]),
    Route("/api/items/{id:int}/retry", item_retry, methods=["POST"]),
    Route("/files/{id:int}", item_file),
    Route("/api/logs", logs),
    Route("/api/simulate/load", sim_load, methods=["POST"]),
    Mount("/static", StaticFiles(directory=STATIC), name="static"),
]

app = Starlette(routes=routes, middleware=[Middleware(BasicAuth)])


def main():
    log.info("FF-680W ScanHub starting on port %d%s", WEB_PORT, " (SIMULATED scanner)" if SIMULATE else "")
    processor.start()
    scanner.start()
    uploader.start()
    watcher.start()
    uvicorn.run(app, host="0.0.0.0", port=WEB_PORT, log_level="warning")


if __name__ == "__main__":
    main()
