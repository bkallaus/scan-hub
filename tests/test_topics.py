import json
import shutil
from pathlib import Path

import pytest

from app import drive as drive_mod
from app import processing, topics
from app.config import settings
from app.store import store
from conftest import text_page

needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs Node.js")

FAKE_GIST = """
let s = ""; for await (const c of process.stdin) s += c;
const req = JSON.parse(s);
if (req.text.includes("FAIL")) { process.stderr.write("gist: boom\\n"); process.exit(1); }
process.stdout.write(JSON.stringify([
  {slug: "finance", name: "Personal Finance", score: 0.912345, extra: 1},
  {slug: "home-garden", name: "Home & Garden", score: 0.4},
  {slug: "law", name: "Law", score: 0.2}].slice(0, req.topK)));
"""


@pytest.fixture
def fake_gist(tmp_path, monkeypatch):
    script = tmp_path / "tag.mjs"
    script.write_text(FAKE_GIST)
    monkeypatch.setattr(topics, "SCRIPT", script)
    return script


@needs_node
def test_tag_parses_topics(fake_gist):
    assert topics.tag("an electric bill", top_k=2) == [
        {"slug": "finance", "name": "Personal Finance", "score": 0.912},
        {"slug": "home-garden", "name": "Home & Garden", "score": 0.4}]


@needs_node
def test_tag_failure_returns_nothing(fake_gist):
    assert topics.tag("FAIL") == []


def test_tag_without_script_or_text(monkeypatch, tmp_path):
    monkeypatch.setattr(topics, "SCRIPT", tmp_path / "missing.mjs")
    assert topics.tag("text") == []
    assert topics.tag("   ") == []


def test_gist_input_is_title_plus_first_page():
    s = topics.gist_input("Acme Bill", "line one\n  line   two\fpage two")
    assert s == "Acme Bill\nline one line two"
    assert len(topics.gist_input("t", "x" * 10_000)) == topics.INPUT_CHARS


def test_topic_title():
    assert processing.topic_title("Health & Medicine") == "Health and Medicine"


def test_render_name_topic_field():
    assert processing.render_name("{topic}_{n}", topic="Law") == "Law_001"
    assert processing.render_name("{topic}_{n}") == "Document_001"


def test_topic_field_allowed_in_document_names_only():
    settings.update({"document": {"filename": "{topic} - {title}"}})
    with pytest.raises(ValueError):
        settings.update({"photo": {"filename": "{topic}"}})


def _pages(folder, n):
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(n):
        p = folder / f"p{i + 1:04d}.png"
        text_page().save(p)
        out.append(p)
    return out


def test_make_pdf_tags_topics(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(processing, "page_text", lambda p, lang: "ACME POWER\nINVOICE\nAmount due $5")
    monkeypatch.setattr(topics, "tag", lambda text, top_k=3: seen.append(text) or [
        {"slug": "finance", "name": "Personal Finance", "score": 0.9}])
    settings.update({"document": {"topics": True, "filename": "{topic} - {title}", "ocr": False}})
    out = processing.process(_pages(tmp_path / "job", 2), "document", settings.get(), 100)
    assert out[0]["path"].name == "Personal Finance - Acme Power Invoice.pdf"
    assert out[0]["meta"]["topics"][0]["slug"] == "finance"
    assert out[0]["meta"]["title"] == "Acme Power Invoice"
    assert seen[0].startswith("Acme Power Invoice\nACME POWER")


def test_topic_names_the_file_when_no_heading_is_found(tmp_path, monkeypatch):
    monkeypatch.setattr(processing, "page_text", lambda p, lang: "")
    monkeypatch.setattr(processing, "doc_title", lambda text: "")
    monkeypatch.setattr(topics, "tag", lambda text, top_k=3: [{"slug": "law", "name": "Law & Courts", "score": 0.5}])
    settings.update({"document": {"topics": True, "filename": "{title}", "ocr": False}})
    out = processing.process(_pages(tmp_path / "job", 1), "document", settings.get(), 100)
    assert out[0]["path"].name == "Law and Courts.pdf"


def test_topics_off_by_default_never_runs_gist(tmp_path, monkeypatch):
    monkeypatch.setattr(topics, "tag", lambda *a, **k: pytest.fail("Gist ran while off"))
    processing.process(_pages(tmp_path / "job", 1), "document", settings.get(), 100)


def test_drive_metadata_has_topics():
    meta = {"title": "Acme Bill", "topics": [{"slug": "finance", "name": "Personal Finance", "score": 0.9},
                                             {"slug": "law", "name": "Law", "score": 0.3}]}
    iid = store.add("/x.pdf", "x.pdf", "pdf", "scan", "Scans/PDF", "pending", meta=meta)
    md = drive_mod.file_metadata(store.get(iid))
    assert md["properties"]["topics"] == "finance,law"
    assert "Topics: Personal Finance, Law" in md["description"]


def test_claude_gets_topic_hint(monkeypatch):
    from app import describe
    seen = {}
    monkeypatch.setattr(describe, "claude_summary", lambda text, key, model, t: seen.update(t=t))
    settings.update({"claude": {"api_key": "k"}, "document": {"describe": "claude"}})
    t = [{"slug": "law", "name": "Law", "score": 0.5}]
    describe.describe("text", settings.get(), t)
    assert seen["t"] == t
    assert "under: Law." in describe._user_content("body", t)
