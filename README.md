# FF-680W ScanHub

A Docker container for the Epson FastFoto FF-680W on your home network. It waits
for paper, scans it, turns documents into PDFs or photos into image files, and
uploads them to Google Drive. A web page on your LAN shows status and lets you
change settings from your phone.

```
 FF-680W ──Wi-Fi──► container ──► /data/scans (local copy)
 (paper in tray)    │  epsonscan2 SANE driver + scanimage
                    │  PDF (img2pdf, optional OCR) / JPEG-PNG-TIFF (auto-crop)
                    │  /data/watch  ◄── files you drop in (NAS share)
                    └─► Google Drive: Scans/PDF, Scans/Photos, Scans/Dropped
 Phone/PC ──► http://<docker-host>:8680  (settings, status, Google sign-in)
```

## What you need

- An x86_64 Linux host running Docker (NAS, mini PC, Unraid, etc.) on the same
  network as the scanner.
- The FF-680W joined to your Wi-Fi (2.4 GHz only) with a **fixed IP**. Reserve
  its address in your router's DHCP settings. The scanner's IP is shown by
  Epson's setup tools or in your router's client list.
- A free Google Cloud OAuth client (5 minutes, steps below).

## 1. Build and start

```bash
# edit SCANNER_IP (and TZ) in docker-compose.yml first
docker compose up -d --build
docker compose logs -f
```

Open `http://<docker-host-ip>:8680`. The header pill should go from
*Connecting* to *Ready* and show a device name like
`epsonscan2:FF-680W:…:network:192.168.1.50`.

The build needs Epson's Linux driver bundle (`epsonscan2` +
`epsonscan2-non-free-plugin`, which provides network scanning). Epson's CDN
usually refuses scripted downloads (HTTP 403), so download the **deb, 64bit
(x86_64)** bundle from <https://support.epson.net/linux/en/epsonscan2.php> in a
browser and put the `.tar.gz` in `vendor/` before building. The build falls
back to downloading it only when `vendor/` is empty. If apt complains about unmet dependencies for the
Epson packages, build on a newer Ubuntu:
`docker compose build --build-arg BASE_IMAGE=ubuntu:24.04`.

`SCANNER_IP` (and the Google client values) from `docker-compose.yml` are
applied at start-up whenever they change, so editing the compose file and
restarting works. Changes you make in the web page stick until you change the
compose value again.

Host networking (`network_mode: host`) is used because Epson's network plugin
does its own discovery and talks to the scanner directly. Docker Desktop on
Mac/Windows does not support it properly; use a Linux host.

On an Ubuntu server, check `uname -m` says `x86_64`, and if `ufw` is on, open
the web port: `sudo ufw allow 8680/tcp`. The container reaches the scanner
on its own; it only needs outgoing access to the scanner's IP.

Epson only ships an x86_64 driver, so the compose file pins
`platform: linux/amd64`. On an ARM machine (Apple Silicon, Raspberry Pi) the
image builds and runs under emulation: fine for testing, slow for OCR.

## 2. Connect Google Drive

ScanHub signs in with Google's "enter a code on another device" flow, so it
works on a plain LAN address with no domain or HTTPS. You create your own OAuth
client so your tokens never touch anyone else's app.

1. Go to <https://console.cloud.google.com/>, create a project (e.g. *ScanHub*).
2. **APIs & Services → Library**: enable **Google Drive API**.
3. **Google Auth Platform** (formerly "OAuth consent screen") → *Get started*:
   app name *ScanHub*, your email as support/contact, audience **External**.
4. **Data Access → Add or remove scopes**: add `.../auth/drive.file`, `openid`
   and `.../auth/userinfo.email`. These are all non-sensitive scopes.
5. **Audience → Publish app** (status *In production*). Important: apps left in
   *Testing* get refresh tokens that expire after 7 days, which would make you
   sign in again every week. Because `drive.file` is non-sensitive, publishing
   doesn't require Google verification. If Google ever shows an "unverified
   app" screen during sign-in, choose *Advanced → Go to ScanHub*.
6. **Clients → Create client** → application type **TVs and Limited Input
   devices** → create, then copy the **Client ID** and **Client secret**.
7. In the ScanHub web page: *Settings → Google Drive*, paste both, then tap
   **Connect Google account** in the Google Drive card. Open the link shown,
   type the code, pick your account and allow access.

The `drive.file` scope means ScanHub can only see files and folders it created.
It creates `My Drive/Scans/PDF`, `Scans/Photos` and `Scans/Dropped` (names are
editable). You can move or rename those folders in Drive later; ScanHub keeps
using them by ID. If you delete one, it is recreated on the next upload.

## 3. Using it

- **Pick a mode** at the top of the page: *Documents → PDF* or *Photos →
  image files*. The choice sticks until you change it.
