"""Scanner engine: talks to the FF-680W through SANE (Epson's epsonscan2 backend),
watches for paper, runs ADF batches and hands pages to the processing pipeline.

Detection strategies
  sensor : poll a read-only SANE option (paper-loaded sensor or scan button)
  probe  : attempt an ADF batch every few seconds; an empty feeder returns
           "out of documents" immediately, a loaded one simply scans
  manual : only scan when "Scan now" is pressed in the web UI
  auto   : sensor if the backend exposes one, otherwise probe
"""
import json
import logging
import queue
import random
import re
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import processing
from .config import SIMULATE, WORK_DIR, settings
from .store import store

log = logging.getLogger("scanner")

EMPTY_RE = re.compile(r"out of documents|no more documents|no docs|feeder.*empty|document feeder out", re.I)
BUSY_RE = re.compile(r"device busy", re.I)
OFFLINE_RE = re.compile(r"open of device|device I/O|No scanner found|Invalid argument|timed out|"
                        r"connection|unreachable|no route", re.I)
# scanimage --batch progress chatter on stderr; not errors.
PROGRESS_RE = re.compile(r"^(Scanning (-?\d+|infinity) pages|Scanning page \d+|Scanned page \d+|Batch terminated)", re.I)
HOLD_PROBE_SECONDS = 45  # probe mode can't see the tray, so pause this long after an early stop
OFFLINE_MSG = "Can't reach the scanner. Is it switched on? Retrying automatically…"
SENSOR_RE = re.compile(
    r"((adf|paper|page|doc|document|feeder|media).*(load|present|detect|exist|avail|in$))"
    r"|((load|present).*(adf|paper|page|doc))", re.I)
BUTTON_RE = re.compile(r"^(scan|start|button|push|scan-button|button-\d+|.*-button|sw\d*)$", re.I)
SIZE_OPTS = ("scan-area", "paper-size", "page-size", "document-size", "scan-size", "size")
TRUTHY = ("yes", "1", "true", "on")


# ------------------------------------------------------------------ SANE parsing
@dataclass
class SaneOption:
    name: str
    spec: str = ""
    current: str | None = None
    flags: list = field(default_factory=list)

    @property
    def inactive(self):
        return self.current == "inactive" or "inactive" in self.flags

    @property
    def readonly(self):
        return any(f in ("read-only", "hardware") for f in self.flags)

    @property
    def is_bool(self):
        return self.spec.replace(" ", "") in ("yes|no", "(yes|no)")

    def choices(self):
        spec = re.sub(r"\(in steps of[^)]*\)", "", self.spec).strip()
        if ".." in spec:
            return []
        return [c.strip() for c in spec.split("|") if c.strip()]

    def range(self):
        m = re.match(r"\s*(-?[\d.]+)\.\.(-?[\d.]+)", self.spec)
        return (float(m.group(1)), float(m.group(2))) if m else None

    def as_dict(self):
        return {"name": self.name, "spec": self.spec, "current": self.current, "flags": self.flags}


OPT_LINE = re.compile(r"^(-{1,2}[A-Za-z0-9][\w\-]*)(\[=\(([^)]*)\)\])?\s*(.*)$")


def parse_options(text):
    opts = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("-"):
            continue
        m = OPT_LINE.match(line)
        if not m:
            continue
        name = m.group(1).lstrip("-")
        rest = m.group(4)
        brackets = []
        while True:
            bm = re.search(r"\s*\[([^\[\]]*)\]\s*$", rest)
            if not bm:
                break
            brackets.insert(0, bm.group(1))
            rest = rest[: bm.start()]
        spec = m.group(3) or rest.strip()
        opt = SaneOption(name=name, spec=spec)
        if brackets:
            opt.current = brackets[0]
            opt.flags = brackets[1:]
        opts[name] = opt
    return opts


def parse_devices(text):
    devs = []
    for line in text.splitlines():
        m = re.match(r"device [`'](.+?)' is an? (.*)$", line.strip())
        if m:
            devs.append({"name": m.group(1), "desc": m.group(2)})
    return devs


def _pick(choices, patterns, avoid=None):
    for pat in patterns:
        for c in choices:
            if re.search(pat, c, re.I) and not (avoid and re.search(avoid, c, re.I)):
                return c
    return None


