"""The app creates its singletons (settings, store, drive, ...) at import time
under DATA_DIR, so point DATA_DIR at a throwaway folder before anything imports."""
import os
import sys
import tempfile
from pathlib import Path

_DATA = tempfile.mkdtemp(prefix="scanhub-test-")
os.environ["DATA_DIR"] = _DATA
os.environ["SIMULATE_SCANNER"] = "1"
for k in ("SCANNER_IP", "SCANNER_DEVICE", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "WEB_PASSWORD"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import copy  # noqa: E402
import shutil  # noqa: E402

import pytest  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from app import config  # noqa: E402
from app.store import store  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_state():
    """Reset settings and the item table between tests."""
    config.settings._data = copy.deepcopy(config.DEFAULTS)
    config.settings._save()
    with store._lock:
        store.db.execute("DELETE FROM items")
        store.db.commit()
    for d in (config.WORK_DIR, config.WATCH_DIR):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
    yield


@pytest.fixture
def data_dir():
    return Path(_DATA)


def text_page(dpi=100, lines=True):
    w, h = int(8.5 * dpi), int(11 * dpi)
    img = Image.new("RGB", (w, h), (250, 250, 248))
    if lines:
        d = ImageDraw.Draw(img)
        y = dpi
        while y < h - dpi:
            d.rectangle([dpi, y, w - dpi, y + dpi // 12], fill=(20, 20, 20))
            y += dpi // 4
    return img


def photo_on_black(dpi=100):
    img = Image.new("RGB", (int(8.5 * dpi), int(6.6 * dpi)), (8, 8, 8))
    d = ImageDraw.Draw(img)
    x0, y0 = int(0.4 * dpi), int(0.3 * dpi)
    d.rectangle([x0, y0, x0 + 6 * dpi, y0 + 4 * dpi], fill=(180, 120, 60))
    return img, (x0, y0, x0 + 6 * dpi, y0 + 4 * dpi)
