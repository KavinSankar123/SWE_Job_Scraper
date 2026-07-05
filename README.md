# Quant/HFT New-Grad Job Watcher

Checks a set of quant/HFT career pages on a schedule and emails you when a **new**
US-based new-grad software role appears. Only alerts once per posting (SQLite dedup
store). Location filtering is US-only by default (toggle with `US_ONLY`).

Day-to-day commands live in **[COMMANDS.md](COMMANDS.md)**; this file covers setup and
how the watcher works.

## 1. Install

```bash
cd ~/Downloads/HFT_Job_Scraper
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# Optional — only a fallback for Citadel/Citadel Securities if their AJAX endpoint
# ever changes (DRW works without it):
# .venv/bin/pip install playwright && .venv/bin/playwright install chromium
```

## 2. Configure email (Gmail)

Put your credentials in a private launcher `run.sh` that runs the tool through the
virtualenv. Gmail needs an **App Password** (Google Account → Security → 2-Step
Verification → App passwords), not your normal password.

```bash
cat > run.sh <<'EOF'
#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
export EMAIL_USER="you@gmail.com"                 # sends FROM here (owns the app password)
export EMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"   # the 16-char code — NO spaces around =
export EMAIL_TO="you@gmail.com"                   # where alerts are delivered
exec .venv/bin/python job_watcher.py "$@"
EOF
chmod 700 run.sh
```

`run.sh` is git-ignored, so your password is never committed. Non-Gmail? also set
`EMAIL_SMTP_HOST` / `EMAIL_SMTP_PORT` inside it (587 → STARTTLS, 465 → SSL).

> Prefer not to use `run.sh`? `export` those three variables in your shell and run
> `.venv/bin/python job_watcher.py …` directly — the tool reads them from the environment.

## 3. Run

```bash
./run.sh --list          # show what will be monitored
./run.sh --once          # single pass (first pass just seeds, no email)
./run.sh --interval 120  # loop forever, checking every 120 minutes
./run.sh --company DRW   # scrape ONE firm now for SWE roles (see below)
./run.sh --email-db      # email everything already stored (no scraping)
```

The **first run seeds silently** so you don't get flooded. From then on you're emailed
only about newly-posted roles. Use `--notify-seed` to email the first batch too. See
**[COMMANDS.md](COMMANDS.md)** for the full reference — stopping it, running in the
background, testing email, and managing the dedup store.

### Scrape a single company on demand (`--company`)

```bash
./run.sh --company DRW                  # name match ignores case/spaces/punctuation
./run.sh --company "Citadel Securities"
./run.sh --company deshaw
```

`--company` does one targeted pass over a single firm and intentionally differs from
the background watcher:
- it keeps **all software roles** (not just new-grad, but still US-only) — your "what
  SWE roles are open here right now?" button;
- it **emails only postings you haven't seen before**, and sends **nothing** when
  there's nothing new;
- postings are added to the dedup store **only after the email actually sends**, so a
  failed send is retried next time rather than silently lost.

So the first `./run.sh --company DRW` emails every current US SWE role at DRW, then only
newly-posted ones after that.

## Running it in the background

For always-on operation that survives logout/reboot (launchd or cron), see the
**"Optional: run it always-on in the background"** section of [COMMANDS.md](COMMANDS.md)
— those recipes call `run.sh`, so no password ever lives in a plist or crontab. On a
laptop, wrap the loop with `caffeinate -s ./run.sh --interval 120` so sleep doesn't
pause it.

## How each firm is reached

Most firms expose a clean data feed and work out of the box:
- **Greenhouse JSON API** — the majority: Radix, Hudson River Trading, Five Rings, Jump
  Trading, Flow Traders, Tower Research, Blackedge, Walleye (students board), Akuna,
  Point72, QRT/Qube, IMC, WorldQuant, Squarepoint, DV Trading, Schonfeld, AQR, Virtu,
  Old Mission, PDT Partners, Vatic Labs, Aquatic
- **Workday CXS JSON API** — Arrowstreet Capital (`Campus_Careers` site)
- **Custom JSON / embedded data** — D.E. Shaw (server HTML), DRW (Next.js `__NEXT_DATA__`),
  SIG (`careers.sig.com/api/jobs`, pre-filtered to New Graduates)
- **Avature HTML** — Two Sigma (server-rendered `JobDetail` links, paginated via `?jobOffset=`)
- **WordPress `admin-ajax.php`** — Citadel, Citadel Securities (the only JS-rendered ones)

If `--list`-style run logs show `Citadel scraped 0 roles`, dump the raw response and
inspect it:

```bash
./run.sh --once --debug     # writes debug_citadel.html etc.
```
Open the dumped file, find the job-card anchors, and either adjust the link hints for
that company in `COMPANIES` or switch its `"adapter"`. For Citadel, Chrome DevTools →
Network → filter `admin-ajax` shows the real request; the parser already handles common
JSON shapes.

## Tuning what counts as a match

Everything lives at the top of `job_watcher.py`:
- `SWE_TERMS` — a title/department must contain one of these.
- `NEWGRAD_TERMS` — required in `"filter"` mode.
- `INTERN_TERMS` + `EXCLUDE_INTERN` — interns are dropped by default; flip to include.
- `COMPANIES[].newgrad_mode` — `scoped` (source already new-grad), `filter`
  (require a new-grad hint), or `all_swe` (all software roles; firm doesn't label level).
- `US_ONLY` — `True` by default, so you're only alerted about US roles. A posting is
  kept if it has **no** location **or** lists **at least one** US location (city, state,
  or "United States"); it's dropped **only** when *every* listed location is recognisably
  outside the US. Set `US_ONLY = False` to alert regardless of location. If a city is
  mis-placed, add it to the `_US_CITIES` / `_FOREIGN` sets just below.

## Add another company

Append to `COMPANIES`. If it's Greenhouse-backed (check its apply URL for
`boards.greenhouse.io/<token>`), it's a one-liner:
```python
{"name": "Some Firm", "adapter": "greenhouse", "board_token": "<token>", "newgrad_mode": "filter"},
```
Workday is already supported (`"adapter": "workday"` with `wd_host` / `wd_tenant` /
`wd_site`). Lever and Ashby have public JSON feeds too — say the word and I'll add adapters.
