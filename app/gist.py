"""Describe a scanned document from its OCR text: a title, a one- or two-line
gist, and a few facts (type, sender, date) that go into Drive's metadata.

Two ways to do it:
  * "local"  - simple rules on this machine; nothing leaves the network.
  * "claude" - sends the recognised text (not the images) to Claude, which
               reads the whole thing and writes a much better title and gist.
Either way, the result is a dict with the keys in FIELDS ("" when unknown).
"""
import json
import logging
import re

from .processing import DOC_WORDS, _NOISE, _clean_line, doc_title

log = logging.getLogger("gist")

FIELDS = ("title", "summary", "doc_type", "sender", "doc_date", "keywords")
MAX_CHARS = 60_000  # ~15k tokens: plenty for a gist, keeps a 100-page stack cheap
DEFAULT_MODEL = "claude-opus-5-5"

SYSTEM = """You file scanned paper documents for a household. You get the OCR \
text of one scan (OCR may contain errors; pages are separated by form feeds). \
Describe it so the person can find it later in Google Drive.

- title: what the document is, the way a person would label a folder tab. \
Lead with who it is from, then what it is, then the period or subject if it \
tells documents apart. 3-8 words, no date in YYYY-MM-DD form, no file extension. \
E.g. "Acme Power Electric Bill October 2026", "Dr. Patel Visit Summary Knee MRI".
- summary: the gist in one or two plain sentences: what it is and what matters \
in it (amounts due and due dates, decisions, deadlines, account or policy \
names). No account numbers, SSNs or other ID numbers in full.
- doc_type: one or two words, e.g. Invoice, Bill, Statement, Letter, Receipt, \
Tax form, Medical record, Contract, Warranty, Notice, Manual.
- sender: the organisation or person it is from, or "".
- doc_date: the date the document is dated, as YYYY-MM-DD, or "" if none.
- keywords: up to 6 short search terms not already in the title.

If the text is unreadable or empty, return empty strings and an empty list."""

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "doc_type": {"type": "string"},
        "sender": {"type": "string"},
        "doc_date": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": list(FIELDS),
    "additionalProperties": False,
}


def empty():
    return {k: [] if k == "keywords" else "" for k in FIELDS}


def _tidy(d):
    """Keep the fields we know, with sane types and lengths."""
    out = empty()
    if not isinstance(d, dict):
        return out
    for k in FIELDS:
        v = d.get(k)
        if k == "keywords":
            if isinstance(v, list):
                out[k] = [str(w).strip()[:40] for w in v if str(w).strip()][:6]
        elif isinstance(v, str):
            out[k] = " ".join(v.split())
    out["title"] = out["title"][:80].strip(" .")
    out["summary"] = out["summary"][:600]
    out["doc_type"] = out["doc_type"][:40]
    out["sender"] = out["sender"][:80]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", out["doc_date"]):
        out["doc_date"] = ""
    return out


# ------------------------------------------------------------------ local rules
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_DATE_PATTERNS = [
    (re.compile(r"\b(20\d{2}|19\d{2})-(\d{1,2})-(\d{1,2})\b"), ("y", "m", "d")),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2}|19\d{2})\b"), ("m", "d", "y")),
    (re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? (\d{1,2}),? (20\d{2}|19\d{2})\b", re.I),
     ("mon", "d", "y")),
    (re.compile(r"\b(\d{1,2}) (jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? (20\d{2}|19\d{2})\b", re.I),
     ("d", "mon", "y")),
]


def find_date(text):
    """First plausible calendar date in the text, as YYYY-MM-DD, else ""."""
    best = None
    for rx, order in _DATE_PATTERNS:
        for m in rx.finditer(text):
            parts = dict(zip(order, m.groups()))
            try:
                y = int(parts["y"])
                mo = _MONTHS[parts["mon"][:3].lower()] if "mon" in parts else int(parts["m"])
                d = int(parts["d"])
            except (KeyError, ValueError):
                continue
            if 1 <= mo <= 12 and 1 <= d <= 31:
                if best is None or m.start() < best[0]:
                    best = (m.start(), f"{y:04d}-{mo:02d}-{d:02d}")
                break
    return best[1] if best else ""


def local_gist(text):
    """A plain, rule-based description; good enough to search by."""
    out = empty()
    text = text or ""
    out["title"] = doc_title(text)
    first = text.split("\f")[0]
    for w in re.findall(r"[A-Za-z]+", first[:3000]):
        if w.lower() in DOC_WORDS:
            out["doc_type"] = w.capitalize()
            break
    out["doc_date"] = find_date(first)
    # The gist: the first few full sentences of running text on page one.
    sentences = []
    flat = " ".join(_clean_line(l) for l in first.splitlines()
                    if len(l.split()) >= 6 and not _NOISE.search(l.strip()))
    for s in re.split(r"(?<=[.!?])\s+", flat):
        if len(s.split()) >= 6 and sum(c.isalpha() for c in s) > 0.6 * len(s.replace(" ", "")):
            sentences.append(s)
        if len(" ".join(sentences)) > 250 or len(sentences) >= 2:
            break
    out["summary"] = " ".join(sentences)
    return _tidy(out)


# ------------------------------------------------------------------ Claude
def claude_gist(text, api_key, model=DEFAULT_MODEL):
    """Ask Claude for the description. Returns None if it can't be had (no
    key, network down, refusal), so the caller can fall back to local rules."""
    if not (api_key and text.strip()):
        return None
    try:
        import anthropic
    except ImportError:
        log.warning("The 'anthropic' package is not installed; using local rules")
        return None
    body = text
    if len(body) > MAX_CHARS:
        log.info("Sending the first %d of %d characters of text to Claude", MAX_CHARS, len(body))
        body = body[:MAX_CHARS] + "\n\n[... rest of the document not included ...]"
    client = anthropic.Anthropic(api_key=api_key, timeout=120.0, max_retries=2)
    try:
        r = client.beta.messages.create(
            model=model or DEFAULT_MODEL,
            max_tokens=4000,
            system=SYSTEM,
            messages=[{"role": "user", "content": f"<ocr_text>\n{body}\n</ocr_text>"}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError:
        log.warning("Claude rejected the API key; using local rules")
        return None
    except anthropic.APIStatusError as e:
        log.warning("Claude request failed (%s): %s; using local rules", e.status_code, e.message)
        return None
    except anthropic.APIConnectionError as e:
        log.warning("Couldn't reach Claude (%s); using local rules", e)
        return None
    if r.stop_reason in ("refusal", "max_tokens"):
        log.warning("Claude returned no description (%s); using local rules", r.stop_reason)
        return None
    raw = next((b.text for b in r.content if b.type == "text"), "")
    try:
        return _tidy(json.loads(raw))
    except ValueError:
        log.warning("Claude's answer wasn't valid JSON; using local rules")
        return None


def describe(text, s):
    """Describe a document per the settings' document.describe mode."""
    mode = s["document"]["describe"]
    if mode == "off":
        return None
    result = None
    if mode == "claude":
        c = s["claude"]
        result = claude_gist(text, c["api_key"], c["model"])
        if result:
            log.info("Claude described the scan: %s", result["title"] or "(no title)")
    if not result:
        result = local_gist(text)
    if not result["title"]:
        result["title"] = doc_title(text)  # Claude gave none; try the heading
    return result
