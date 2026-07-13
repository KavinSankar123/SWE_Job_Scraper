# Job Watchers

Two independent job watchers that email you when a **new** role appears — each with its
own script, its own dedup store, and its own launcher. They never interfere with each
other, and you can run either (or both).

| Watcher | Watches for | Docs |
|---|---|---|
| **`quant/`** | Quant/HFT **new-grad** SWE roles + recruiting **events** (29 firms) | [quant/README.md](quant/README.md) · [quant/COMMANDS.md](quant/COMMANDS.md) |
| **`tech/`** | Tech-company **mid-level** SWE roles (46 firms) | [tech/README.md](tech/README.md) |
| **`dist/`** | Packages the tech watcher into a zip to share with someone else | [dist/](dist/) |

```
.
├── run.sh                 # launcher for the quant watcher   (git-ignored — holds your password)
├── run_tech.sh            # launcher for the tech watcher    (git-ignored — holds your password)
├── push.sh                # safety-gated commit & push
├── requirements.txt
├── quant/                 # job_watcher.py + COMMANDS.md + seen_jobs.sqlite3
├── tech/                  # tech_watcher.py + seen_tech_jobs.sqlite3
└── dist/                  # build_zip.sh + the shareable package files
```

Each watcher stores its state **next to its own script** (`quant/seen_jobs.sqlite3`,
`tech/seen_tech_jobs.sqlite3`), so wiping one never affects the other.

---

## 1. Install (once)

```bash
cd ~/Downloads/HFT_Job_Scraper
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Optional — only a fallback for the JS-rendered quant pages (Citadel, and the HRT events
page). Everything else works without it:

```bash
.venv/bin/pip install playwright && .venv/bin/playwright install chromium
```

## 2. Configure email (once)

Each watcher is driven by a private launcher at the repo root that holds your Gmail
credentials and runs the right script through the virtualenv. Gmail needs an
**App Password** (Google Account → Security → 2-Step Verification → App passwords) —
your normal password will not work.

```bash
# quant watcher
cat > run.sh <<'EOF'
#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
export EMAIL_USER="you@gmail.com"                 # sends FROM here (owns the app password)
export EMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"   # the 16-char code — NO spaces around =
export EMAIL_TO="you@gmail.com"                   # where alerts are delivered
exec .venv/bin/python quant/job_watcher.py "$@"
EOF
chmod 700 run.sh

# tech watcher — same three values, different script
sed 's|quant/job_watcher\.py|tech/tech_watcher.py|' run.sh > run_tech.sh
chmod 700 run_tech.sh
```

Both launchers are **git-ignored**, so your password is never committed, and `push.sh`
refuses to push if either one ever gets staged. Non-Gmail? also set `EMAIL_SMTP_HOST` /
`EMAIL_SMTP_PORT` inside them (587 → STARTTLS, 465 → SSL).

> Prefer no launcher? `export` those three variables in your shell and run
> `.venv/bin/python quant/job_watcher.py …` directly — the scripts read them from the
> environment.

## 3. Run

Always run the launchers **from the repo root**:

```bash
# quant/HFT new-grad roles + recruiting events
./run.sh --list                 # what's being watched
./run.sh --once                 # one pass (first run seeds silently)
./run.sh --list-events          # recruiting events with direct register links

# tech mid-level SWE roles
./run_tech.sh --list            # the 46 companies
./run_tech.sh --preview         # every matching role — no email, no DB write
./run_tech.sh --once            # one pass (first run seeds silently)
```

⚠️ Both watchers **seed silently on the first run** so you aren't flooded with hundreds
of existing roles. Add `--notify-seed` if you *do* want that first batch emailed.

See [quant/README.md](quant/README.md) and [tech/README.md](tech/README.md) for the full
reference.

## Sharing the tech watcher

To send the tech watcher to someone else, one command builds a clean zip **from the live
script** (so you can never hand out a stale copy), and refuses to build if a credential,
database, or log would end up inside:

```bash
./dist/build_zip.sh          # -> ~/Downloads/tech-job-watcher.zip
```

They unzip it, run `./setup.sh`, add their **own** Gmail app password, and go. Details in
[dist/README.md](dist/README.md).