def build_args(opts, s, mode):
    """Map our friendly settings onto whatever options the backend exposes."""
    sect = s["document"] if mode == "document" else s["photo"]
    args = []
    o = opts.get("mode")
    if o and not o.inactive and o.choices():
        want = "color" if mode == "photo" else sect["color"]
        pats = {"color": [r"^colou?r", r"colou?r", r"24"],
                "gray": [r"gr[ae]y", r"8\s*bit"],
                "bw": [r"lineart", r"binary", r"mono", r"black", r"b&w", r"\bbw\b"]}[want]
        v = _pick(o.choices(), pats)
        if v:
            args += ["--mode", v]
    o = opts.get("resolution")
    if o and not o.inactive:
        want = sect["resolution"]
        nums = [int(float(x)) for x in re.findall(r"\d+(?:\.\d+)?", o.spec.split("(")[0])]
        if o.range():
            lo, hi = o.range()
            val = int(max(lo, min(hi, want)))
        elif nums:
            val = min(nums, key=lambda n: abs(n - want))
        else:
            val = want
        args += ["--resolution", str(val)]
    o = opts.get("source")
    if o and not o.inactive and o.choices():
        dup = r"duplex|double|both|2[- ]?sided"
        if sect["duplex"]:
            v = _pick(o.choices(), [dup])
        else:
            v = _pick(o.choices(), [r"front", r"simplex", r"adf", r"feeder", r"single"], avoid=dup)
        if v:
            args += ["--source", v]
    for name in SIZE_OPTS:
        o = opts.get(name)
        if o and not o.inactive and o.choices():
            v = _pick(o.choices(), [r"^auto(?!.*long)", r"auto"])
            if v:
                args += [f"--{name}", v]
            break
    extra = s["scanner"]["extra_args"].strip()
    if extra:
        args += shlex.split(extra)
    return args


def find_sensor(opts):
    """Read-only yes/no options that report paper in the tray or a button press.
    Writable options are settings (e.g. a "paper size detection" toggle), not sensors."""
    found = []
    for o in opts.values():
        if o.is_bool and not o.inactive and o.readonly and (SENSOR_RE.search(o.name) or BUTTON_RE.match(o.name)):
            found.append(o)
    return found


# ------------------------------------------------------------------ backends
class SaneBackend:
    name = "sane"

    @staticmethod
    def _run(cmd, timeout):
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr

    @staticmethod
    def network_device(ip):
        """SANE name Epson's network plugin uses for a scanner at a fixed IP. It
        can be opened directly even when 'scanimage -L' doesn't list it."""
        return f"epsonscan2:networkscanner:esci2:network:{ip}" if ip else None

    def configure_network(self, ip):
        """Tell Epson's network plugin where the scanner is (manual 8.1.2 / 10.5)."""
        if not ip:
            return
        if shutil.which("epsonscan2"):
            try:
                subprocess.run(["epsonscan2", "--set-ip", ip], capture_output=True, timeout=30)
            except Exception as e:
                log.debug("epsonscan2 --set-ip failed: %s", e)
        for home in (Path.home(), Path("/home")):
            try:
                d = home / ".epsonscan2" / "Network"
                d.mkdir(parents=True, exist_ok=True)
                (d / "epsonscan2.conf").write_text(f"[Network]\n{ip}\n")
            except OSError as e:
                log.debug("could not write network conf in %s: %s", home, e)

    def list_devices(self):
        if not shutil.which("scanimage"):
            raise RuntimeError("scanimage not installed")
        _, out, err = self._run(["scanimage", "-L"], timeout=90)
        return parse_devices(out + "\n" + err)

    def options(self, dev):
        rc, out, err = self._run(["scanimage", "-d", dev, "-A"], timeout=60)
        if rc != 0 and not out:
            raise RuntimeError(err.strip() or f"scanimage -A failed ({rc})")
        return parse_options(out)

    def scan(self, dev, args, outdir, on_page=None, stall_timeout=180):
        outdir.mkdir(parents=True, exist_ok=True)
        cmd = ["scanimage", "-d", dev, f"--batch={outdir}/p%04d.pnm", "--format=pnm", *args]
        log.info("run: %s", " ".join(shlex.quote(c) for c in cmd))
        # stderr goes to a file: an unread pipe fills up (64 KB) if the driver is
        # chatty, and scanimage would then freeze mid-scan.
        errfile = outdir / "scanimage.log"
        with open(errfile, "w") as ef:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=ef)
        last_count, last_change = 0, time.time()
        while proc.poll() is None:
            time.sleep(0.5)
            n = len(list(outdir.glob("p*.pnm")))
            if n != last_count:
                last_count, last_change = n, time.time()
                if on_page:
                    on_page(n)
            if time.time() - last_change > stall_timeout:
                log.warning("No new page for %ss; stopping the scan", stall_timeout)
                proc.kill()
                break
        proc.wait()
        try:
            err = errfile.read_text(errors="replace")[-4000:]
        except OSError:
            err = ""
        pages = sorted(outdir.glob("p*.pnm"))
        # The page being written when scanimage dies can be truncated.
        pages = [p for p in pages if p.stat().st_size > 1024]
        return pages, proc.returncode, err


