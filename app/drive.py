"""Google sign-in (OAuth device flow) and Drive uploads.

Uses the "TVs and Limited Input devices" OAuth client type so sign-in works from
any phone/computer without the container needing a public URL or HTTPS. The
drive.file scope only lets this app see files and folders it created itself.
"""
import base64
import json
import logging
import mimetypes
import os
import threading
import time
from pathlib import Path

import requests

from .config import STATE_DIR, settings
from .store import store

log = logging.getLogger("drive")

DEVICE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
SCOPES = "openid email https://www.googleapis.com/auth/drive.file"
FOLDER_MIME = "application/vnd.google-apps.folder"
TOKEN_FILE = STATE_DIR / "google_token.json"
FOLDERS_FILE = STATE_DIR / "drive_folders.json"


class AuthError(Exception):
    pass


FOLDER_RECHECK_SECONDS = 600  # re-verify cached folder ids at most this often


def _jwt_email(id_token):
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("email")
    except Exception:
        return None


class Drive:
    def __init__(self):
        self._lock = threading.RLock()
        self.token = self._load(TOKEN_FILE)
        self.folders = self._load(FOLDERS_FILE)
        self.pending = None   # device-flow state shown in the UI
        self.last_error = None
        self._verified = {}   # folder id -> time it was last confirmed to exist
        self.http = requests.Session()

    @staticmethod
    def _load(path):
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}

    @staticmethod
    def _save(path, data):
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(path)

    # ------------------------------------------------------------ status
    @property
    def connected(self):
        return bool(self.token.get("refresh_token"))

    def status(self):
        s = settings.get()["drive"]
        p = None
        pend = self.pending  # local copy: the polling thread may clear it
        if pend:
            p = {k: pend[k] for k in ("user_code", "verification_url", "state", "message")}
            p["expires_in"] = max(0, int(pend["expires_at"] - time.time()))
        return {
            "configured": bool(s["client_id"] and s["client_secret"]),
            "connected": self.connected,
            "email": self.token.get("email"),
            "pending": p,
            "error": self.last_error,
        }

    # ------------------------------------------------------------ device flow
    def start_device_flow(self):
        s = settings.get()["drive"]
        if not (s["client_id"] and s["client_secret"]):
            raise AuthError("Enter your Google OAuth client ID and secret first.")
        r = self.http.post(DEVICE_URL, data={"client_id": s["client_id"], "scope": SCOPES}, timeout=20)
        if r.status_code != 200:
            raise AuthError(f"Google rejected the request: {r.text[:300]}")
        d = r.json()
        self.pending = {
            "device_code": d["device_code"],
            "user_code": d["user_code"],
            "verification_url": d.get("verification_url") or d.get("verification_uri"),
            "interval": int(d.get("interval", 5)),
            "expires_at": time.time() + int(d.get("expires_in", 1800)),
            "state": "waiting",
            "message": "Enter the code on Google's page",
        }
        threading.Thread(target=self._poll_device, args=(self.pending,), daemon=True).start()
        return self.status()

    def _poll_device(self, pend):
        s = settings.get()["drive"]
        while self.pending is pend and time.time() < pend["expires_at"]:
            time.sleep(pend["interval"])
            try:
                r = self.http.post(TOKEN_URL, data={
                    "client_id": s["client_id"], "client_secret": s["client_secret"],
                    "device_code": pend["device_code"],
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                }, timeout=20)
            except requests.RequestException as e:
                log.warning("token poll failed: %s", e)
                continue
            d = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            err = d.get("error")
            if r.status_code == 200 and "access_token" in d:
                if not d.get("refresh_token"):
                    pend["state"] = "failed"
                    pend["message"] = "Google didn't return a long-lived token. Remove ScanHub's access at myaccount.google.com/permissions and connect again."
                    log.warning(pend["message"])
                    return
                with self._lock:
                    self.token = {
                        "access_token": d["access_token"],
                        "refresh_token": d.get("refresh_token"),
                        "expires_at": time.time() + int(d.get("expires_in", 3600)) - 60,
                        "email": _jwt_email(d.get("id_token", "")),
                        "client_id": s["client_id"],
                    }
                    self._save(TOKEN_FILE, self.token)
                    self.folders = {}
                    self._verified = {}
                    self._save(FOLDERS_FILE, self.folders)
                self.pending = None
                self.last_error = None
                log.info("Connected to Google Drive as %s", self.token.get("email") or "(unknown)")
                return
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                pend["interval"] += 5
                continue
            pend["state"] = "failed"
            pend["message"] = {"access_denied": "Access was denied on the Google page.",
                               "expired_token": "The code expired. Start again."}.get(err, d.get("error_description") or err or r.text[:200])
            log.warning("Google sign-in failed: %s", pend["message"])
            return
        if self.pending is pend:
            pend["state"] = "failed"
            pend["message"] = "The code expired. Start again."

    def cancel_device_flow(self):
        self.pending = None

    def disconnect(self):
        tok = self.token.get("refresh_token") or self.token.get("access_token")
        if tok:
            try:
                self.http.post(REVOKE_URL, params={"token": tok}, timeout=15)
            except requests.RequestException:
                pass
        with self._lock:
            self.token = {}
            self.folders = {}
            self._verified = {}
            TOKEN_FILE.unlink(missing_ok=True)
            FOLDERS_FILE.unlink(missing_ok=True)
        log.info("Disconnected from Google Drive")

    # ------------------------------------------------------------ tokens
    def access_token(self):
        with self._lock:
            if not self.connected:
                raise AuthError("Google Drive is not connected")
            if self.token.get("access_token") and time.time() < self.token.get("expires_at", 0):
                return self.token["access_token"]
            s = settings.get()["drive"]
            r = self.http.post(TOKEN_URL, data={
                "client_id": s["client_id"], "client_secret": s["client_secret"],
                "refresh_token": self.token["refresh_token"], "grant_type": "refresh_token",
            }, timeout=20)
            try:
                d = r.json()
            except ValueError:  # e.g. an HTML 5xx page from a Google outage
                d = {}
            if r.status_code != 200 or "access_token" not in d:
                if d.get("error") in ("invalid_grant", "unauthorized_client", "invalid_client"):
                    self.token = {}
                    TOKEN_FILE.unlink(missing_ok=True)
                    self.last_error = ("Google sign-in expired or was revoked. Reconnect. "
                                       "(If this happens weekly, publish the OAuth app; see README.)")
                    log.error(self.last_error)
                raise AuthError(d.get("error_description") or d.get("error") or r.text[:200])
            self.token["access_token"] = d["access_token"]
            self.token["expires_at"] = time.time() + int(d.get("expires_in", 3600)) - 60
            self._save(TOKEN_FILE, self.token)
            return d["access_token"]

    def _headers(self):
        return {"Authorization": f"Bearer {self.access_token()}"}

    def _check(self, r):
        """Raise for HTTP errors; a 401 means our access token is stale, so drop it
        and let the next attempt refresh it instead of failing for up to an hour."""
        if r.status_code == 401:
            with self._lock:
                self.token.pop("access_token", None)
                self.token["expires_at"] = 0
            raise AuthError("Google access token expired; will refresh and retry")
        r.raise_for_status()
        return r

    # ------------------------------------------------------------ folders
    def ensure_folder(self, path):
        """Return the Drive id for a slash path like 'Scans/PDF', creating as needed."""
        parent = "root"
        built = []
        for part in [p for p in path.split("/") if p]:
            built.append(part)
            key = "/".join(built)
            fid = self.folders.get(key)
            if fid and time.time() - self._verified.get(fid, 0) > FOLDER_RECHECK_SECONDS:
                if self._folder_ok(fid):
                    self._verified[fid] = time.time()
                else:
                    fid = None
            if not fid:
                fid = self._find_folder(part, parent) or self._create_folder(part, parent)
                with self._lock:
                    self.folders[key] = fid
                    self._save(FOLDERS_FILE, self.folders)
                self._verified[fid] = time.time()
            parent = fid
        return parent

    def _folder_ok(self, fid):
        r = self.http.get(f"{API}/files/{fid}", params={"fields": "id,trashed"}, headers=self._headers(), timeout=20)
        if r.status_code in (403, 404):
            return False
        self._check(r)
        return not r.json().get("trashed")

    def _find_folder(self, name, parent):
        safe = name.replace("\\", "\\\\").replace("'", "\\'")  # Drive query escaping
        q = (f"name = '{safe}' and mimeType = '{FOLDER_MIME}' "
             f"and '{parent}' in parents and trashed = false")
        r = self.http.get(f"{API}/files", params={"q": q, "fields": "files(id)", "spaces": "drive"},
                          headers=self._headers(), timeout=20)
        self._check(r)
        files = r.json().get("files", [])
        return files[0]["id"] if files else None

    def _create_folder(self, name, parent):
        r = self.http.post(f"{API}/files", params={"fields": "id"}, headers=self._headers(), timeout=20,
                           json={"name": name, "mimeType": FOLDER_MIME, "parents": [parent]})
        self._check(r)
        log.info("Created Drive folder '%s'", name)
        return r.json()["id"]

    # ------------------------------------------------------------ upload
    def upload(self, path, folder_path):
        path = Path(path)
        folder_id = self.ensure_folder(folder_path)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        size = path.stat().st_size
        h = self._headers()
        r = self.http.post(UPLOAD, params={"uploadType": "resumable", "fields": "id,webViewLink"},
                           headers={**h, "X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(size),
                                    "Content-Type": "application/json; charset=UTF-8"},
                           json={"name": path.name, "parents": [folder_id]}, timeout=30)
        if r.status_code == 404:
            # Upload folder vanished between the check and the upload: forget it.
            self._verified.pop(folder_id, None)
            with self._lock:
                self.folders = {k: v for k, v in self.folders.items() if v != folder_id}
                self._save(FOLDERS_FILE, self.folders)
        self._check(r)
        session = r.headers["Location"]
        with open(path, "rb") as f:
            r = self.http.put(session, data=f, headers={"Content-Length": str(size), "Content-Type": mime},
                              timeout=(30, 3600))
        self._check(r)
        d = r.json()
        return d["id"], d.get("webViewLink")


