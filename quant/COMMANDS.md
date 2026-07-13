# HFT Job Watcher — Command Reference

Every command assumes you're **inside the project folder first**:

```bash
cd ~/Downloads/HFT_Job_Scraper
```

`run.sh` already holds your email settings and uses the project's virtualenv, so you
never need to activate anything or retype the password.

---

## ⚡ The three you'll use most

```bash
./run.sh --list            # show the firms being watched
./run.sh --once            # check once now, email any NEW roles
./run.sh --interval 120    # keep checking every 120 min (Ctrl+C to stop)
```

---

## Running it

| Command | What it does |
|---|---|
| `./run.sh --list` | Print the monitored firms and exit (no network) |
| `./run.sh --once` | One pass now; emails newly-posted roles only |
| `./run.sh --interval 120` | Loop forever, checking every 120 minutes (stays in the terminal) |
| `caffeinate -s ./run.sh --interval 120` | Same, but stops your Mac from sleeping mid-loop (good for laptops) |

**Run it detached** (close the terminal, it keeps going):

```bash
nohup ./run.sh --interval 120 >> watcher.out 2>&1 &
```

---

## Scrape one company on demand

Check a single firm right now for **all** its software roles (not just new-grad).
Emails only postings you haven't seen before; sends nothing if there's nothing new.

```bash
./run.sh --company DRW                   # name is case/space/punctuation-insensitive
./run.sh --company "Citadel Securities"
./run.sh --company deshaw
./run.sh --company DRW --debug            # also dump the raw page for troubleshooting
```

Run `./run.sh --list` for the full roster (29 firms). Names match
case/space/punctuation-insensitively — e.g. `imc`, `aqr`, `two sigma`, `deshaw`,
`qube`. The first run for a firm emails everything currently open there, then only
new postings after that.

---

## Events (recruiting events, not jobs)

Detects **events** — info sessions, networking nights, trading challenges, tech
talks, women-in-trading days — on the firms' Greenhouse boards and emails you the
**direct registration link**. No relevance/US/date filtering: if it's classified as
an event, you get it (foreign events included).

```bash
./run.sh --list-events          # show events detected right now (no email, no store)
./run.sh --events-once          # one pass; emails any NEW events, then exits
./run.sh --events-interval 120  # loop, checking for new events every 120 min
```

The **first** `--events-once` emails every event currently open (no silent seeding —
the set is small and time-sensitive); after that only newly-posted events. Events are
deduped in the same `quant/seen_jobs.sqlite3` file under a separate `seen_events` table, so
the jobs watcher is completely unaffected. Run events on their own cron line:

```
30 */2 * * * /Users/kavinsankar/Downloads/HFT_Job_Scraper/run.sh --events-once >> /Users/kavinsankar/Downloads/HFT_Job_Scraper/cron.log 2>&1
```

> **Coverage note:** Events come from two kinds of source. **Greenhouse boards** (the 22
> already watched) are low-yield — events sit mixed into the job board, detected by title.
> **Jane Street's** `programs-and-events` page is fully covered (every entry is a real
> program/event — AMP, INSIGHT, WiSE, QTC, JSIP, …). **JS-rendered firm pages** (Hudson
> River Trading wired; Citadel and Two Sigma are candidates) need the optional Playwright
> renderer:
>
> ```bash
> .venv/bin/pip install playwright && .venv/bin/playwright install chromium
> ./run.sh --list-events --debug   # dumps debug_<firm>.html so you can tune its link pattern
> ```
>
> Then adjust that firm's `link_regex` in `EVENT_SOURCES` (top of `job_watcher.py`) to
> match its event-card anchors.

---

## Stopping it

```bash
# Running in your terminal (foreground):  press
Ctrl + C

# Started detached / with nohup:
pkill -f job_watcher.py
```

---

## Checking on it

```bash
pgrep -fl job_watcher.py     # is it running? (prints the process, or nothing)
tail -f quant/job_watcher.log      # watch it work live (Ctrl+C stops watching, not the job)
```

---

## Testing & verifying

