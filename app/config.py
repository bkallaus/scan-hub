"""Settings storage: JSON file under DATA_DIR with defaults and env overrides."""
import copy
import json
import logging
import os
import re
import shlex
import string
import threading
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CONFIG_DIR = DATA_DIR / "config"
STATE_DIR = DATA_DIR / "state"
SCANS_DIR = DATA_DIR / "scans"
WATCH_DIR = DATA_DIR / "watch"
WORK_DIR = DATA_DIR / "work"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
ENV_SEEN_FILE = CONFIG_DIR / "env_applied.json"

SIMULATE = os.environ.get("SIMULATE_SCANNER", "").lower() in ("1", "true", "yes")
WEB_PORT = int(os.environ.get("WEB_PORT", "8680"))
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")

SECRET_MASK = "••••••••"

DEFAULTS = {
    "mode": "document",  # "document" -> PDF, "photo" -> image files
    "scanner": {
        "ip": "",
        "device": "",            # SANE device name; blank = auto-discover
        "detection": "auto",     # auto | sensor | probe | manual
        "poll_seconds": 3,
        "paused": False,
        "extra_args": "",        # appended to every scanimage call
    },
    "document": {
        "resolution": 300,
        "color": "color",        # color | gray | bw
        "duplex": True,
        "skip_blank": True,
        "blank_sensitivity": 0.4,  # % of "ink" pixels below which a page is blank
        "jpeg_quality": 80,
        "ocr": False,
        "ocr_lang": "eng",
        "combine_seconds": 0,    # >0: batches within this window join one PDF
        "filename": "Scan_{date}_{time}",
    },
    "photo": {
        "resolution": 600,
        "format": "jpg",         # jpg | png | tiff
        "jpeg_quality": 92,
        "duplex": True,          # scan backs too (kept only if written on)
        "autocrop": True,
        "filename": "Photo_{date}_{time}_{n}",
    },
    "drive": {
        "client_id": "",
        "client_secret": "",
        "upload_scans": True,
        "pdf_folder": "Scans/PDF",
        "photo_folder": "Scans/Photos",
        "upload_watch": True,
        "watch_folder": "Scans/Dropped",
        "keep_local_scans": True,
    },
    "watch": {
        "enabled": True,
        "after_upload": "move",  # move | delete | keep
        "stable_seconds": 5,
        "interval_seconds": 5,
    },
}

ENV_OVERRIDES = {
    "SCANNER_IP": ("scanner", "ip"),
    "SCANNER_DEVICE": ("scanner", "device"),
    "GOOGLE_CLIENT_ID": ("drive", "client_id"),
    "GOOGLE_CLIENT_SECRET": ("drive", "client_secret"),
}


def _deep_merge(base, patch):
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _coerce(default, value):
    """Coerce incoming JSON values to the default's type."""
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes", "on")
        return bool(value)
    if isinstance(default, int):
        return int(float(value))
    if isinstance(default, float):
        return float(value)
    if isinstance(default, str):
        return str(value)
    return value


def _clean(defaults, patch):
    """Keep only known keys, coerce types."""
    if not isinstance(patch, dict):
        raise ValueError("Settings must be a JSON object")
    out = {}
    for k, v in patch.items():
        if k not in defaults:
            continue
        if isinstance(defaults[k], dict):
            if isinstance(v, dict):
                out[k] = _clean(defaults[k], v)
        else:
            try:
                out[k] = _coerce(defaults[k], v)
            except (TypeError, ValueError, OverflowError):
                raise ValueError(f"Invalid value for {k}: {v!r}")
    return out


CHOICES = {
    ("mode",): {"document", "photo"},
    ("scanner", "detection"): {"auto", "sensor", "probe", "manual"},
    ("document", "color"): {"color", "gray", "bw"},
    ("photo", "format"): {"jpg", "png", "tiff"},
    ("watch", "after_upload"): {"move", "delete", "keep"},
}