- **Load paper.** ScanHub notices and scans the whole stack, then saves and
  uploads. *Scan now* works any time.
- **Documents:** duplex and blank-page removal are on by default. *Combine
  batches within N seconds* lets you feed a thick document in several loads and
  still get one PDF (a *Finish PDF* button appears while it waits). *Make PDFs
  searchable* runs OCR (adds time per page).
- **Photos:** each photo becomes its own file, auto-cropped from the black
  scanner background. With *Scan backs* on, a back side is kept as
  `…_b.jpg` only when it has writing on it.
- **Watch folder:** anything copied into `/data/watch` (map it to a NAS share in
  `docker-compose.yml`) is uploaded to `Scans/Dropped`, mirroring sub-folders,
  then moved to `/data/watch/_uploaded` (or deleted/left, your choice).
- If Drive isn't connected yet, or the internet is down, files wait locally and
  upload later with automatic retries. Failed items have an *Upload* button.

## How scans get started (read this if auto-scan misbehaves)

Epson's scan button normally works by waking Epson's Windows/Mac software over
the network. There is no official Linux equivalent, so ScanHub uses one of
these strategies (*Settings → Scanner → Start scans*):

| Setting | How it works |
|---|---|
| **Auto** (default) | Uses *sensor* if the driver exposes one, else *probe*. |
| **Sensor** | Polls a paper-loaded sensor or scan-button option from the driver. If the driver reports the button, pressing it starts a scan. |
| **Probe** | Asks the scanner to start a feed every few seconds. An empty tray answers "out of documents" instantly; a loaded tray just scans. |
| **Manual** | Only the *Scan now* button. |

The page shows which one is active (e.g. `start: probe`). *Show driver
options* lists everything the driver exposes, which is useful if you want to
add extra `scanimage` options such as `-y 356` for longer paper.

Probe mode keeps a light conversation going with the scanner. If you notice it
stops the scanner from sleeping, raise *Check every* or use *Pause auto-scan*
when you're done for the day.

## Troubleshooting

```bash
docker exec -it ff680w-scanhub scanimage -L          # does the driver see it?
docker exec -it ff680w-scanhub scanimage -d 'epsonscan2:networkscanner:esci2:network:192.168.1.50' -A
                                                      # open it directly by IP (use yours)
docker exec -it ff680w-scanhub scanimage -d '<device>' -A   # options + sensors
docker exec -it ff680w-scanhub cat /root/.epsonscan2/Network/epsonscan2.conf
```

- **"No scanner found":** check the IP, that the Wi-Fi light is solid, and that
  the scanner isn't asleep/off (press its power button). Make sure no other
  computer is mid-scan. Try *Reconnect scanner*.
- ScanHub first opens the scanner directly at `SCANNER_IP`, then falls back
  to a network search. If neither works but the direct command above does,
  paste that device name into *SANE device*.
- **Wrong device picked:** copy the right name from *Show driver options* into
  *SANE device*.
- **The scanner is also used by Epson FastFoto on a PC:** that works, but only one
  program can hold the scanner at a time; the page shows "Scanner busy".
- **Photos not cropped well:** turn off auto-crop and the full feed area is kept.
- **Scans interrupted by a restart or crash** are picked up from `/data/work`
  and converted on the next start. Batches that fail to convert are kept in
  `/data/work/failed_job_*` so nothing is lost.
- Logs: the *Log* section at the bottom of the page, or `docker compose logs`.

## Test without the scanner

Set `SIMULATE_SCANNER: "1"` in `docker-compose.yml`. A fake FF-680W appears and
a yellow bar on the page lets you "load" pages. Everything else (PDFs, photos,
Drive uploads, watch folder) runs for real.

On Docker Desktop (no host networking) use the dev override, which publishes
port 8680, turns on the simulator and keeps its data in `./data-dev`:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

Without the Epson bundle you can still build a simulator-only image:
`docker compose build --build-arg INSTALL_EPSON_DRIVER=false`.

## Tests

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest
```

## Data layout (`./data`)

```
config/settings.json      all settings (also editable from the page)
state/google_token.json   Google refresh token (chmod 600) — keep private
state/scanhub.db          history and upload queue
scans/YYYY-MM/            local copies of every scan
watch/                    drop folder; watch/_uploaded after upload
```

## Security

The page has no login by default; anyone on your LAN can change settings and
start scans. Set `WEB_PASSWORD` to require a password (any username).
Requests that change something are refused when they come from another
website (checked with the browser's `Origin` header), so a page you visit
can't start scans or disconnect Drive behind your back. The
Google permission only covers files ScanHub created; you can revoke it any time
at <https://myaccount.google.com/permissions> or with *Disconnect*.