```bash
# Offline parser self-test — proves the matching logic works, no network
./run.sh --selftest

# Verify the Gmail app password authenticates (logs in only — sends nothing)
.venv/bin/python -c "
import re, smtplib, pathlib
t = pathlib.Path('run.sh').read_text()
user = re.search(r'EMAIL_USER=\"([^\"]+)\"', t).group(1)
pw   = re.search(r'EMAIL_APP_PASSWORD=\"([^\"]+)\"', t).group(1)
s = smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=30); s.login(user, pw); s.quit()
print('LOGIN OK for', user)
"

# Send ONE test email to confirm delivery (check Spam the first time)
.venv/bin/python -c "
import re, smtplib, pathlib
from email.message import EmailMessage
t = pathlib.Path('run.sh').read_text()
g = lambda k: re.search(k+r'=\"([^\"]+)\"', t).group(1)
user, pw, to = g('EMAIL_USER'), g('EMAIL_APP_PASSWORD'), g('EMAIL_TO')
m = EmailMessage(); m['Subject']='[Jobs] test — watcher email is working'
m['From']=user; m['To']=to
m.set_content('If you can read this, the HFT job watcher can email you.')
s = smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=30); s.login(user, pw); s.send_message(m); s.quit()
print('Sent test email to', to)
"

# Force a full email of ALL current roles right now (fresh start, then back to new-only)
rm -f quant/seen_jobs.sqlite3 && ./run.sh --once --notify-seed
```

---

## Changing the sender email / app password

Generate a new app password **on the sending account** at
<https://myaccount.google.com/apppasswords> (needs 2-Step Verification on), then
rewrite `run.sh` with your three values:

```bash
cat > run.sh <<'EOF'
#!/usr/bin/env bash
# Private launcher — holds your Gmail App Password. DO NOT commit or share.
cd "$(dirname "$0")" || exit 1

export EMAIL_USER="SENDER_EMAIL@gmail.com"        # sends FROM here (owns the app password)
export EMAIL_APP_PASSWORD="aaaa bbbb cccc dddd"   # the 16-char code — NO space after the =
export EMAIL_TO="kavin.sankar@gmail.com"          # alerts land here

exec .venv/bin/python quant/job_watcher.py "$@"
EOF
chmod 700 run.sh
```

> ⚠️ In a shell script the `=` must have **no spaces around it**. `VAR= "x"` sets the
> variable to empty. After editing, re-run the "verify" login test above.

---

## The job database (dedup store)

The file `quant/seen_jobs.sqlite3` remembers what you've already been emailed.

```bash
# Email a snapshot of EVERYTHING currently in the store (no scraping, store unchanged)
./run.sh --email-db

# How many roles are tracked
sqlite3 quant/seen_jobs.sqlite3 "SELECT COUNT(*) FROM seen;"

# List everything currently tracked
sqlite3 quant/seen_jobs.sqlite3 "SELECT company, title FROM seen ORDER BY company;"

# Events live in the same file under a separate table
sqlite3 quant/seen_jobs.sqlite3 "SELECT company, title FROM seen_events ORDER BY company;"

# Wipe it and start fresh (next run re-seeds jobs; next --events-once re-emails events)
rm -f quant/seen_jobs.sqlite3
```

---

## Fixing a site that returns 0 roles

```bash
./run.sh --once --debug      # dumps quant/debug_<company>.html for the JS/AJAX sites
```

Every firm works out of the box (DRW included — it no longer needs a browser).
Playwright is only a fallback for Citadel/Citadel Securities if their AJAX endpoint
ever changes — optional, ~150 MB:

```bash
.venv/bin/pip install playwright && .venv/bin/playwright install chromium
```

---

## First-time setup on a fresh machine

```bash
cd ~/Downloads/HFT_Job_Scraper
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# then recreate run.sh (see "Changing the sender email" above) and chmod 700 run.sh
```

---

## Optional: run it always-on in the background

**launchd** (survives logout/reboot). Save as
`~/Library/LaunchAgents/com.kavin.jobwatcher.plist` — note it just calls `run.sh`,
so no password lives in this file:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.kavin.jobwatcher</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/kavinsankar/Downloads/HFT_Job_Scraper/run.sh</string>
    <string>--interval</string><string>120</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/kavinsankar/Downloads/HFT_Job_Scraper/launchd.out.log</string>
  <key>StandardErrorPath</key><string>/Users/kavinsankar/Downloads/HFT_Job_Scraper/launchd.err.log</string>
</dict></plist>
```

```bash
launchctl load   ~/Library/LaunchAgents/com.kavin.jobwatcher.plist   # start (and enable at login)
launchctl unload ~/Library/LaunchAgents/com.kavin.jobwatcher.plist   # stop
```

**cron** (simpler; runs one pass every 2 hours). Run `crontab -e` and add:

```
0 */2 * * * /Users/kavinsankar/Downloads/HFT_Job_Scraper/run.sh --once >> /Users/kavinsankar/Downloads/HFT_Job_Scraper/cron.log 2>&1
```


**Remove all saved jobs in the database and email a fresh list of postings**
```bash
rm -f quant/seen_jobs.sqlite3 && ./run.sh --once --notify-seed
```