def _validate(s):
    for path, allowed in CHOICES.items():
        node = s
        for p in path:
            node = node[p]
        if node not in allowed:
            raise ValueError(f"{'.'.join(path)} must be one of {sorted(allowed)}")
    s["scanner"]["poll_seconds"] = max(1, min(60, s["scanner"]["poll_seconds"]))
    for sect in ("document", "photo"):
        s[sect]["resolution"] = max(50, min(1200, s[sect]["resolution"]))
        s[sect]["jpeg_quality"] = max(30, min(100, s[sect]["jpeg_quality"]))
    s["document"]["combine_seconds"] = max(0, min(600, s["document"]["combine_seconds"]))
    s["document"]["blank_sensitivity"] = max(0.0, min(20.0, s["document"]["blank_sensitivity"]))
    s["watch"]["stable_seconds"] = max(1, min(600, s["watch"]["stable_seconds"]))
    s["watch"]["interval_seconds"] = max(2, min(600, s["watch"]["interval_seconds"]))
    ip = s["scanner"]["ip"].strip()
    if ip and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:\-]*", ip):
        raise ValueError("Scanner IP must be an IP address or host name, e.g. 192.168.1.50")
    s["scanner"]["ip"] = ip
    lang = s["document"]["ocr_lang"].strip() or "eng"
    if not re.fullmatch(r"[A-Za-z_]+(\+[A-Za-z_]+)*", lang):
        raise ValueError("OCR language must be Tesseract codes like eng or eng+spa")
    s["document"]["ocr_lang"] = lang
    for sect, key in (("drive", "pdf_folder"), ("drive", "photo_folder"), ("drive", "watch_folder")):
        s[sect][key] = "/".join(p.strip() for p in s[sect][key].split("/") if p.strip()) or "Scans"
    for sect in ("document", "photo"):
        tpl = s[sect]["filename"].strip()
        try:
            fields = [f for _, f, _, _ in string.Formatter().parse(tpl) if f is not None]
        except ValueError:
            raise ValueError(f"{sect} file name has an unmatched {{ or }}")
        bad = [f for f in fields if f not in ("date", "time", "n", "mode")]
        if bad:
            raise ValueError(f"{sect} file name: unknown field {{{bad[0]}}}; use {{date}} {{time}} {{n}} {{mode}}")
        if not tpl:
            raise ValueError(f"{sect} file name can't be empty")
        s[sect]["filename"] = tpl
    try:
        extra = shlex.split(s["scanner"]["extra_args"])
    except ValueError as e:
        raise ValueError(f"Extra scanimage options: {e}")
    # These would redirect where scans are written or which device is used,
    # so scans would silently go missing (or overwrite files in the container).
    for a in extra:
        if re.match(r"^(-[bdo]|--batch|--device-name|--output-file|--format|-L|--list-devices|-A|--all-options)", a):
            raise ValueError(f"Extra scanimage options: {a.split('=')[0]} is managed by ScanHub")


class Settings:
    def __init__(self):
        self._lock = threading.RLock()
        self._data = copy.deepcopy(DEFAULTS)
        for d in (CONFIG_DIR, STATE_DIR, SCANS_DIR, WATCH_DIR, WORK_DIR):
            d.mkdir(parents=True, exist_ok=True)
        if SETTINGS_FILE.exists():
            try:
                _deep_merge(self._data, _clean(DEFAULTS, json.loads(SETTINGS_FILE.read_text())))
            except Exception as e:
                bad = SETTINGS_FILE.with_suffix(".json.bad")
                SETTINGS_FILE.replace(bad)
                logging.getLogger("config").error(
                    "settings.json could not be read (%s); using defaults. The old file was kept as %s", e, bad.name)
        # An env var (docker-compose) is applied when it's new or has changed since
        # the last start, so editing SCANNER_IP in compose works, while changes
        # made later in the web page aren't overwritten on every restart.
        try:
            seen = json.loads(ENV_SEEN_FILE.read_text())
        except Exception:
            seen = {}
        for env, (sect, key) in ENV_OVERRIDES.items():
            val = os.environ.get(env, "").strip()
            if val and (val != seen.get(env) or not self._data[sect][key]):
                self._data[sect][key] = val
            seen[env] = val
        ENV_SEEN_FILE.write_text(json.dumps(seen))
        os.chmod(ENV_SEEN_FILE, 0o600)
        try:
            _validate(self._data)
        except ValueError as e:
            # A hand-edited settings file with bad values: fall back to defaults.
            logging.getLogger("config").error("Invalid saved settings (%s); using defaults", e)
            if SETTINGS_FILE.exists():
                SETTINGS_FILE.replace(SETTINGS_FILE.with_suffix(".json.bad"))
            self._data = copy.deepcopy(DEFAULTS)
            for env, (sect, key) in ENV_OVERRIDES.items():
                if seen.get(env):
                    self._data[sect][key] = seen[env]
            _validate(self._data)
        self._save()

    def _save(self):
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(SETTINGS_FILE)

    def get(self):
        with self._lock:
            return copy.deepcopy(self._data)

    def public(self):
        d = self.get()
        if d["drive"]["client_secret"]:
            d["drive"]["client_secret"] = SECRET_MASK
        return d

    def update(self, patch):
        with self._lock:
            if not isinstance(patch, dict):
                raise ValueError("Settings must be a JSON object")
            drv = patch.get("drive")
            if isinstance(drv, dict) and drv.get("client_secret") == SECRET_MASK:
                drv.pop("client_secret")
            new = copy.deepcopy(self._data)
            _deep_merge(new, _clean(DEFAULTS, patch))
            _validate(new)
            self._data = new
            self._save()
            return copy.deepcopy(new)


