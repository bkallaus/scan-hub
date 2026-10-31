"""Topic tags for a scanned document from Desert Ant Labs' Gist, which runs on
this machine (see gist/tag.mjs). Gist picks 1-3 of 36 fixed topics, such as
"Personal Finance" or "Health & Medicine", in 101 languages."""
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("topics")

SCRIPT = Path(os.environ.get("GIST_SCRIPT", Path(__file__).resolve().parent.parent / "gist" / "tag.mjs"))
INPUT_CHARS = 2000   # Gist is tuned for titles and short descriptions
TIMEOUT = 600        # the first run downloads the ~74 MB model


def gist_input(title, text):
    """What Gist reads: the title, then the start of the first page."""
    first = " ".join((text or "").split("\f")[0].split())
    return f"{title}\n{first}".strip()[:INPUT_CHARS]


def tag(text, top_k=3):
    """[{slug, name, score}, ...] best first; [] if Gist isn't available."""
    if not text.strip():
        return []
    node = shutil.which("node")
    if not node or not SCRIPT.is_file():
        log.warning("Topic tags need Node.js and %s, which aren't in this image", SCRIPT)
        return []
    model_dir = os.environ.get("GIST_MODEL_DIR")
    if model_dir:
        Path(model_dir).mkdir(parents=True, exist_ok=True)
    try:
        p = subprocess.run([node, str(SCRIPT)], input=json.dumps({"text": text, "topK": top_k}),
                           capture_output=True, text=True, timeout=TIMEOUT, cwd=SCRIPT.parent)
    except subprocess.TimeoutExpired:
        log.warning("Tagging topics timed out")
        return []
    if p.returncode != 0:
        tail = p.stderr.strip().splitlines()
        log.warning("Tagging topics failed: %s", tail[-1] if tail else p.returncode)
        return []
    try:
        topics = json.loads(p.stdout)
        return [{"slug": str(t["slug"]), "name": str(t["name"]), "score": round(float(t["score"]), 3)}
                for t in topics][:top_k]
    except (ValueError, KeyError, TypeError):
        log.warning("Gist returned something unexpected: %r", p.stdout[:200])
        return []

