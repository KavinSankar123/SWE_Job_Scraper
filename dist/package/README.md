# Tech Job Watcher — mid-level SWE roles

Checks **46 tech companies** on a schedule and emails you when a **new mid-level
software engineering role** is posted. You're only emailed once per posting.

Companies are reached through their public job-board APIs (Greenhouse, Lever, Ashby) —
no scraping, no browser, nothing to log into.

---

## Setup (one time, ~2 minutes)

Requires **Python 3.10+** and a **Gmail** account.

```bash
cd tech-job-watcher
./setup.sh
```

That creates a virtual environment, installs dependencies, runs a self-test, and
creates `run_tech.sh` for you.

### Then add your email credentials

Open **`run_tech.sh`** and fill in the three values at the top:

```bash
export EMAIL_USER="you@gmail.com"                 # sends FROM here
export EMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"   # 16-char App Password (see below)
export EMAIL_TO="you@gmail.com"                   # where alerts land
```

> **Gmail requires an "App Password"** — your normal password will NOT work.
> 1. Turn on 2-Step Verification: <https://myaccount.google.com/security>
> 2. Create an app password: <https://myaccount.google.com/apppasswords>
> 3. Paste the 16-character code into `EMAIL_APP_PASSWORD`.
>
> `run_tech.sh` holds a real password — **don't share or commit it.**

---

## Running it

```bash
./run_tech.sh --list        # the 46 companies being watched
./run_tech.sh --preview     # print every matching role open right now (NO email)
./run_tech.sh --once        # one pass; emails newly-posted roles
./run_tech.sh --interval 180    # loop forever, checking every 180 min (Ctrl+C to stop)
```

**Start with `--preview`** — it shows exactly what you'd be emailed, without sending
anything.

> ⚠️ The **first** `--once` **seeds silently** (no email) so you don't get flooded with
> hundreds of existing roles. From then on you're emailed only about *new* postings.
> Want that first batch emailed anyway? `./run_tech.sh --once --notify-seed`

### Other useful commands

```bash
./run_tech.sh --company Stripe     # scrape ONE company now, email anything new
./run_tech.sh --email-db           # email everything already in the database (no scraping)
./run_tech.sh --selftest           # offline tests, no network
```

### Keep it running in the background

```bash
nohup ./run_tech.sh --interval 180 >> tech_nohup.log 2>&1 &   # detached
pgrep -fl tech_watcher.py                                     # is it running?
tail -f tech_watcher.log                                      # watch it live
pkill -f tech_watcher.py                                      # stop it
```

On a laptop, wrap it so sleep doesn't pause it:
`caffeinate -s ./run_tech.sh --interval 180`

---

## What counts as a "mid-level" role

Deliberately **broad** — a software-engineering title that is:
- **not** senior-tier (senior / staff / principal / lead / manager / director / …), and
- **not** entry-level (intern / new-grad / junior / …).

An unlevelled `Software Engineer` counts, as does `Software Engineer II / III`.
**US-only** by default.

Tuning is all at the top of `tech_watcher.py`:
- `SWE_RE` — what looks like a software role
- `SENIOR_RE` / `ENTRY_RE` — what's excluded above and below mid-level
- `US_ONLY` — set to `False` to get roles anywhere
- `COMPANIES` — add a company; find its token in its careers URL
  (`boards.greenhouse.io/<token>`, `jobs.lever.co/<token>`, `jobs.ashbyhq.com/<token>`)

---

## The database

`seen_tech_jobs.sqlite3` remembers what you've already been emailed, so you never get
the same posting twice.

```bash
sqlite3 seen_tech_jobs.sqlite3 "SELECT COUNT(*) FROM seen_tech;"   # how many tracked
rm -f seen_tech_jobs.sqlite3                                        # wipe & start over
```

---

## Troubleshooting

**No email arrives.** Check spam first. Then verify the app password authenticates:
```bash
.venv/bin/python -c "
import smtplib, os, re, pathlib
t = pathlib.Path('run_tech.sh').read_text()
g = lambda k: re.search(k + r'=\"([^\"]+)\"', t).group(1)
s = smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=30)
s.login(g('EMAIL_USER'), g('EMAIL_APP_PASSWORD')); s.quit()
print('LOGIN OK')
"
```
If that fails, the app password is wrong (or 2-Step Verification isn't on).

**"Email not sent — set EMAIL_USER..."** — you haven't filled in `run_tech.sh` yet.

**A company shows 0 roles.** Its board token probably changed. Check the company's
careers page URL and update its entry in `COMPANIES`.

**`./setup.sh: Permission denied`** — run `chmod +x setup.sh` first.
