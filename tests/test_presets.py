import copy

import pytest
from starlette.testclient import TestClient

from app import main
from app.config import DEFAULTS, PRESETS, active_preset, preset_spec, settings


@pytest.mark.parametrize("p", PRESETS, ids=lambda p: p["id"])
def test_every_preset_is_valid_and_recognised(p):
    s = settings.update(copy.deepcopy(p["settings"]))
    assert active_preset(s) == p["id"]  # nothing clamped or rewritten by validation


def test_defaults_match_the_recommended_presets():
    s = copy.deepcopy(DEFAULTS)
    assert active_preset(s) == "doc_standard"
    s["mode"] = "photo"
    assert active_preset(s) == "photo_standard"


def test_one_recommended_per_mode():
    for mode in ("document", "photo"):
        assert sum(p.get("recommended", False) for p in PRESETS if p["settings"]["mode"] == mode) == 1


def test_manual_change_becomes_custom():
    settings.update({"document": {"resolution": 400}})
    assert active_preset(settings.get()) is None


def test_spec_lines():
    by_id = {p["id"]: preset_spec(p) for p in PRESETS}
    assert by_id["doc_standard"] == "300 dpi · Color"
    assert by_id["doc_high"] == "600 dpi · Color · Searchable"
    assert by_id["photo_quick"] == "300 dpi · JPEG"
    assert by_id["photo_archive"] == "1200 dpi · TIFF · Backs"


def test_presets_api():
    c = TestClient(main.app)
    lst = c.get("/api/presets").json()
    assert [p["id"] for p in lst] == [p["id"] for p in PRESETS]
    assert {"icon", "title", "desc", "spec", "mode", "recommended"} <= lst[0].keys()
    r = c.post("/api/presets/photo_archive/apply")
    assert r.status_code == 200 and r.json()["mode"] == "photo" and r.json()["photo"]["format"] == "tiff"
    assert c.get("/api/status").json()["preset"] == "photo_archive"
    assert c.post("/api/presets/nope/apply").status_code == 404
    assert c.post("/api/presets/doc_quick/apply", headers={"Origin": "https://evil.example"}).status_code == 403