# One-tap scan presets shown on the web page. Each sets every option that
# shapes the result, so applying one always gives the same output.
PRESETS = [
    {"id": "doc_quick", "icon": "bolt", "title": "Quick",
     "desc": "Receipts, notes, mail",
     "settings": {"mode": "document", "document": {
         "resolution": 200, "color": "gray", "jpeg_quality": 70, "ocr": False,
         "duplex": True, "skip_blank": True}}},
    {"id": "doc_standard", "icon": "doc", "title": "Everyday", "recommended": True,
     "desc": "Letters, bills, forms",
     "settings": {"mode": "document", "document": {
         "resolution": 300, "color": "color", "jpeg_quality": 80, "ocr": False,
         "duplex": True, "skip_blank": True}}},
    {"id": "doc_high", "icon": "sparkle", "title": "High quality",
     "desc": "Contracts, certificates, art",
     "settings": {"mode": "document", "document": {
         "resolution": 600, "color": "color", "jpeg_quality": 92, "ocr": True,
         "duplex": True, "skip_blank": True}}},
    {"id": "photo_quick", "icon": "stack", "title": "Quick",
     "desc": "Big boxes of prints, fast",
     "settings": {"mode": "photo", "photo": {
         "resolution": 300, "format": "jpg", "jpeg_quality": 85, "autocrop": True, "duplex": False}}},
    {"id": "photo_standard", "icon": "image", "title": "Standard", "recommended": True,
     "desc": "Albums and sharing",
     "settings": {"mode": "photo", "photo": {
         "resolution": 600, "format": "jpg", "jpeg_quality": 92, "autocrop": True, "duplex": True}}},
    {"id": "photo_archive", "icon": "archive", "title": "Archival",
     "desc": "Lossless, for enlarging. Large files",
     "settings": {"mode": "photo", "photo": {
         "resolution": 1200, "format": "tiff", "jpeg_quality": 92, "autocrop": True, "duplex": True}}},
]


def preset_spec(p):
    """Short summary line, e.g. '300 dpi · Color'. The group heading on the page
    already says PDF / image files, two-sided and auto-crop."""
    mode = p["settings"]["mode"]
    o = p["settings"][mode]
    if mode == "document":
        parts = [f"{o['resolution']} dpi", {"color": "Color", "gray": "Gray", "bw": "B&W"}[o["color"]]]
        if o["ocr"]:
            parts.append("Searchable")
    else:
        parts = [f"{o['resolution']} dpi", {"jpg": "JPEG", "png": "PNG", "tiff": "TIFF"}[o["format"]]]
        if o["duplex"]:
            parts.append("Backs")
    return " · ".join(parts)


def active_preset(s):
    """id of the preset the current settings match exactly, else None."""
    for p in PRESETS:
        mode = p["settings"]["mode"]
        if s["mode"] == mode and all(s[mode][k] == v for k, v in p["settings"][mode].items()):
            return p["id"]
    return None


def preset(pid):
    return next((p for p in PRESETS if p["id"] == pid), None)


settings = Settings()
