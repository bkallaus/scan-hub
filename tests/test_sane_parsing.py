from app.config import DEFAULTS
from app.scanner import build_args, find_sensor, parse_devices, parse_options
import copy
from pathlib import Path

SCANIMAGE_A = """
All options specific to device `epsonscan2:networkscanner:esci2:network:192.168.1.50':
  Scan Mode:
    --mode Color|Grayscale|Monochrome [Color]
        Selects the scan mode.
    --resolution 50..1200dpi (in steps of 1) [300]
        Sets the resolution of the scanned image.
    --source ADF Front|ADF Duplex [ADF Front]
        Selects the scan source.
    --scan-area Auto Detect|Auto Detect (long paper)|Letter|A4 [Letter]
        Select an area to scan.
    --adf-loaded[=(yes|no)] [no] [read-only]
        Paper is loaded in the feeder.
    --paper-detect[=(yes|no)] [yes]
        A writable setting, not a sensor.
    --scan[=(yes|no)] [no] [hardware]
        Scan button.
    --gamma 0..2 [inactive]
"""


def test_parse_options_values_and_flags():
    o = parse_options(SCANIMAGE_A)
    assert o["mode"].choices() == ["Color", "Grayscale", "Monochrome"]
    assert o["mode"].current == "Color"
    assert o["resolution"].range() == (50.0, 1200.0)
    assert o["adf-loaded"].is_bool and o["adf-loaded"].readonly
    assert o["adf-loaded"].current == "no"
    assert not o["paper-detect"].readonly
    assert o["gamma"].inactive


def test_find_sensor_ignores_writable_and_inactive():
    names = {s.name for s in find_sensor(parse_options(SCANIMAGE_A))}
    assert names == {"adf-loaded", "scan"}


def test_parse_devices():
    txt = ("device `epsonscan2:FF-680W:001:network:192.168.1.50' is a EPSON FF-680W\n"
           "device `test:0' is a Noname frontend-tester virtual device\n")
    assert parse_devices(txt) == [
        {"name": "epsonscan2:FF-680W:001:network:192.168.1.50", "desc": "EPSON FF-680W"},
        {"name": "test:0", "desc": "Noname frontend-tester virtual device"},
    ]


def _s(**over):
    s = copy.deepcopy(DEFAULTS)
    for k, v in over.items():
        s[k].update(v)
    return s


def test_build_args_document_duplex_gray():
    args = build_args(parse_options(SCANIMAGE_A), _s(document={"color": "gray", "resolution": 200, "duplex": True}), "document")
    assert args[args.index("--mode") + 1] == "Grayscale"
    assert args[args.index("--resolution") + 1] == "200"
    assert args[args.index("--source") + 1] == "ADF Duplex"
    assert args[args.index("--scan-area") + 1] == "Auto Detect"  # not the long-paper one


def test_build_args_photo_simplex_and_bw_mapping():
    o = parse_options(SCANIMAGE_A)
    args = build_args(o, _s(photo={"duplex": False, "resolution": 9999}), "photo")
    assert args[args.index("--mode") + 1] == "Color"
    assert args[args.index("--source") + 1] == "ADF Front"
    assert args[args.index("--resolution") + 1] == "1200"  # clamped to driver range
    bw = build_args(o, _s(document={"color": "bw"}), "document")
    assert bw[bw.index("--mode") + 1] == "Monochrome"


def test_build_args_list_resolution_and_extra_args():
    o = parse_options("    --resolution 75|150|300|600dpi [300]\n")
    args = build_args(o, _s(document={"resolution": 400}, scanner={"extra_args": "-y 356"}), "document")
    assert args == ["--resolution", "300", "-y", "356"]


def test_configure_network_writes_epson_conf(tmp_path, monkeypatch):
    from pathlib import Path

    from app.scanner import SaneBackend
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    SaneBackend().configure_network("192.168.1.50")
    # Byte-for-byte what `epsonscan2 --set-ip 192.168.1.50` writes.
    assert (tmp_path / ".epsonscan2/Network/epsonscan2.conf").read_text() == "[Network]\n192.168.1.50\n"
    SaneBackend().configure_network("")  # blank IP leaves the file alone
    assert "192.168.1.50" in (tmp_path / ".epsonscan2/Network/epsonscan2.conf").read_text()


def test_options_error_reports_scanimage_line(monkeypatch):
    import pytest

    from app.scanner import SaneBackend
    err = ("Output format is not set, using pnm as a default.\n"
           "scanimage: open of device epsonscan2:networkscanner:esci2:network:192.0.2.5 failed: Error during device I/O\n")
    monkeypatch.setattr(SaneBackend, "_run", staticmethod(lambda cmd, timeout: (1, "", err)))
    with pytest.raises(RuntimeError) as e:
        SaneBackend().options("dev")
    assert str(e.value).startswith("scanimage: open of device") and "\n" not in str(e.value)


# Captured from a real FF-680W (driver 6.7.80) with `scanimage -A`.
REAL_FF680W = (Path(__file__).parent / "ff680w_options.txt").read_text()


def _arg(args, name):
    for i, a in enumerate(args):
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
        if a == name:
            return args[i + 1]
    return None


def test_real_ff680w_document_duplex():
    o = parse_options(REAL_FF680W)
    args = build_args(o, _s(document={"duplex": True, "color": "gray", "resolution": 300}), "document")
    assert _arg(args, "--duplex") == "yes"
    assert "--duplex=yes" in args  # one token: scanimage rejects "--duplex yes"
    assert _arg(args, "--source") == "ADF"
    assert _arg(args, "--mode") == "Grayscale"
    assert _arg(args, "--resolution") == "300"
    assert _arg(args, "--scan-area") == "Auto Detect"   # not "Manual" (the full 393 mm feed)


def test_real_ff680w_simplex_and_photo():
    o = parse_options(REAL_FF680W)
    simplex = build_args(o, _s(document={"duplex": False}), "document")
    assert _arg(simplex, "--duplex") == "no"
    photo = build_args(o, _s(photo={"duplex": True}), "photo")
    assert _arg(photo, "--duplex") == "yes" and _arg(photo, "--mode") == "Color"
    bw = build_args(o, _s(document={"color": "bw"}), "document")
    assert _arg(bw, "--mode") == "Monochrome"


def test_real_ff680w_has_no_sensor():
    # Nothing read-only to poll, so "auto" detection falls back to probing.
    assert find_sensor(parse_options(REAL_FF680W)) == []
