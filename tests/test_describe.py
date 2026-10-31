import json
import sys
import time
import types
from pathlib import Path

import pytest

from app import drive as drive_mod
from app import describe, processing
from app.config import DEFAULTS, SECRET_MASK, settings
from app.store import store
from conftest import text_page

BILL = """ACME POWER AND LIGHT
PO Box 100, Springfield
Statement Date: October 3, 2026
ELECTRIC BILL
Your electricity use this month was higher than last month because of the hot weather.
The amount due of $84.12 must be paid by October 24, 2026 to avoid a late fee.
\fPage 2 details
"""


def test_find_date_formats():
    assert describe.find_date("Dated 10/03/2026, paid 2025-01-02") == "2026-10-03"  # the first one
    assert describe.find_date("on 10/03/2026") == "2026-10-03"
    assert describe.find_date("Statement Date: October 3, 2026") == "2026-10-03"
    assert describe.find_date("3 Oct 2026") == "2026-10-03"
    assert describe.find_date("13/45/2026 nothing") == ""


def test_local_summary():
    g = describe.local_summary(BILL)
    assert g["title"]
    assert g["doc_type"] == "Statement"
    assert g["doc_date"] == "2026-10-03"
    assert g["summary"].startswith("Your electricity use")
    assert set(g) == set(describe.FIELDS)


def test_local_summary_empty_text():
    assert describe.local_summary("") == describe.empty()


class FakeClaude:
    """Stands in for the anthropic module; records the request."""

    def __init__(self, answer=None, stop="end_turn", raise_=None):
        self.calls = []
        fake = self

        class APIStatusError(Exception):
            status_code, message = 500, "boom"

        class AuthenticationError(APIStatusError):
            pass

        class APIConnectionError(Exception):
            pass

        class Messages:
            def create(self, **kw):
                fake.calls.append(kw)
                if raise_:
                    raise getattr(mod, raise_)()
                block = types.SimpleNamespace(type="text", text=json.dumps(answer) if isinstance(answer, dict) else answer)
                return types.SimpleNamespace(stop_reason=stop, content=[block])

        class Anthropic:
            def __init__(self, **kw):
                fake.client_kw = kw
                self.beta = types.SimpleNamespace(messages=Messages())

        mod = types.ModuleType("anthropic")
        mod.Anthropic = Anthropic
        mod.APIStatusError = APIStatusError
        mod.AuthenticationError = AuthenticationError
        mod.APIConnectionError = APIConnectionError
        self.module = mod


@pytest.fixture
def claude(monkeypatch):
    def make(**kw):
        f = FakeClaude(**kw)
        monkeypatch.setitem(sys.modules, "anthropic", f.module)
        return f
    return make


ANSWER = {"title": "Acme Power Electric Bill October 2026", "summary": "Electric bill; $84.12 due Oct 24.",
          "doc_type": "Bill", "sender": "Acme Power and Light", "doc_date": "2026-10-03",
          "keywords": ["electricity", "utilities"]}


def test_claude_summary_request_and_result(claude):
    f = claude(answer=ANSWER)
    g = describe.claude_summary(BILL, "sk-test", "claude-opus-5-5")
    assert g == ANSWER
    kw = f.calls[0]
    assert f.client_kw["api_key"] == "sk-test"
    assert kw["model"] == "claude-opus-5-5"
    assert kw["output_config"]["format"]["schema"] == describe.SCHEMA
    assert "ACME POWER" in kw["messages"][0]["content"]
    assert kw["fallbacks"] == "default"


def test_claude_summary_cleans_bad_fields(claude):
    claude(answer=dict(ANSWER, doc_date="Oct 3", keywords=["a"] * 10, title="  Long  title. "))
    g = describe.claude_summary(BILL, "k")
    assert g["doc_date"] == "" and len(g["keywords"]) == 6 and g["title"] == "Long title"


def test_claude_summary_caps_long_text(claude):
    f = claude(answer=ANSWER)
    describe.claude_summary("word " * 50_000, "k")
    assert len(f.calls[0]["messages"][0]["content"]) < describe.MAX_CHARS + 200


@pytest.mark.parametrize("kw", [{"raise_": "APIStatusError"}, {"raise_": "APIConnectionError"},
                                {"answer": ANSWER, "stop": "refusal"}, {"answer": "not json"}])
