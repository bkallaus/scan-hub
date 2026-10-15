"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
let settings = null;
let lastStatus = null;

async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), 2600);
}

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const get = (obj, path) => path.split(".").reduce((o, k) => (o ? o[k] : undefined), obj);

function nested(path, value) {
  const keys = path.split("."); const out = {}; let o = out;
  keys.slice(0, -1).forEach((k) => (o = o[k] = {}));
  o[keys.at(-1)] = value;
  return out;
}

// ---------------------------------------------------------------- settings form
function fillForm() {
  for (const el of $$("[data-key]")) {
    const v = get(settings, el.dataset.key);
    if (el.type === "checkbox") el.checked = !!v;
    else if (document.activeElement !== el) {
      if (el.tagName === "SELECT" && ![...el.options].some((o) => o.value == v)) el.add(new Option(v, v));
      el.value = v ?? "";
    }
  }
}

async function save(patch, msg = "Saved") {
  try {
    settings = await api("/api/settings", { method: "PUT", body: patch });
    fillForm(); toast(msg); refresh();
  } catch (e) { toast("Not saved: " + e.message); fillForm(); }
}

for (const el of $$("[data-key]")) {
  el.addEventListener("change", () => {
    let v = el.type === "checkbox" ? el.checked : el.value;
    if (el.dataset.type === "int") v = parseInt(v, 10);
    if (el.dataset.type === "float") v = parseFloat(v);
    save(nested(el.dataset.key, v));
  });
}

// ---------------------------------------------------------------- presets
const ICONS = {
  bolt: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="m12.5 10.5-3 4.5h4l-3 4.5"/>',
  doc: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 13h6M9 17h6M9 9h2"/>',
  sparkle: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="m12 10.5 1.1 2.4 2.4 1.1-2.4 1.1L12 17.5l-1.1-2.4-2.4-1.1 2.4-1.1z"/>',
  stack: '<rect x="7" y="7" width="14" height="12" rx="1.5"/><path d="M4 16V5.5A1.5 1.5 0 0 1 5.5 4H17"/><path d="m21 16-4-4-7 7"/>',
  image: '<rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="8.5" cy="10" r="1.5"/><path d="m21 16-5-5-8 8"/>',
  archive: '<rect x="3" y="4" width="18" height="4" rx="1"/><path d="M5 8v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8"/><path d="M10 12h4"/>',
};
const GROUPS = { document: "Documents → PDF", photo: "Photos → image files" };
let presets = [];

function renderPresets() {
  $("#presets").innerHTML = Object.entries(GROUPS).map(([mode, label]) => `
    <h3 class="group">${label}</h3>
    <div class="preset-grid">${presets.filter((p) => p.mode === mode).map((p) => `
      <button class="preset" role="radio" aria-checked="false" data-preset="${esc(p.id)}">
        <svg class="pico" viewBox="0 0 24 24" aria-hidden="true">${ICONS[p.icon] || ICONS.doc}</svg>
        <span class="ptitle">${esc(p.title)}${p.recommended ? ' <span class="rec">Recommended</span>' : ""}</span>
        <span class="pdesc">${esc(p.desc)}</span>
        <span class="pspec">${p.spec.split(" · ").map((x) => `<span>${esc(x)}</span>`).join(" · ")}</span>
      </button>`).join("")}
    </div>`).join("");
  $$("[data-preset]").forEach((b) => (b.onclick = async () => {
    try {
      settings = await api(`/api/presets/${b.dataset.preset}/apply`, { method: "POST" });
      fillForm(); markPreset(b.dataset.preset);
      toast(presets.find((p) => p.id === b.dataset.preset).title);
      refresh();
    } catch (e) { toast("Not changed: " + e.message); }
  }));
}

function markPreset(id) {
  $$("[data-preset]").forEach((b) => b.setAttribute("aria-checked", b.dataset.preset === id));
  $("#custom").hidden = !!id || !presets.length;
}

// ---------------------------------------------------------------- actions
const act = (p, msg) => api(p, { method: "POST" }).then(() => msg && toast(msg)).catch((e) => toast(e.message));
$("#scanNow").onclick = () => act("/api/scan", "Scanning…");
$("#pause").onclick = () => save({ scanner: { paused: !settings.scanner.paused } },
  settings.scanner.paused ? "Auto-scan on" : "Auto-scan paused");
