# Job Watchers

Three independent job watchers that email you when a **new** role appears — each with its
own script, its own dedup store, and its own launcher. They never interfere with each
other, and you can run any of them (or all three).

| Watcher | Watches for | Docs |
|---|---|---|
| **`quant/`** | Quant/HFT **new-grad** SWE roles + recruiting **events** (29 firms) | [quant/README.md](quant/README.md) · [quant/COMMANDS.md](quant/COMMANDS.md) |
| **`tech/`** | Tech-company **mid-level** SWE roles (119 firms) | [tech/README.md](tech/README.md) |
| **`newgrad/`** | Tech-company **new-graduate** SWE roles (301 firms) | [newgrad/README.md](newgrad/README.md) |
| **`dist/`** | Packages the tech watcher into a zip to share with someone else | [dist/](dist/) |

`tech/` and `newgrad/` are deliberate opposites: the tech watcher *excludes* new-grad
titles, the new-grad watcher keeps **only** those.

```
.
├── run.sh                 # launcher for the quant watcher    (git-ignored — holds your password)
├── run_tech.sh            # launcher for the tech watcher     (git-ignored — holds your password)
├── run_newgrad.sh         # launcher for the new-grad watcher (git-ignored — holds your password)
├── push.sh                # safety-gated commit & push
├── requirements.txt
├── quant/                 # job_watcher.py + COMMANDS.md + seen_jobs.sqlite3
├── tech/                  # tech_watcher.py + install_agent.sh + seen_tech_jobs.sqlite3
├── newgrad/               # newgrad_watcher.py + install_agent.sh + seen_newgrad_jobs.sqlite3
└── dist/                  # build_zip.sh + the shareable package files
```

Each watcher stores its state **next to its own script** (`quant/seen_jobs.sqlite3`,
`tech/seen_tech_jobs.sqlite3`, `newgrad/seen_newgrad_jobs.sqlite3`), so wiping one never
affects the others.

---

## Setup

```bash
git clone https://github.com/KavinSankar123/SWE_Job_Scraper.git
cd SWE_Job_Scraper
./setup.sh
```

`setup.sh` builds the virtualenv, installs dependencies, self-tests all three watchers, and
creates your launchers (`run.sh`, `run_tech.sh`, `run_newgrad.sh`) from the `.example`
templates.