def test_claude_failures_fall_back_to_local(claude, kw):
    claude(**kw)
    settings.update({"claude": {"api_key": "k"}, "document": {"describe": "claude"}})
    g = describe.describe(BILL, settings.get())
    assert g == describe.local_summary(BILL)


def test_describe_off_and_no_key_skips_claude(claude):
    f = claude(answer=ANSWER)
    assert describe.describe(BILL, settings.get()) is None  # default: off
    assert describe.claude_summary(BILL, "") is None
    assert not f.calls


def test_claude_mode_needs_key():
    with pytest.raises(ValueError, match="API key"):
        settings.update({"document": {"describe": "claude"}})


def test_claude_key_is_masked():
    settings.update({"claude": {"api_key": "sk-secret"}})
    assert settings.public()["claude"]["api_key"] == SECRET_MASK
    settings.update({"claude": {"api_key": SECRET_MASK, "model": "claude-sonnet-5-5"}})
    assert settings.get()["claude"]["api_key"] == "sk-secret"


def _pages(tmp_path, n):
    tmp_path.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(n):
        p = tmp_path / f"p{i + 1:04d}.png"
        text_page().save(p)
        out.append(p)
    return out


def test_make_pdf_describes_with_claude(tmp_path, monkeypatch, claude):
    seen = []
    monkeypatch.setattr(processing, "page_text", lambda p, lang: seen.append(p) or BILL)
    f = claude(answer=ANSWER)
    settings.update({"claude": {"api_key": "k"}, "document": {"describe": "claude", "filename": "{title}_{date}", "ocr": False}})
    s = settings.get()
    out = processing.process(_pages(tmp_path / "job", 5), "document", s, 100)
    assert out[0]["path"].name.startswith("Acme Power Electric Bill October 2026_")
    assert out[0]["meta"]["summary"] == ANSWER["summary"]
    assert "ACME POWER" in out[0]["meta"]["text"]
    assert len(seen) == processing.DESCRIBE_PAGES and len(f.calls) == 1


def test_file_metadata_for_drive():
    meta = dict(ANSWER, text="line one\fpage two")
    iid = store.add("/x.pdf", "x.pdf", "pdf", "scan", "Scans/PDF", "pending", pages=2, meta=meta)
    md = drive_mod.file_metadata(store.get(iid))
    assert md["description"].startswith("Electric bill; $84.12 due Oct 24.")
    assert "From: Acme Power and Light" in md["description"] and "Pages: 2" in md["description"]
    assert "Keywords: electricity, utilities" in md["description"]
    assert md["properties"]["docType"] == "Bill" and md["properties"]["documentDate"] == "2026-10-03"
    assert md["contentHints"]["indexableText"] == "line one\npage two"
    assert md["createdTime"].endswith("Z")
    for k, v in md["properties"].items():
        assert len(k.encode()) + len(v.encode()) <= 124


def test_file_metadata_without_meta_only_sets_time():
    iid = store.add("/x.pdf", "x.pdf", "pdf", "scan", "Scans/PDF", "pending")
    assert set(drive_mod.file_metadata(store.get(iid))) == {"createdTime"}


def test_long_property_is_trimmed():
    assert len(("sender" + drive_mod._prop("sender", "é" * 200)).encode()) <= 124


def test_upload_sends_metadata(tmp_path, monkeypatch):
    from test_drive import FakeHTTP, Resp
    settings.update({"drive": {"client_id": "cid", "client_secret": "sec"}})
    d = drive_mod.Drive()
    d.token = {"access_token": "at", "refresh_token": "rt", "expires_at": time.time() + 600}
    d.folders = {"Scans": "F"}
    d._verified = {"F": time.time()}
    d.http = FakeHTTP([
        ("POST", "upload/drive/v3/files", Resp(200, {}, {"Location": "https://upload/session"})),
        ("PUT", "upload/session", Resp(200, {"id": "FILE"})),
    ])
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF")
    d.upload(f, "Scans", {"description": "gist", "name": "ignored"})
    body = d.http.calls[0][2]["json"]
    assert body == {"description": "gist", "name": "x.pdf", "parents": ["F"]}