class SimBackend:
    """Fake FF-680W for testing the pipeline without hardware (SIMULATE_SCANNER=1)."""
    name = "simulated"

    def __init__(self):
        self.loaded = 0
        self.kind = None

    def load(self, pages, kind=None):
        self.loaded = max(0, int(pages))
        self.kind = kind

    def configure_network(self, ip):
        pass

    @staticmethod
    def network_device(ip):
        return None

    def list_devices(self):
        return [{"name": "simulated:FF-680W", "desc": "Epson FF-680W (simulated)"}]

    def options(self, dev):
        txt = f"""
    --mode Color|Gray|Lineart [Color]
    --resolution 50..600dpi [300]
    --source ADF Front|ADF Duplex [ADF Front]
    --page-loaded[=(yes|no)] [{'yes' if self.loaded else 'no'}] [read-only]
"""
        return parse_options(txt)

    def scan(self, dev, args, outdir, on_page=None, stall_timeout=180):
        from PIL import Image, ImageDraw
        outdir.mkdir(parents=True, exist_ok=True)
        if not self.loaded:
            return [], 7, "scanimage: sane_start: Document feeder out of documents\n"
        a = dict(zip(args[::2], args[1::2]))
        dpi = int(a.get("--resolution", 300))
        duplex = "Duplex" in a.get("--source", "")
        kind = self.kind or settings.get()["mode"]
        rnd = random.Random()
        n = 0
        for i in range(self.loaded):
            sides = ("front", "back") if duplex else ("front",)
            for side in sides:
                time.sleep(0.2)
                if kind == "photo":
                    w, h = int(8.5 * dpi), int(6.6 * dpi)
                    img = Image.new("RGB", (w, h), (8, 8, 8))
                    d = ImageDraw.Draw(img)
                    x0, y0 = int(0.4 * dpi), int(0.3 * dpi)
                    x1, y1 = x0 + 6 * dpi, y0 + 4 * dpi
                    if side == "front":
                        base = [rnd.randint(60, 200) for _ in range(3)]
                        for y in range(y0, y1, 4):
                            c = tuple(min(255, b + (y - y0) // 8) for b in base)
                            d.rectangle([x0, y, x1, y + 4], fill=c)
                        d.ellipse([x0 + dpi, y0 + dpi, x0 + 3 * dpi, y0 + 3 * dpi], fill=(240, 200, 90))
                    else:
                        d.rectangle([x0, y0, x1, y1], fill=(236, 232, 220))
                        if i % 3 == 0:
                            d.line([x0 + dpi, y0 + dpi, x0 + 4 * dpi, y0 + dpi + 20], fill=(30, 30, 120), width=5)
                            d.text((x0 + dpi, y0 + dpi * 1.4), f"Summer {1970 + i}", fill=(30, 30, 120))
                else:
                    w, h = int(8.5 * dpi), int(11 * dpi)
                    img = Image.new("RGB", (w, h), (250, 250, 248))
                    d = ImageDraw.Draw(img)
                    if side == "front" or i % 2 == 1:
                        d.text((dpi, dpi // 2), f"Simulated page {i + 1} ({side})", fill=(0, 0, 0))
                        y = dpi
                        while y < h - dpi:
                            d.rectangle([dpi, y, w - dpi - rnd.randint(0, 3 * dpi), y + dpi // 12], fill=(40, 40, 40))
                            y += dpi // 4
                n += 1
                img.save(outdir / f"p{n:04d}.pnm")
                if on_page:
                    on_page(n)
        self.loaded = 0
        return sorted(outdir.glob("p*.pnm")), 0, "scanimage: sane_start: Document feeder out of documents\n"


# ------------------------------------------------------------------ processing worker
class Processor(threading.Thread):
    """Runs PDF/photo processing (and OCR) off the scanner thread so the next
    stack can be scanned while the previous one is still being converted."""

    def __init__(self):
        super().__init__(daemon=True, name="processor")
        self.q = queue.Queue()
        self.active = 0

    def submit(self, pages, dirs, mode, dpi):
        self.q.put({"pages": list(pages), "dirs": list(dirs), "mode": mode, "dpi": dpi})

    @property
    def backlog(self):
        return self.q.qsize() + self.active

    def run(self):
        while True:
            job = self.q.get()
            self.active = 1
            try:
                self._process_job(job)
            except Exception:
                log.exception("Unexpected processing error")
            finally:
                self.active = 0

    @staticmethod
    def _process_job(job):
        s = settings.get()
        try:
            outputs = processing.process(job["pages"], job["mode"], s, job["dpi"])
        except Exception:
            failed = []
            for d in job["dirs"]:
                dest = d.with_name("failed_" + d.name)
                try:
                    d.rename(dest)
                    failed.append(str(dest))
                except OSError:
                    failed.append(str(d))
            log.exception("Processing failed; raw pages kept in %s", ", ".join(failed))
            return
        for out in outputs:
            status = "pending" if s["drive"]["upload_scans"] else "local"
            folder = s["drive"]["pdf_folder"] if out["kind"] == "pdf" else s["drive"]["photo_folder"]
            store.add(out["path"], out["path"].name, out["kind"], "scan", folder, status,
                      size=out["path"].stat().st_size, pages=out.get("pages"))
        if outputs:
            log.info("Saved %d file(s): %s", len(outputs), ", ".join(o["path"].name for o in outputs))
        for d in job["dirs"]:
            shutil.rmtree(d, ignore_errors=True)


processor = Processor()


# ------------------------------------------------------------------ scanner service
class ScanService(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="scanner")
        self.backend = SimBackend() if SIMULATE else SaneBackend()
        self.wake = threading.Event()
        self.scan_request = threading.Event()
        self.rediscover_request = threading.Event()
        self.device = None
        self.devices = []
        self.opts = {}
        self.sensors = []
        self.detection = None                 # effective strategy
        self.state = "starting"
        self.message = "Starting up"
        self.progress = 0
        self.last_scan = None
        self.pending_doc = None               # {"pages": [...], "dirs": [...], "deadline": t, "dpi": n}
        self._errors = 0
        self._last_error = None
        self._configured_ip = None
        # Set when a batch stops early (jam, double feed, power blip). Auto-scan
        # then waits for the tray to be emptied/reloaded or for "Scan now",
        # instead of feeding the rest of the stack into a separate file.
        self.hold = None

    # ---- public API used by the web layer (called from other threads)
    def status(self):
        pd = self.pending_doc                 # local copy: the scanner thread may clear it
        pend = None
        if pd:
            pend = {"pages": len(pd["pages"]), "seconds_left": max(0, int(pd["deadline"] - time.time()))}
        return {
            "state": self.state, "message": self.message, "device": self.device,
            "backend": self.backend.name, "detection": self.detection,
            "sensors": [o.name for o in self.sensors], "progress": self.progress,
            "last_scan": self.last_scan, "pending_document": pend,
            "processing": processor.backlog,
        }

    def request_scan(self):
        self.scan_request.set()
        self.wake.set()

    def request_rediscover(self):
        self.rediscover_request.set()
        self.wake.set()

    def finish_document(self):
        pd = self.pending_doc
        if pd:
            pd["deadline"] = 0
            self.wake.set()

    def _set(self, state, message):
        self.state, self.message = state, message

    # ---- startup recovery
    def recover_jobs(self):
        """Process scans left in the work folder by a crash or restart."""
        # Remove stale temp folders first: the processor creates new ones as
        # soon as a recovered job is submitted.
        for d in WORK_DIR.glob("tmp_*"):
            shutil.rmtree(d, ignore_errors=True)
        for d in sorted(WORK_DIR.glob("job_*")):
            if not d.is_dir():
                continue
            pages = sorted(p for p in d.glob("p*.pnm") if p.stat().st_size > 0)
            meta = {}
            try:
                meta = json.loads((d / "job.json").read_text())
            except Exception:
                pass
            if pages and meta.get("mode") in ("document", "photo"):
                log.info("Recovering %d unprocessed page image(s) from %s", len(pages), d.name)
                processor.submit(pages, [d], meta["mode"], meta.get("dpi"))
            elif pages:
                d.rename(d.with_name("failed_" + d.name))
                log.warning("Unprocessed pages without job info kept in %s", d.with_name("failed_" + d.name))
            else:
                shutil.rmtree(d, ignore_errors=True)

    # ---- discovery
    def discover(self):
        s = settings.get()["scanner"]
        if s["ip"] != self._configured_ip:
            self.backend.configure_network(s["ip"])
            self._configured_ip = s["ip"]
        self._set("starting", "Looking for the scanner…")
        dev = s["device"].strip() or None
        opts = None
        direct = None if dev else self.backend.network_device(s["ip"])
        if direct:
            # Fast path: open the scanner straight at its IP.
            try:
                opts = self.backend.options(direct)
                if opts:
                    dev = direct
                else:
                    opts = None
            except Exception as e:
                log.info("Direct connection to %s failed (%s); searching the network", s["ip"], e)
        if not dev:
            devs = self.backend.list_devices()
            self.devices = devs
            names = [d["name"] for d in devs]
            dev = (next((n for n in names if "epsonscan2" in n and s["ip"] and s["ip"] in n), None)
                   or next((n for n in names if "epsonscan2" in n), None)
                   or next((d["name"] for d in devs if re.search(r"FF-?680", d["desc"], re.I)), None)
                   or (names[0] if names else None))
        if not dev:
            raise RuntimeError("No scanner found. Check the scanner IP and that it is on and awake.")
        if opts is None:
            opts = self.backend.options(dev)
        if not self.devices:
            self.devices = [{"name": dev, "desc": "connected directly"}]
        self.device, self.opts = dev, opts
        self.sensors = find_sensor(opts)
        want = s["detection"]
        if want == "auto":
            self.detection = "sensor" if self.sensors else "probe"
        elif want == "sensor" and not self.sensors:
            log.warning("Sensor detection requested but the driver exposes no sensor; using probe")
            self.detection = "probe"
        else:
            self.detection = want
        log.info("%s %s (detection: %s%s)", "Reconnected to" if self._last_error else "Using device",
                 dev, self.detection, f", sensors: {[o.name for o in self.sensors]}" if self.sensors else "")
        self._errors = 0
        self._last_error = None

    def _sensor_triggered(self, quiet=False):
        opts = self.backend.options(self.device)
        for o in self.sensors:
            cur = opts.get(o.name)
            if cur and (cur.current or "").lower() in TRUTHY:
                if not quiet:
                    log.info("Sensor %s is active", o.name)
                return True
        return False

    # ---- scanning
    def _scan_once(self, trigger):
        s = settings.get()
        mode = s["mode"]
        if self.pending_doc and (mode != "document" or s["document"]["combine_seconds"] == 0):
            self._finalize_doc()  # keep files in scan order
        args = build_args(self.opts, s, mode)
        dpi = int(args[args.index("--resolution") + 1]) if "--resolution" in args else None
        job = WORK_DIR / f"job_{time.strftime('%Y%m%d_%H%M%S')}_{random.randint(1000, 9999)}"
        job.mkdir(parents=True, exist_ok=True)
        (job / "job.json").write_text(json.dumps({"mode": mode, "dpi": dpi}))
        self.progress = 0

        def on_page(n):
            self.progress = n
            self._set("scanning", f"Scanning… {n} page{'s' if n != 1 else ''}")

        if trigger != "probe":
            self._set("scanning", "Starting scan…")
        try:
            pages, rc, err = self.backend.scan(self.device, args, job, on_page=on_page)
        except Exception:
            shutil.rmtree(job, ignore_errors=True)
            raise
        lines = [l.strip() for l in err.splitlines() if l.strip() and not PROGRESS_RE.match(l.strip())]
        # Prefer scanimage's own message over driver debug chatter.
        err_tail = next((l for l in reversed(lines) if l.startswith("scanimage:")), lines[-1] if lines else "")
        err_tail = err_tail[:300]
        if not pages:
            shutil.rmtree(job, ignore_errors=True)
            if EMPTY_RE.search(err) or rc == 7:
                return "empty"
            if BUSY_RE.search(err):
                return "busy"
            raise RuntimeError(err_tail or f"scan failed (exit {rc})")
        stopped_early = (rc not in (0, 7) or (err_tail and not EMPTY_RE.search(err_tail)))
        if stopped_early:
            log.warning("Scan stopped early: %s", err_tail or f"exit code {rc}")
            self.hold = {"reason": err_tail or f"exit code {rc}", "since": time.time()}
        log.info("Scanned %d page image(s) in %s mode", len(pages), mode)
        self.last_scan = {"time": time.time(), "pages": len(pages), "mode": mode}
        combine = s["document"]["combine_seconds"]
        if mode == "document" and combine > 0:
            if not self.pending_doc:
                self.pending_doc = {"pages": [], "dirs": [], "deadline": 0, "dpi": dpi}
            self.pending_doc["pages"] += pages
            self.pending_doc["dirs"].append(job)
            self.pending_doc["deadline"] = time.time() + combine
            log.info("Holding document open for %ss in case more pages follow", combine)
        else:
            processor.submit(pages, [job], mode, dpi)
        return "scanned"

    def _finalize_doc(self):
        pd, self.pending_doc = self.pending_doc, None
        if pd:
            processor.submit(pd["pages"], pd["dirs"], "document", pd.get("dpi"))

    # ---- main loop
    def run(self):
        try:
            self.recover_jobs()
        except Exception:
            log.exception("Job recovery failed")
        while True:
            try:
                self._tick()
                self._last_error = None
            except Exception as e:  # noqa: BLE001
                self._errors += 1
                msg = str(e) or e.__class__.__name__
                offline = self.device is None or bool(OFFLINE_RE.search(msg))
                if msg != self._last_error:  # don't repeat the same error every retry
                    log.error("%s", msg)
                    self._last_error = msg
                self.scan_request.clear()  # a "Scan now" tap shouldn't fire hours later
                self._set("offline" if offline else "error", OFFLINE_MSG if offline else msg)
                if self._errors >= 3 or (offline and self._errors >= 2):
                    self.device = None
                self.wake.wait(min(60, 10 * self._errors))
                self.wake.clear()

    def _tick(self):
        # Close an expired multi-batch PDF first, so it isn't held up if the
        # scanner has gone offline in the meantime.
        if self.pending_doc and time.time() >= self.pending_doc["deadline"]:
            self._finalize_doc()
        if self.rediscover_request.is_set() or self.device is None:
            self.rediscover_request.clear()
            self.device = None
            self.discover()
        s = settings.get()

        if self.scan_request.is_set():
            self.scan_request.clear()
            self.hold = None
            r = self._scan_once("manual")
            if r == "empty":
                self._set("idle", "No paper in the feeder")
                log.info("Scan requested but the feeder is empty")
            elif r == "busy":
                self._set("idle", "Scanner busy (in use by another computer?)")
            else:
                self._idle_message(s)
            self._errors = 0
            return

        if s["scanner"]["paused"] or self.detection == "manual":
            if s["scanner"]["paused"]:
                self._set("paused", "Auto-scan paused")
            else:
                self._set("idle", "Ready. Press Scan now to scan")
            self._sleep(s)
            return

        if self.hold:
            if self.detection == "sensor":
                if not self._sensor_triggered(quiet=True):
                    self.hold = None  # tray emptied: next load starts a new scan
                    log.info("Feeder emptied; auto-scan resumed")
            elif time.time() - self.hold["since"] > HOLD_PROBE_SECONDS:
                self.hold = None
            if self.hold:
                self._set("idle", f"Last batch stopped early ({self.hold['reason'][:80]}). "
                                  "Fix and reload the stack, or tap Scan now to continue.")
                self._sleep(s)
                return
        if self.detection == "sensor":
            r = "empty"
            if self._sensor_triggered():
                time.sleep(1.5)  # let the stack settle in the tray
                r = self._scan_once("sensor")
        else:
            r = self._scan_once("probe")
        self._errors = 0
        if r == "busy":
            self._set("idle", "Scanner busy (in use by another computer?)")
        elif not self.hold:
            self._idle_message(s)
            if r == "scanned":
                return  # look for more paper right away
        self._sleep(s)

    def _idle_message(self, s):
        pd = self.pending_doc
        if pd:
            self._set("idle", "Load more pages to add to this PDF, or tap Finish")
        else:
            label = "PDF" if s["mode"] == "document" else "photos"
            self._set("idle", f"Waiting for paper → {label}")

    def _sleep(self, s):
        timeout = s["scanner"]["poll_seconds"]
        pd = self.pending_doc
        if pd:
            timeout = max(0.2, min(timeout, pd["deadline"] - time.time()))
        self.wake.wait(timeout)
        self.wake.clear()


scanner = ScanService()
