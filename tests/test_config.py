import pytest

from app import config
from app.config import SECRET_MASK, settings


def test_defaults_validate():
    s = settings.get()
    assert s["mode"] == "document"
    assert s["scanner"]["detection"] == "auto"


def test_update_coerces_types():
    s = settings.update({"document": {"resolution": "200", "duplex": "false", "blank_sensitivity": "1.5"}})
    assert s["document"]["resolution"] == 200
    assert s["document"]["duplex"] is False
    assert s["document"]["blank_sensitivity"] == 1.5


def test_update_ignores_unknown_keys():
    s = settings.update({"bogus": 1, "document": {"nope": 2}})
    assert "bogus" not in s and "nope" not in s["document"]


def test_update_clamps_ranges():
    s = settings.update({"scanner": {"poll_seconds": 0}, "document": {"resolution": 99999, "jpeg_quality": 5}})
    assert s["scanner"]["poll_seconds"] == 1
    assert s["document"]["resolution"] == 1200
    assert s["document"]["jpeg_quality"] == 30


@pytest.mark.parametrize("patch", [
    {"mode": "fax"},
    {"document": {"resolution": None}},
    {"document": {"resolution": "abc"}},
    {"document": {"ocr_lang": "eng; rm -rf"}},
    {"document": {"filename": "{oops}"}},
    {"document": {"filename": "{date"}},
    {"photo": {"filename": "   "}},
    {"scanner": {"extra_args": "--batch=/tmp/x"}},
    {"scanner": {"extra_args": "-o /etc/passwd"}},
    {"scanner": {"extra_args": "-d other"}},
    {"scanner": {"extra_args": "'unterminated"}},
    {"scanner": {"ip": "192.168.1.5\n[Other]"}},
    {"scanner": {"ip": "1.2.3.4 --flag"}},
])
def test_update_rejects_bad_values(patch):
    before = settings.get()
    with pytest.raises(ValueError):
        settings.update(patch)
    assert settings.get() == before  # nothing half-applied


@pytest.mark.parametrize("ip", ["", "192.168.1.50", "scanner.lan", "fe80::1"])
def test_update_accepts_ip_and_hostnames(ip):
    assert settings.update({"scanner": {"ip": ip}})["scanner"]["ip"] == ip


def test_update_rejects_non_object():
    with pytest.raises(ValueError):
        settings.update(["x"])


def test_folder_paths_normalised():
    s = settings.update({"drive": {"pdf_folder": " /Scans// Tax  /", "photo_folder": "///"}})
    assert s["drive"]["pdf_folder"] == "Scans/Tax"
    assert s["drive"]["photo_folder"] == "Scans"


def test_secret_masked_and_mask_not_saved():
    settings.update({"drive": {"client_secret": "s3cret"}})
    assert settings.public()["drive"]["client_secret"] == SECRET_MASK
    settings.update({"drive": {"client_secret": SECRET_MASK}})
    assert settings.get()["drive"]["client_secret"] == "s3cret"


def test_settings_persisted_to_disk():
    settings.update({"mode": "photo"})
    assert '"mode": "photo"' in config.SETTINGS_FILE.read_text()
