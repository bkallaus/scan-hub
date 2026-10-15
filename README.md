# FF-680W ScanHub

Scan from an Epson FastFoto FF-680W straight to Google Drive. Load paper and
ScanHub scans it, makes a PDF (documents) or image files (photos), and uploads
them. A web page on your home network shows status and settings.

## What you need

- A Linux machine on your network with Docker, on an **x86_64** CPU (Epson
  only makes an x86_64 driver).
- The FF-680W connected to your Wi-Fi.
- A Google account.

## Setup

**1. Get Epson's driver.** Download the **deb, 64bit (x86_64)** bundle from
<https://support.epson.net/linux/en/epsonscan2.php> and put the `.tar.gz` file
in the `vendor/` folder.

The driver's network plugin is covered by Epson's own license, which doesn't
allow sharing it. That's why this repo doesn't include it, and why you
shouldn't publish the Docker image you build.

**2. Edit `docker-compose.yml`.** Set your time zone (`TZ`) and a password:

```yaml
      TZ: America/Chicago
      WEB_PASSWORD: pick-something
```

Without `WEB_PASSWORD`, anyone on your network can open the page and see your
scans.

**3. Start it.**

```bash
docker compose up -d --build
```

Open `http://<your-machine-ip>:8680`. Log in with any username and your
password. After a moment the status should say **Ready**.

**4. Connect Google Drive.** ScanHub uses your own Google sign-in app, so
create one (about 5 minutes):

1. Go to <https://console.cloud.google.com/> and create a project.
2. **APIs & Services → Library:** enable **Google Drive API**.
3. **Google Auth Platform → Get started:** name it *ScanHub*, enter your email,
   choose **External**.
4. **Data Access → Add or remove scopes:** add `.../auth/drive.file`, `openid`
   and `.../auth/userinfo.email`.
5. **Audience → Publish app.** If you skip this, Google signs you out every
   7 days.
6. **Clients → Create client:** type **TVs and Limited Input devices**. Copy
   the Client ID and Client secret.
7. In ScanHub, open **Settings → Google Drive**, paste both, then tap
   **Connect Google account** and follow the instructions.

If Google warns that the app is unverified, choose **Advanced → Go to
ScanHub**. It's your own app.

ScanHub can only see files it created. Scans go to `My Drive/Scans/PDF` and
`My Drive/Scans/Photos`.

## Scanning

1. On the web page, choose **Documents** or **Photos**, or tap a preset.
2. Put paper in the scanner. ScanHub notices and scans the whole stack.
3. The file shows up in the list on the page and uploads to Drive.

You can also tap **Scan now**. If the internet or Drive is down, scans are
kept locally and upload later.

**Name documents by what's on them.** Under **Settings → Documents**, set the
file name to something like `{title}_{date}`. ScanHub reads the first page on
this machine (nothing is sent anywhere) and uses its heading or sender, for
example `Acme Power and Light Invoice_2026-10-15.pdf`. If it can't find any
text, `{title}` becomes `Scan`. For other languages, build with
`docker compose build --build-arg OCR_LANGS="eng spa"` and set
**OCR language** to `eng+spa`.

## If something goes wrong

- **"No scanner found":** make sure the scanner is on and awake (press its
  power button), then tap **Reconnect scanner**. Setting the scanner's IP
  address under **Settings → Scanner** also helps.
- **"Scanner busy":** another computer is using it. Wait and try again.
- **Logs:** open **Log** at the bottom of the page, or run
  `docker compose logs -f`.

## Your data

Everything is stored in `./data` next to `docker-compose.yml`:

```
config/settings.json      settings
state/google_token.json   Google sign-in (keep private)
scans/YYYY-MM/            local copies of your scans
```

To disconnect Google, use **Disconnect** on the page or
<https://myaccount.google.com/permissions>.

## Trying it without a scanner

Set `SIMULATE_SCANNER: "1"` in `docker-compose.yml`. A fake scanner appears and
you can "load" pages from the page. On a Mac or Windows machine, use:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

Run the tests with:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest
```
