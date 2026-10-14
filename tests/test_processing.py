from pathlib import Path

import pytest
from PIL import Image

from app import processing
from app.config import DEFAULTS
from conftest import photo_on_black, text_page


def test_ink_percent_blank_vs_text():
    blank = Image.new("RGB", (850, 1100), (250, 250, 248))
    assert processing.is_blank(blank, 0.4)
    assert not processing.is_blank(text_page(), 0.4)


def test_autocrop_finds_photo():
    img, (x0, y0, x1, y1) = photo_on_black()
    out = processing.autocrop(img)
    w, h = out.size
    assert abs(w - (x1 - x0)) <= 6 and abs(h - (y1 - y0)) <= 6


def test_autocrop_leaves_plain_scan_alone():
    img = Image.new("RGB", (800, 600), (8, 8, 8))
    assert processing.autocrop(img).size == img.size


def test_render_name_sanitises_and_fills_fields():
    import time
    t = time.strptime("2026-03-04 05:06:07", "%Y-%m-%d %H:%M:%S")
    assert processing.render_name("Scan_{date}_{time}_{n}", 7, "document", t) == "Scan_2026-03-04_050607_007"
    assert processing.render_name("a/b:c*{mode}", 1, "photo", t) == "a_b_c_photo"


def test_unique_path(tmp_path):
    p1 = processing.unique_path(tmp_path, "x", ".pdf")
    p1.touch()
    assert processing.unique_path(tmp_path, "x", ".pdf").name == "x-2.pdf"


@pytest.mark.parametrize("names,expected", [
    (["p0001", "p0002", "p0003", "p0004"], [("p0001", "p0002"), ("p0003", "p0004")]),
    # p0002 was unreadable / dropped: photo 2 must keep its own back, not shift.
    (["p0001", "p0003", "p0004"], [("p0001", None), ("p0003", "p0004")]),
])
def test_pair_sides_by_page_number(names, expected):
    pages = [Path(n + ".pnm") for n in names]
    got = [(f and f.stem, b and b.stem) for f, b in processing.pair_sides(pages, True)]
    assert got == expected


def _write_pages(folder, images):
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for i, im in enumerate(images, start=1):
        p = folder / f"p{i:04d}.pnm"
        im.save(p)
        out.append(p)
    return out


@pytest.mark.parametrize("color", ["color", "gray", "bw"])
def test_make_pdf_skips_blank_pages(tmp_path, color):
    blank = Image.new("RGB", (850, 1100), (250, 250, 248))
    pages = _write_pages(tmp_path / "job", [text_page(), blank, text_page()])
    d = dict(DEFAULTS["document"], color=color, ocr=False)
    out = processing.make_pdf(pages, d, 100)
    assert len(out) == 1 and out[0]["pages"] == 2 and out[0]["kind"] == "pdf"
    data = out[0]["path"].read_bytes()
    assert data.startswith(b"%PDF")
    assert data.count(b"/Subtype /Image") == 2  # one image per kept page


def test_make_pdf_all_blank_produces_nothing(tmp_path):
    blank = Image.new("RGB", (850, 1100), (250, 250, 248))
    pages = _write_pages(tmp_path / "job", [blank])
    assert processing.make_pdf(pages, dict(DEFAULTS["document"]), 100) == []


def test_make_pdf_skips_unreadable_page(tmp_path):
    pages = _write_pages(tmp_path / "job", [text_page()])
    bad = tmp_path / "job" / "p0002.pnm"
    bad.write_bytes(b"P6\n100 100\n255\n" + b"\0" * 10)  # truncated
    out = processing.make_pdf(pages + [bad], dict(DEFAULTS["document"]), 100)
    assert out[0]["pages"] == 1


@pytest.mark.parametrize("fmt,ext", [("jpg", ".jpg"), ("png", ".png"), ("tiff", ".tif")])
def test_save_photos_formats_and_blank_backs(tmp_path, fmt, ext):
    front, _ = photo_on_black()
    blank_back = Image.new("RGB", front.size, (8, 8, 8))
    blank_back.paste((236, 232, 220), (40, 30, 640, 430))
    written_back = blank_back.copy()
    from PIL import ImageDraw
    ImageDraw.Draw(written_back).rectangle([100, 100, 500, 140], fill=(30, 30, 120))
    pages = _write_pages(tmp_path / "job", [front, blank_back, front, written_back])
    ph = dict(DEFAULTS["photo"], format=fmt, duplex=True)
    out = processing.save_photos(pages, ph, 100)
    names = [o["path"].name for o in out]
    assert len(out) == 3, names           # two fronts + one written-on back
    assert all(n.endswith(ext) for n in names)
    assert sum(n.endswith("_b" + ext) for n in names) == 1
