# Quant/HFT New-Grad Job Watcher

Checks a set of quant/HFT career pages on a schedule and emails you when a **new**
new-grad software role appears. Only alerts once per posting (SQLite dedup store).

## 1. Install

```bash
pip install -r requirements.txt
# Optional: a fallback for Citadel/Citadel Securities only, if their AJAX endpoint
# ever changes. DRW no longer needs it — it now works without a browser.
pip install playwright && playwright install chromium
```

## 2. Configure email (Gmail example)

Gmail blocks your normal password over SMTP — create an **App Password**
(Google Account → Security → 2-Step Verification → App passwords), then export:

```bash
export EMAIL_USER="kavin.sankar@gmail.com"
export EMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"   # the 16-char app password
export EMAIL_TO="kavin.sankar@gmail.com"              # optional; defaults to EMAIL_USER
# Non-Gmail? also set EMAIL_SMTP_HOST / EMAIL_SMTP_PORT (587 => STARTTLS, 465 => SSL)
```

## 3. Run

```bash
python job_watcher.py --list          # show what will be monitored
python job_watcher.py --once          # single pass (first pass just seeds, no email)
python job_watcher.py --interval 120  # loop forever, checking every 120 minutes
python job_watcher.py --company DRW    # scrape ONE firm now for SWE roles (see below)
python job_watcher.py --email-db      # email everything already stored (no scraping)
```

The **first run seeds silently** so you don't get flooded. From then on you're
emailed only about newly-posted roles. Use `--notify-seed` to email the first batch too.

### Scrape a single company on demand (`--company`)

```bash
python job_watcher.py --company DRW                  # name match ignores case/spaces/punctuation
python job_watcher.py --company "Citadel Securities"
python job_watcher.py --company deshaw
```

`--company` does one targeted pass over a single firm and intentionally differs from
the background watcher:
- it keeps **all software roles** (not just new-grad) — your "what SWE roles are open
  here right now?" button;
- it **emails only postings you haven't seen before**, and sends **nothing** when
  there's nothing new;
- postings are added to the dedup store **only after the email actually sends**, so a
  failed send is retried next time rather than silently lost.

So the first `--company DRW` emails every current SWE role at DRW (~80), then only
newly-posted ones after that.

## Running it as a real background job (macOS)

**Option A — cron** (simple; runs every 2 hours):
```
crontab -e
0 */2 * * * cd /path/to/folder && /usr/bin/python3 job_watcher.py --once >> cron.log 2>&1
```

**Option B — launchd** (survives logout; keeps the loop alive). Save as
`~/Library/LaunchAgents/com.shan.jobwatcher.plist`, edit the paths, then
`launchctl load ~/Library/LaunchAgents/com.shan.jobwatcher.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.shan.jobwatcher</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/ABSOLUTE/PATH/job_watcher.py</string>
    <string>--interval</string><string>120</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>EMAIL_USER</key><string>kavin.sankar@gmail.com</string>
    <key>EMAIL_APP_PASSWORD</key><string>xxxxxxxxxxxxxxxx</string>
    <key>EMAIL_TO</key><string>kavin.sankar@gmail.com</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/ABSOLUTE/PATH/launchd.out.log</string>
  <key>StandardErrorPath</key><string>/ABSOLUTE/PATH/launchd.err.log</string>
</dict></plist>
```
Tip: if you use the internal `--interval` loop on a laptop, wrap it with
`caffeinate -s python job_watcher.py --interval 120` so sleep doesn't pause it.

## How each firm is reached

Most firms expose a clean data feed and work out of the box:
- **Greenhouse JSON API** — Radix, Hudson River Trading, Five Rings, Jump Trading, Flow
  Traders, Tower Research, Blackedge, Walleye (students board), Akuna, Point72
- **Workday CXS JSON API** — Arrowstreet Capital (`Campus_Careers` site)
- **Custom JSON / embedded data** — D.E. Shaw (server HTML), DRW (Next.js `__NEXT_DATA__`),
  SIG (`careers.sig.com/api/jobs`, pre-filtered to New Graduates)
- **Avature HTML** — Two Sigma (server-rendered `JobDetail` links, paginated via `?jobOffset=`)
- **WordPress `admin-ajax.php`** — Citadel, Citadel Securities (the only JS-rendered ones)

If `--list`-style run logs show `Citadel scraped 0 roles`, do this:

```bash
python job_watcher.py --once --debug     # writes debug_citadel.html etc.
```
Open the dumped HTML, find the anchor tags for the job cards, and either:
- adjust the `link_regex` / link hints for that company in `COMPANIES`, or
- switch that company's `"adapter"` to `"playwright"` (and add `url` + `link_regex`).

For Citadel specifically, opening the careers page in Chrome DevTools → Network →
filtering `admin-ajax` shows the real request; if it returns JSON, the parser
already handles common shapes. If Citadel's cards link to `boards.greenhouse.io`,
tell me the board token and I'll switch it to the clean Greenhouse adapter too.

## Tuning what counts as a match

Everything lives at the top of `job_watcher.py`:
- `SWE_TERMS` — a title/department must contain one of these.
- `NEWGRAD_TERMS` — required in `"filter"` mode (HRT, DRW).
- `INTERN_TERMS` + `EXCLUDE_INTERN` — interns are dropped by default; flip to include.
- `COMPANIES[].newgrad_mode` — `scoped` (source already new-grad), `filter`
  (require a new-grad hint), or `all_swe` (all software roles; firm doesn't label level).

## Add another company

Append to `COMPANIES`. If it's Greenhouse-backed (check its apply URL for
`boards.greenhouse.io/<token>`), it's a one-liner:
```python
{"name": "Some Firm", "adapter": "greenhouse", "board_token": "<token>", "newgrad_mode": "filter"},
```
Lever, Ashby, and Workday also have public JSON feeds — say the word and I'll add adapters.