Then **add your email credentials** — open whichever launchers you plan to use and fill in
the three values at the top. Gmail needs an **App Password**
(<https://myaccount.google.com/apppasswords> — turn on 2-Step Verification first); your
normal password will not work.

```bash
export EMAIL_USER="you@gmail.com"                 # sends FROM here (owns the app password)
export EMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"   # the 16-char code — NO spaces around =
export EMAIL_TO="you@gmail.com"                   # where alerts are delivered
```

All three launchers are **git-ignored**, so your password is never committed, `git pull`
never overwrites it, and `push.sh` refuses to push if any of them ever gets staged.
Non-Gmail? also set `EMAIL_SMTP_HOST` / `EMAIL_SMTP_PORT` inside them (587 → STARTTLS,
465 → SSL).

Optional — only a fallback for the JS-rendered quant pages (Citadel, and the HRT events
page). Everything else works without it:

```bash
.venv/bin/pip install playwright && .venv/bin/playwright install chromium
```

## What's running in the background?

```bash
./status.sh
```

One place for the whole picture: which launchd agents are installed and loaded,
**which checkout each one actually runs**, any live `--interval` loop or nohup'd
process (and a warning if two are running for the same watcher), cron entries,
and when each watcher last did something.

A launchd agent is normally *not* a running process — it fires `--once` on a
timer and exits within seconds — so "loaded" is what you want to see, not
"running".

## Staying up to date

New companies get added upstream over time. To pick them up:

```bash
git pull
./run_tech.sh --once      # you'll be emailed the open roles at any newly-added company
./run_newgrad.sh --once   # same, for the new-grad watcher
```

If the new-grad watcher is running under launchd, you don't even need that second line —
each fire is a fresh process, so it picks the new companies up on its next run by itself.

### If you're running a background `--interval` loop

A long-running loop **auto-restarts itself** when `git pull` updates the script, so it
picks up newly-added companies on its next cycle:

```
tech_watcher.py changed on disk — restarting to pick up the new company list.
Watching 119 tech companies every 120 min. Ctrl+C to stop.
```

This matters because Python loads the company list **once, at process start**. Without
the auto-restart, a loop started *before* a pull would keep scraping the **old** list
forever — silently ignoring every newly-added company — until someone restarted it by
hand. Now you can just `git pull` and leave it running. (A pull that somehow breaks the
script won't kill the watcher either: it logs the error and keeps running the version it
already has.)

`setup.sh` is safe to re-run after a pull (do it if dependencies changed) — it **never**
overwrites an existing launcher and **never** touches your database. Concretely:

| | Tracked by git? | What `git pull` does to it |
|---|---|---|
| `quant/job_watcher.py`, `tech/tech_watcher.py`, `newgrad/newgrad_watcher.py` | yes | **updated** — new companies arrive here |
| `run.sh`, `run_tech.sh`, `run_newgrad.sh` (your password) | **no** — git-ignored | untouched |
| `seen_jobs.sqlite3`, `seen_tech_jobs.sqlite3`, `seen_newgrad_jobs.sqlite3` | **no** — git-ignored | untouched |

So pulling never re-emails you old roles and never clobbers your credentials.

## Run

Always run the launchers **from the repo root**:

```bash
# quant/HFT new-grad roles + recruiting events
./run.sh --list                 # what's being watched
./run.sh --once                 # one pass (first run seeds silently)
./run.sh --list-events          # recruiting events with direct register links

# tech mid-level SWE roles
./run_tech.sh --list            # the 119 companies
./run_tech.sh --preview         # every matching role — no email, no DB write
./run_tech.sh --once            # one pass (first run seeds silently)

# tech new-graduate SWE roles
./run_newgrad.sh --list         # the 301 companies
./run_newgrad.sh --preview      # every matching role — no email, no DB write
./run_newgrad.sh --once         # one pass (first run DOES email — see below)
```

⚠️ The quant and tech watchers **seed silently on the first run** so you aren't flooded
with hundreds of existing roles. Add `--notify-seed` if you *do* want that first batch
emailed. The **new-grad** watcher is the exception: its matching set is a few dozen and
time-sensitive, so the first run emails what's open right now (`--quiet-seed` opts out).

## Run it in the background, with no terminal open

The **tech** and **new-grad** watchers each ship a one-command macOS **LaunchAgent**
installer. Unlike `nohup ... &`, a LaunchAgent survives closing the terminal, logging out,
and rebooting — and a run missed while the laptop slept fires once on wake:

```bash
# tech mid-level SWE roles — every 3 hours by default
./tech/install_agent.sh install
./tech/install_agent.sh status           # loaded? last exit code? recent log lines
./tech/install_agent.sh run-now          # trigger a pass immediately
./tech/install_agent.sh uninstall

# tech new-graduate SWE roles — every 2 hours by default
./newgrad/install_agent.sh install
./newgrad/install_agent.sh status
./newgrad/install_agent.sh run-now
./newgrad/install_agent.sh uninstall
```

Both accept `--interval-hours N`, install to `~/Library/LaunchAgents/`
(`com.kavin.techwatcher.plist` / `com.kavin.newgradwatcher.plist`), and log to
`launchd.tech.out.log` / `launchd.newgrad.out.log` at the repo root. They are independent —
run one, the other, or both.

Each runs `--once` on a timer rather than holding an `--interval` loop open, so every fire
is a fresh process and a `git pull` that adds companies is picked up on the next run with
no restart.

Both **refuse to install** while the matching launcher still holds the placeholder
password (the agent would otherwise fail silently forever), and both refuse to install from
`~/Downloads`, `~/Documents` or `~/Desktop`, where macOS privacy protection stops a
LaunchAgent from executing files at all.

`state = not running` in `status` output is normal — these are periodic agents, not
daemons, so they are only live processes for a few minutes per cycle. **`last exit code = 0`**
plus recent log lines is what "healthy" looks like.

See [tech/README.md](tech/README.md) and [newgrad/README.md](newgrad/README.md) for details.
The **quant** watcher has no installer; `quant/COMMANDS.md` documents a hand-written
launchd plist and a cron line for it.

See [quant/README.md](quant/README.md), [tech/README.md](tech/README.md) and
[newgrad/README.md](newgrad/README.md) for the full reference.

## Sharing the tech watcher

To send the tech watcher to someone else, one command builds a clean zip **from the live
script** (so you can never hand out a stale copy), and refuses to build if a credential,
database, or log would end up inside:

```bash
./dist/build_zip.sh          # -> ~/Downloads/tech-job-watcher.zip
```

They unzip it, run `./setup.sh`, add their **own** Gmail app password, and go. Details in
[dist/README.md](dist/README.md).
