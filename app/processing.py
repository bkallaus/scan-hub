"""Turn raw page images from the scanner into PDFs or photo files."""
import logging
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import img2pdf
import numpy as np
from PIL import Image

from .config import SCANS_DIR, WORK_DIR

log = logging.getLogger("processing")
Image.MAX_IMAGE_PIXELS = None  # long-paper scans are legitimately huge
BACK_BLANK_PERCENT = 0.15     # photo backs with less "ink" than this are dropped


# ------------------------------------------------------------------ helpers
def _small(img, target=800):
    w, h = img.size
    scale = max(1, max(w, h) // target)
    return img.resize((max(1, w // scale), max(1, h // scale))), scale


def ink_percent(img):
    """Percentage of pixels that differ clearly from the page's background tone."""
    g, _ = _small(img.convert("L"), 600)
    a = np.asarray(g, dtype=np.int16)
    h, w = a.shape
    my, mx = int(h * 0.06), int(w * 0.06)  # ignore feeder edges / shadows
    a = a[my:h - my, mx:w - mx]
    if a.size == 0:
        return 0.0
    bg = np.median(a)
    return float((np.abs(a - bg) > 60).mean() * 100)


def is_blank(img, sensitivity):
    return ink_percent(img) < sensitivity


def autocrop(img):
    """Crop a photo out of the scanner background (FF-680W uses a black backing)."""
    rgb = img.convert("RGB")
    small, scale = _small(rgb, 1000)
    a = np.asarray(small, dtype=np.int16)
    h, w, _ = a.shape
    bw = max(2, int(min(h, w) * 0.02))
    border = np.concatenate([a[:bw].reshape(-1, 3), a[-bw:].reshape(-1, 3),
                             a[:, :bw].reshape(-1, 3), a[:, -bw:].reshape(-1, 3)])
    bg = np.median(border, axis=0)
    mask = np.abs(a - bg).sum(axis=2) > 60
    rows = np.where(mask.mean(axis=1) > 0.03)[0]
    cols = np.where(mask.mean(axis=0) > 0.03)[0]
    if len(rows) < 2 or len(cols) < 2:
        return img
    y0, y1, x0, x1 = rows[0], rows[-1] + 1, cols[0], cols[-1] + 1
    if (y1 - y0) * (x1 - x0) < 0.05 * h * w:
        return img  # implausibly small; leave the scan alone
    W, H = img.size
    inset = scale  # trim the soft edge pixel row
    box = (min(W, x0 * scale + inset), min(H, y0 * scale + inset),
           max(0, min(W, x1 * scale) - inset), max(0, min(H, y1 * scale) - inset))
    if box[2] <= box[0] or box[3] <= box[1]:
        return img
    return img.crop(box)


NAME_FIELDS = ("date", "time", "n", "mode", "title")


def render_name(template, n=1, mode="", now=None, title=""):
    now = now or time.localtime()
    values = {
        "date": time.strftime("%Y-%m-%d", now),
        "time": time.strftime("%H%M%S", now),
        "n": f"{n:03d}",
        "mode": mode,
        "title": title or "Scan",
    }
    try:
        name = template.format_map(values)
    except (KeyError, ValueError, IndexError, AttributeError):
        log.warning("Bad file name template %r; using the default", template)
        name = "{mode}_{date}_{time}_{n}".format_map(values)
    keep = "".join(c if c.isalnum() or c in " ._-()" else "_" for c in name).strip()
    return keep or f"scan_{int(time.time())}"


# Words that say what kind of document a line is naming, e.g. "INVOICE".
DOC_WORDS = {
    "agreement", "application", "bill", "certificate", "contract", "estimate",
    "form", "invoice", "letter", "lease", "notice", "order", "policy", "quote",
    "receipt", "record", "report", "statement", "summary", "warranty",
}
_NOISE = re.compile(r"@|https?:|www\.|\.com\b|\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}"
                    r"|^(dear|page|to|from|re|date|tel|fax|phone)\b", re.I)


def _clean_line(line):
    """Keep only the plain words of an OCR line; drop stray OCR junk symbols."""
    line = re.sub(r"['\u2019]", "", line.replace("&", " and "))
    line = re.sub(r"[^\w.()-]+", " ", line).replace("_", " ")
    words = [w.strip(".-") for w in line.split()]
    return " ".join(w for w in words if w and (len(w) > 1 or w.isalnum()))


def doc_title(text, max_words=7, max_len=60):
    """Guess a short title from a document's OCR text: its heading, or the
    sender plus document type ("Acme Power Invoice"). "" if nothing fits."""
    first_page = (text or "").split("\f")[0]
    lines = []
    for raw in first_page.splitlines():
        line = _clean_line(raw)
        letters = sum(c.isalpha() for c in line)
        if letters < 3 or letters < 0.6 * len(line.replace(" ", "")) or _NOISE.search(raw.strip()):
            continue
        lines.append(line)
        if len(lines) >= 25:
            break
    if not lines:
        return ""

    def score(i, line):
        words = line.split()
        sc = -i  # headings come first
        if any(w.lower().strip("()") in DOC_WORDS for w in words):
            sc += 12
        if line.isupper() or all(w[0].isupper() for w in words if w[0].isalpha()):
            sc += 4
        if len(words) > max_words:
            sc -= 3 * (len(words) - max_words)
        return sc

    best = max(range(len(lines)), key=lambda i: score(i, lines[i]))
    title = lines[best]
    # A bare "INVOICE" says little on its own; put the sender (the line at the top) in front.
    if best > 0 and len(title.split()) <= 2 and title.split()[0].lower() in DOC_WORDS:
        title = f"{lines[0]} {title}"
    # Shouted words read better as "Invoice"; short ones are likely acronyms (IRS, LLC).
    words = [w.capitalize() if w.isupper() and w.isalpha() and len(w) > 3 else w for w in title.split()[:max_words]]
    title = " ".join(words)
    if len(title) > max_len:
        title = title[:max_len].rsplit(" ", 1)[0]
    return title


def page_text(image_path, lang):
    """OCR one page image with tesseract; "" if it isn't available or fails."""
    if not shutil.which("tesseract"):
        log.warning("Naming from text needs tesseract, which is not installed in this image")
        return ""
    try:
        p = subprocess.run(["tesseract", str(image_path), "stdout", "-l", lang or "eng"],
                           capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        log.warning("Reading the first page timed out")
        return ""
    if p.returncode != 0:
        tail = p.stderr.strip().splitlines()
        log.warning("Reading the first page failed: %s", tail[-1] if tail else p.returncode)
        return ""
    return p.stdout


def unique_path(folder, stem, ext):
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / f"{stem}{ext}"
    i = 2
    while p.exists():
        p = folder / f"{stem}-{i}{ext}"
        i += 1
    return p


def output_dir():
    return SCANS_DIR / time.strftime("%Y-%m")


# ------------------------------------------------------------------ documents
def open_page(path):
    """Load a page image, or None if it is unreadable (e.g. cut off by a jam)."""
    try:
        with Image.open(path) as im:
            im.load()
            return im.copy()
    except Exception as e:
        log.warning("Skipping unreadable page %s: %s", Path(path).name, e)
        return None


def make_pdf(pages, d, dpi):
    # Convert one page at a time to a compressed file so a 100-page batch
    # doesn't need gigabytes of RAM.
    tmp = Path(tempfile.mkdtemp(prefix="tmp_pdf_", dir=WORK_DIR))
    try:
        parts = []
        for p in pages:
            im = open_page(p)
            if im is None:
                continue
            if d["skip_blank"] and is_blank(im, d["blank_sensitivity"]):
                log.info("Skipping blank page %s", p.name)
                continue
            i = len(parts)
            if d["color"] == "bw":
                f = tmp / f"{i:04d}.tif"
                bw = im if im.mode == "1" else im.convert("L").point(lambda v: 255 if v > 160 else 0).convert("1")
                bw.save(f, compression="group4", dpi=(dpi, dpi))
            else:
                f = tmp / f"{i:04d}.jpg"
                conv = im.convert("L" if d["color"] == "gray" else "RGB")
                conv.save(f, quality=d["jpeg_quality"], dpi=(dpi, dpi), optimize=True)
            del im
            parts.append(str(f))
        if not parts:
            log.warning("All %d page(s) were blank or unreadable; no PDF created", len(pages))
            return []
        raw_pdf = tmp / "out.pdf"
        raw_pdf.write_bytes(img2pdf.convert(parts))
        final_pdf = raw_pdf
        text = ""
        if d["ocr"]:
            sidecar = tmp / "ocr.txt"
            final_pdf = run_ocr(raw_pdf, tmp / "ocr.pdf", d["ocr_lang"], sidecar) or raw_pdf
            if sidecar.exists():
                text = sidecar.read_text(errors="replace")
        title = ""
        if "{title}" in d["filename"]:
            # Without OCR, read just the first page; that's where the heading is.
            title = doc_title(text or page_text(parts[0], d["ocr_lang"]))
            log.info("Named from content: %s", title or "(no title found)")
        dest = unique_path(output_dir(), render_name(d["filename"], 1, "document", title=title), ".pdf")
        shutil.move(str(final_pdf), dest)
        return [{"path": dest, "kind": "pdf", "pages": len(parts)}]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_ocr(src, dst, lang, sidecar=None):
    if not shutil.which("ocrmypdf"):
        log.warning("OCR enabled but ocrmypdf is not installed in this image")
        return None
    cmd = ["ocrmypdf", "--output-type", "pdf", "--rotate-pages", "-l", lang or "eng"]
    if sidecar:
        cmd += ["--sidecar", str(sidecar)]  # the recognised text, pages split by \f
    cmd += [str(src), str(dst)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if p.returncode == 0:
            return dst
        tail = p.stderr.strip().splitlines()
        log.warning("OCR failed (%s): %s", p.returncode, tail[-1] if tail else "no error output")
    except subprocess.TimeoutExpired:
        log.warning("OCR timed out; saving PDF without text layer")
    return None


# ------------------------------------------------------------------ photos
def _page_number(path, fallback):
    m = re.search(r"(\d+)", Path(path).stem)
    return int(m.group(1)) if m else fallback


def pair_sides(pages, duplex):
    """Group page images into (front, back) per photo using the scanner's page
    numbers (p0001 front, p0002 back, ...), so a dropped or unreadable side
    doesn't shift every later photo onto the wrong back."""
    if not duplex:
        return [(p, None) for p in pages]
    photos = {}
    for i, p in enumerate(pages, start=1):
        n = _page_number(p, i)
        photo, side = (n - 1) // 2, (n - 1) % 2
        photos.setdefault(photo, [None, None])[side] = p
    return [tuple(v) for _, v in sorted(photos.items())]


def save_photos(pages, ph, dpi):
    ext = {"jpg": ".jpg", "png": ".png", "tiff": ".tif"}[ph["format"]]
    outputs = []
    folder = output_dir()
    batch_time = time.localtime()
    for idx, (front, back) in enumerate(pair_sides(pages, ph["duplex"]), start=1):
        stem = render_name(ph["filename"], idx, "photo", batch_time)
        sides = [(front, "")] if front else []
        if back:
            sides.append((back, "_b"))
        for page, suffix in sides:
            img = open_page(page)
            if img is None:
                continue
            img = img.convert("RGB")
            if ph["autocrop"]:
                img = autocrop(img)
            if suffix and is_blank(img, BACK_BLANK_PERCENT):
                continue  # plain back side
            dest = unique_path(folder, stem + suffix, ext)
            kw = {"dpi": (dpi, dpi)}
            if ph["format"] == "jpg":
                kw.update(quality=ph["jpeg_quality"], optimize=True, subsampling=0 if ph["jpeg_quality"] >= 90 else 2)
            elif ph["format"] == "tiff":
                kw["compression"] = "tiff_lzw"
            img.save(dest, **kw)
            outputs.append({"path": dest, "kind": "photo", "pages": 1})
    return outputs


def process(pages, mode, s, dpi=None):
    if mode == "document":
        return make_pdf(pages, s["document"], dpi or s["document"]["resolution"])
    return save_photos(pages, s["photo"], dpi or s["photo"]["resolution"])