$("#finish").onclick = () => act("/api/document/finish", "Finishing PDF…");
$("#rediscover").onclick = () => act("/api/scanner/rediscover", "Reconnecting…");
$("#retryErrors").onclick = () => act("/api/items/retry-errors", "Retrying").then(loadItems);
$("#showOpts").onclick = async () => {
  let d;
  try { d = await api("/api/scanner/devices"); } catch (e) { return toast(e.message); }
  const pre = $("#opts");
  pre.hidden = false;
  pre.textContent = `Selected: ${d.selected || "none"}\n\nDevices:\n` +
    (d.devices.map((x) => `  ${x.name}  (${x.desc})`).join("\n") || "  none") +
    "\n\nOptions:\n" + (d.options.map((o) => `  --${o.name} ${o.spec} [${o.current}] ${o.flags.map((f) => `[${f}]`).join(" ")}`).join("\n") || "  none");
};
$$("[data-sim]").forEach((b) => b.addEventListener("click", () =>
  api("/api/simulate/load", { method: "POST", body: { pages: +b.dataset.sim } }).then(() => toast("Paper loaded")).catch((e) => toast(e.message))));

// ---------------------------------------------------------------- status rendering
const STATE = {
  idle: ["Ready", "ok"], scanning: ["Scanning", "busy"], processing: ["Processing", "busy"],
  paused: ["Paused", "warn"], starting: ["Connecting", ""], offline: ["Offline", "bad"], error: ["Error", "bad"],
};

function renderStatus(st) {
  const sc = st.scanner;
  const [label, cls] = STATE[sc.state] || [sc.state, ""];
  const pill = $("#pill"); pill.textContent = label; pill.className = "pill " + cls;
  $("#msg").textContent = sc.message;
  $("#device").textContent = sc.device
    ? `${sc.device} · start: ${sc.detection}${sc.sensors.length ? " (" + sc.sensors.join(", ") + ")" : ""}`
    : "";
  $("#pause").textContent = st.paused ? "Resume auto-scan" : "Pause auto-scan";
  $("#scanNow").disabled = ["scanning", "starting", "offline"].includes(sc.state);
  $("#proc").textContent = sc.processing ? `Converting ${sc.processing} batch${sc.processing > 1 ? "es" : ""} in the background…` : "";
  const pd = sc.pending_document;
  $("#finish").hidden = !pd;
  if (pd) {
    $("#finish").textContent = `Finish PDF (${pd.pages} pages)`;
    if (sc.state === "idle") $("#msg").textContent = `Load more pages within ${pd.seconds_left}s to add to this PDF, or tap Finish`;
  }
  $("#simbar").hidden = !st.simulate;
  markPreset(st.preset);
  renderDrive(st.drive);
  // Keep in step with changes made from another phone/computer.
  if (settings && (settings.mode !== st.mode || settings.scanner.paused !== st.paused)) {
    settings.mode = st.mode; settings.scanner.paused = st.paused; fillForm();
  }
  fillDevices(sc);
}

function fillDevices(sc) {
  const dl = $("#devlist");
  const key = sc.device || "";
  if (fillDevices._k === key) return; fillDevices._k = key;
  api("/api/scanner/devices").then((d) => {
    dl.innerHTML = d.devices.map((x) => `<option value="${esc(x.name)}">${esc(x.desc)}</option>`).join("");
  }).catch(() => {});
}