drive = Drive()


# ---------------------------------------------------------------- upload worker
class Uploader(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="uploader")
        self.wake = threading.Event()

    def run(self):
        while True:
            try:
                if not drive.connected:
                    self.wake.wait(10)
                    self.wake.clear()
                    continue
                item = store.claim_next()
                if not item:
                    self.wake.wait(5)
                    self.wake.clear()
                    continue
                self._do(item)
            except Exception:
                # Never let one bad item stop all future uploads.
                log.exception("Uploader error")
                time.sleep(10)

    def _do(self, item):
        path = Path(item["path"])
        if not path.is_file():
            store.update(item["id"], status="error", error="Local file no longer exists")
            return
        try:
            fid, link = drive.upload(path, item["folder"])
        except AuthError as e:
            store.update(item["id"], status="pending", next_try=time.time() + 30, error=str(e))
            return
        except Exception as e:  # network / API error -> retry with backoff
            attempts = item["attempts"] + 1
            delay = min(3600, 30 * 2 ** (attempts - 1))
            status = "pending" if attempts < 10 else "error"
            store.update(item["id"], status=status, attempts=attempts, next_try=time.time() + delay, error=str(e)[:300])
            log.warning("Upload of %s failed (attempt %d): %s", path.name, attempts, e)
            return
        store.update(item["id"], status="done", drive_id=fid, drive_link=link, error=None)
        log.info("Uploaded %s → Drive:/%s", path.name, item["folder"])
        self._after(item, path)

    @staticmethod
    def _after(item, path):
        try:
            # Only delete our own scans (older databases may hold other sources).
            if item["source"] == "scan" and not settings.get()["drive"]["keep_local_scans"]:
                path.unlink()
        except Exception as e:
            log.warning("Post-upload cleanup for %s failed: %s", path.name, e)


uploader = Uploader()
