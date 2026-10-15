import json

import pytest

from app import scanner as scanner_mod
from app.config import WORK_DIR, settings
from app.scanner import Processor, ScanService, SimBackend
from app.store import store


class FakeProcessor:
    def __init__(self):
        self.jobs = []
        self.backlog = 0

    def submit(self, pages, dirs, mode, dpi):
        self.jobs.append({"pages": list(pages), "dirs": list(dirs), "mode": mode, "dpi": dpi})


@pytest.fixture
def svc(monkeypatch):
    fp = FakeProcessor()
    monkeypatch.setattr(scanner_mod, "processor", fp)
    s = ScanService()
    s.backend = SimBackend()
    s.discover()
    s.fake_processor = fp
    return s


def test_discover_sim_uses_sensor(svc):
    assert svc.device == "simulated:FF-680W"
    assert svc.detection == "sensor"
    assert [o.name for o in svc.sensors] == ["page-loaded"]


def test_scan_empty_feeder(svc):
    assert svc._scan_once("probe") == "empty"
    assert not list(WORK_DIR.glob("job_*"))


def test_scan_document_batch_submits_job(svc):
    settings.update({"document": {"duplex": False}})
    svc.backend.load(3, "document")
    assert svc._scan_once("manual") == "scanned"
    job = svc.fake_processor.jobs[0]
    assert job["mode"] == "document" and len(job["pages"]) == 3
    meta = json.loads((job["dirs"][0] / "job.json").read_text())
    assert meta["mode"] == "document"
    assert svc.hold is None


def test_combine_window_holds_then_finalises(svc):
    settings.update({"document": {"combine_seconds": 30, "duplex": False}})
    svc.backend.load(2, "document")
    svc._scan_once("manual")
    svc.backend.load(1, "document")
    svc._scan_once("manual")
    assert svc.fake_processor.jobs == []
    assert len(svc.pending_doc["pages"]) == 3
    svc.finish_document()
    svc._tick()  # finalises the expired document first
    assert len(svc.fake_processor.jobs[0]["pages"]) == 3
    assert len(svc.fake_processor.jobs[0]["dirs"]) == 2


class StderrBackend(SimBackend):
    """Real scanimage --batch progress chatter, as printed by sane-backends."""

    def __init__(self, err, rc):
        super().__init__()
        self.err, self.rc = err, rc

    def scan(self, dev, args, outdir, on_page=None, stall_timeout=180):
        pages, _, _ = super().scan(dev, args, outdir, on_page, stall_timeout)
        return pages, self.rc, self.err


# Captured from scanimage 1.1.1 with the SANE "test" backend in this image.
BATCH_OK = ("Scanning infinity pages, incrementing by 1, numbering from 1\n"
            "Scanning page 1\nScanned page 1. (scanner status = 5)\n"
            "Scanning page 2\nScanned page 2. (scanner status = 5)\n"
            "Scanning page 3\nscanimage: sane_start: Document feeder out of documents\n"
            "Batch terminated, 2 pages scanned\n")


@pytest.mark.parametrize("err,rc,held", [
    (BATCH_OK, 0, False),
    # Same, but without scanimage's own "out of documents" line.
    ("Scanning page 1\nScanned page 1. (scanner status = 5)\nBatch terminated, 1 pages scanned\n", 0, False),
    # Driver noise before the final scanimage line doesn't count as an error.
    ("Scanning page 1\nScanned page 1. (scanner status = 5)\nsh: 1: killall: not found\n"
     "scanimage: sane_start: Document feeder out of documents\nBatch terminated, 1 pages scanned\n", 0, False),
    ("Scanning page 1\nScanned page 1. (scanner status = 5)\n"
     "scanimage: sane_read: Document feeder jammed\n", 9, True),
])
def test_stopped_early_detection(svc, err, rc, held):
    b = StderrBackend(err, rc)
    b.load(2, "document")
    svc.backend = b
    assert svc._scan_once("probe") == "scanned"
    assert (svc.hold is not None) == held


def test_recover_jobs_resubmits_and_quarantines(svc, monkeypatch):
    good = WORK_DIR / "job_recover_good"
    good.mkdir(parents=True)
    (good / "p0001.pnm").write_bytes(b"x" * 2000)
    (good / "job.json").write_text(json.dumps({"mode": "document", "dpi": 300}))
    orphan = WORK_DIR / "job_recover_orphan"
    orphan.mkdir()
    (orphan / "p0001.pnm").write_bytes(b"x" * 2000)
    (WORK_DIR / "tmp_pdf_stale").mkdir()
    svc.recover_jobs()
    assert [j["dirs"][0].name for j in svc.fake_processor.jobs] == ["job_recover_good"]
    assert (WORK_DIR / "failed_job_recover_orphan").is_dir()
    assert not (WORK_DIR / "tmp_pdf_stale").exists()