function renderDrive(d) {
  const badge = $("#driveBadge"); const body = $("#driveBody");
  const key = JSON.stringify([d, settings?.drive.pdf_folder, settings?.drive.photo_folder]);
  if (renderDrive._k === key) return; renderDrive._k = key;
  if (!d.configured) {
    badge.textContent = "Not set up"; badge.className = "badge warn";
    body.innerHTML = `<p class="small">Add your Google OAuth client (type <b>TVs and Limited Input devices</b>) under
      Settings → Google Drive. The README walks through creating one in about five minutes.</p>`;
  } else if (d.pending && d.pending.state === "waiting") {
    badge.textContent = "Signing in"; badge.className = "badge warn";
    body.innerHTML = `<ol class="steps small">
        <li>Open <a href="${esc(d.pending.verification_url)}" target="_blank" rel="noopener">${esc(d.pending.verification_url)}</a> on any device</li>
        <li>Enter this code and pick your Google account:</li></ol>
      <div class="code">${esc(d.pending.user_code)}</div>
      <div class="row between small muted"><span>Expires in ${Math.ceil(d.pending.expires_in / 60)} min</span>
      <button class="link" id="cancelAuth">Cancel</button></div>`;
    $("#cancelAuth").onclick = () => api("/api/drive/cancel", { method: "POST" }).then(refresh);
  } else if (d.connected) {
    badge.textContent = "Connected"; badge.className = "badge ok";
    body.innerHTML = `<p class="small">Signed in as <b>${esc(d.email || "your Google account")}</b>.
      PDFs go to <b>My Drive/${esc(settings?.drive.pdf_folder)}</b>, photos to <b>${esc(settings?.drive.photo_folder)}</b>.</p>
      <button class="link small" id="disc">Disconnect</button>`;
    $("#disc").onclick = () => confirm("Disconnect Google Drive?") &&
      api("/api/drive/disconnect", { method: "POST" }).then(refresh);
  } else {
    badge.textContent = "Not connected"; badge.className = "badge bad";
    const fail = d.pending && d.pending.state === "failed" ? `<p class="small" style="color:var(--bad)">${esc(d.pending.message)}</p>` : "";
    const errMsg = d.error ? `<p class="small" style="color:var(--bad)">${esc(d.error)}</p>` : "";
    body.innerHTML = `${fail}${errMsg}<p class="small">Scans are saved locally and will upload once you connect.</p>
      <button class="primary" id="conn">Connect Google account</button>`;
    $("#conn").onclick = () => api("/api/drive/connect", { method: "POST" })
      .then(refresh).catch((e) => toast(e.message));
  }
}

const STATUS_TXT = { pending: "Waiting to upload", uploading: "Uploading…", done: "Uploaded", error: "Upload failed", local: "Saved locally" };
function ago(t) {
  const s = Date.now() / 1000 - t;
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + " min ago";
  if (s < 86400) return Math.floor(s / 3600) + " h ago";
  return new Date(t * 1000).toLocaleDateString();
}
function size(b) { return b > 1e6 ? (b / 1e6).toFixed(1) + " MB" : Math.max(1, Math.round(b / 1e3)) + " KB"; }

async function loadItems() {
  const list = await api("/api/items?limit=40");
  const ul = $("#items");
  $("#retryErrors").hidden = !list.some((i) => i.status === "error");
  if (!list.length) return;
  ul.innerHTML = list.map((i) => {
    const ico = i.kind === "pdf" ? "PDF" : i.kind === "photo" ? "IMG" : "FILE";
    const st = STATUS_TXT[i.status] || i.status;
    const extra = i.status === "done" && i.drive_link ? ` · <a href="${esc(i.drive_link)}" target="_blank" rel="noopener">Open in Drive</a>`
      : i.status === "error" || i.status === "local" ? ` · <button class="link" data-retry="${i.id}">Upload</button>` : "";
    const err = i.error && i.status !== "done" ? `<div class="detail" title="${esc(i.error)}">${esc(i.error)}</div>` : "";
    return `<li><div class="ico">${ico}</div><div class="meta">
      <a class="name" href="/files/${i.id}" target="_blank">${esc(i.name)}</a>
      <div class="detail">${st}${extra} · ${i.pages ? i.pages + " p · " : ""}${i.size ? size(i.size) + " · " : ""}${i.source === "watch" ? "watch folder · " : ""}${ago(i.created)}</div>
      ${err}</div></li>`;
  }).join("");
  $$("[data-retry]", ul).forEach((b) => (b.onclick = () => act(`/api/items/${b.dataset.retry}/retry`, "Queued").then(loadItems)));
}

async function loadLogs() {
  if (!$("#logsBox").open) return;
  const lines = await api("/api/logs");
  const pre = $("#logs");
  const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
  pre.textContent = lines.map((l) => `${new Date(l.t * 1000).toLocaleTimeString()} ${l.level === "INFO" ? "" : l.level + " "}${l.msg}`).join("\n");
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}

async function refresh() {
  try {
    lastStatus = await api("/api/status");
    renderStatus(lastStatus);
  } catch (e) {
    $("#pill").textContent = "No connection"; $("#pill").className = "pill bad";
  }
}

let tick = 0;
async function loop() {
  await refresh();
  if (tick % 3 === 0) { loadItems().catch(() => {}); loadLogs().catch(() => {}); }
  tick++;
  setTimeout(loop, 2000);
}

(async () => {
  for (;;) {
    try { settings = await api("/api/settings"); break; }
    catch (e) { $("#msg").textContent = "Can't reach ScanHub. Retrying…"; await new Promise((r) => setTimeout(r, 3000)); }
  }
  fillForm();
  try { presets = await api("/api/presets"); renderPresets(); } catch (e) { /* page still works without them */ }
  loop();
})();
