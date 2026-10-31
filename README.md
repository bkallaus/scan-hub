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

**Topic tags.** Under **Settings → Documents**, turn on **Tag topics with
Gist** to tag each PDF with one to three of 36 topics, such as *Personal
Finance*, *Health & Medicine* or *Law*, using [Gist](https://desertant.com/models/gist/)
from Desert Ant Labs. Gist runs on this machine and reads the title and the
start of the first page, in any of 101 languages. Use `{topic}` in the file
name, for example `{topic} - {title}`, to get
`Personal Finance - Acme Power Invoice.pdf`. If no heading is found, the top
topic becomes the `{title}` too. The topics also go to Drive (see below) and
help Claude pick a title.

- The first tagged scan downloads the 74 MB model from Hugging Face into
  `data/models/gist`. Later scans use that copy.
- Gist's SDK sends license requests to Desert Ant Labs
  (`events.desertant.com`). Your document text stays on this machine. Gist is
  free for most apps under the
  [Desert Ant Labs Source-Available License](https://license.desertant.com/).
- Gist's Node build runs on x86_64 and arm64 Linux.

**Titles, summaries and Drive details.** Under **Settings → Documents**, set
**Title and summary** to describe each PDF from its text (the OCR text, or the
first three pages if OCR is off):

- **On this machine** uses simple rules, the same as `{title}`, plus the
  document's date and its first sentences as a summary. Nothing is sent
  anywhere.
- **With Claude** sends the recognised *text* (not the images) to Anthropic's
  Claude API, which reads it and writes a much better title, for example
  `Acme Power Electric Bill October 2026`, and a one- or two-line summary of
  what matters ("$84.12 due October 24"). Put your API key from
  <https://console.anthropic.com> in **Settings → Claude**, or set
  `ANTHROPIC_API_KEY` in `docker-compose.yml`. Very long stacks send only the
  first 60,000 characters. If Claude can't be reached, ScanHub falls back to
  the local rules.

Use `{title}` in the file name to name the file with the title. When the PDF
is uploaded with topics or a summary, Google Drive also gets:

- **Description**: the summary, the type, who it's from, its date, the
  topics, the page count and a few keywords. It shows in Drive's details panel.
- **Properties** `title`, `docType`, `sender`, `documentDate` and `topics`
  (topic ids such as `finance,law`), which you can search for in the Drive API.
- **Searchable text**, so Drive search finds the words even when the PDF has
  no OCR text layer.
- **Created time**: when it was scanned, not when it was uploaded.

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