def test_processor_end_to_end_records_items(monkeypatch, svc):
    settings.update({"document": {"duplex": False}})
    svc.backend.load(2, "document")
    svc._scan_once("manual")
    job = svc.fake_processor.jobs[0]
    Processor._process_job(job)
    items = store.recent()
    assert len(items) == 1 and items[0]["kind"] == "pdf" and items[0]["status"] == "pending"
    assert items[0]["folder"] == "Scans/PDF"
    assert not job["dirs"][0].exists()


def test_processor_failure_keeps_raw_pages(monkeypatch, tmp_path):
    d = WORK_DIR / "job_will_fail"
    d.mkdir(parents=True)
    monkeypatch.setattr(scanner_mod.processing, "process", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    Processor._process_job({"pages": [], "dirs": [d], "mode": "document", "dpi": 300})
    assert (WORK_DIR / "failed_job_will_fail").is_dir()


# ---------------------------------------------------------------- network search
class NetBackend(SimBackend):
    """Real-driver-shaped backend: only network devices at `live` IPs open."""
    name = "sane"

    def __init__(self, live, candidates, listed=()):
        super().__init__()
        self.live, self.candidates, self.listed = set(live), list(candidates), list(listed)
        self.opened = []

    @staticmethod
    def network_device(ip):
        return f"epsonscan2:networkscanner:esci2:network:{ip}" if ip else None

    def list_devices(self):
        return list(self.listed)

    def find_ips(self):
        return list(self.candidates)

    def options(self, dev):
        self.opened.append(dev)
        ip = dev.rsplit(":", 1)[-1]
        if ip not in self.live:
            raise RuntimeError(f"scanimage: open of device {dev} failed: Error during device I/O")
        return super().options(dev)


def _svc(backend, monkeypatch):
    monkeypatch.setattr(scanner_mod, "processor", FakeProcessor())
    s = ScanService()
    s.backend = backend
    return s


def test_search_finds_and_saves_scanner_ip(monkeypatch):
    b = NetBackend(live={"192.168.86.77"}, candidates=["192.168.86.12", "192.168.86.77"])
    s = _svc(b, monkeypatch)
    s.discover()
    assert s.device == "epsonscan2:networkscanner:esci2:network:192.168.86.77"
    assert settings.get()["scanner"]["ip"] == "192.168.86.77"  # remembered for next start


def test_wrong_saved_ip_is_replaced(monkeypatch):
    settings.update({"scanner": {"ip": "192.168.1.50"}})
    b = NetBackend(live={"192.168.86.77"}, candidates=["192.168.1.50", "192.168.86.77"])
    s = _svc(b, monkeypatch)
    s.discover()
    assert settings.get()["scanner"]["ip"] == "192.168.86.77"
    # the saved IP is tried once directly, not again during the search
    assert b.opened.count("epsonscan2:networkscanner:esci2:network:192.168.1.50") == 1


def test_working_saved_ip_skips_search(monkeypatch):
    settings.update({"scanner": {"ip": "192.168.86.77"}})
    b = NetBackend(live={"192.168.86.77"}, candidates=[])
    b.find_ips = lambda: pytest.fail("network search shouldn't run when the saved IP works")
    _svc(b, monkeypatch).discover()


def test_driver_listed_device_preferred_over_search(monkeypatch):
    listed = [{"name": "epsonscan2:FF-680W:001:network:192.168.86.77", "desc": "EPSON FF-680W"}]
    b = NetBackend(live={"192.168.86.77"}, candidates=[], listed=listed)
    b.options = lambda dev: SimBackend.options(b, dev)
    b.find_ips = lambda: pytest.fail("no search needed when the driver lists the scanner")
    s = _svc(b, monkeypatch)
    s.discover()
    assert s.device == listed[0]["name"]


def test_search_finds_nothing(monkeypatch):
    b = NetBackend(live=set(), candidates=["192.168.86.12"])
    s = _svc(b, monkeypatch)
    with pytest.raises(RuntimeError, match="No scanner found"):
        s.discover()
    assert settings.get()["scanner"]["ip"] == ""


def test_explicit_device_never_searches(monkeypatch):
    settings.update({"scanner": {"device": "epsonscan2:networkscanner:esci2:network:192.168.86.5"}})
    b = NetBackend(live=set(), candidates=["192.168.86.77"])
    b.find_ips = lambda: pytest.fail("a hand-picked SANE device must not be overridden")
    with pytest.raises(RuntimeError):
        _svc(b, monkeypatch).discover()
