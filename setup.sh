#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# setup.sh — one-command setup after cloning (or after a `git pull`).
#
# Safe to re-run any time. It NEVER overwrites an existing launcher, so your
# email credentials survive every pull, and it never touches your database, so
# you are never re-emailed roles you have already seen.
#
#   git clone <repo> && cd SWE_Job_Scraper && ./setup.sh
# -----------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Checking Python..."
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found. Install Python 3.10+ and re-run ./setup.sh"
  echo "  macOS:  brew install python3   (or https://www.python.org/downloads/)"
  exit 1
fi
python3 --version

if [ -d .venv ]; then
  echo "==> Reusing existing virtual environment (.venv)"
else
  echo "==> Creating virtual environment (.venv)..."
  python3 -m venv .venv
fi

echo "==> Installing/updating dependencies..."
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt

# --- launchers: create from template ONLY if missing --------------------------
# This is what makes `git pull` safe: run.sh / run_tech.sh are git-ignored and are
# never regenerated once they exist, so your password is never clobbered.
created=""
for w in run_tech run; do
  if [ ! -f "$w.sh" ]; then
    cp "$w.sh.example" "$w.sh"
    chmod 700 "$w.sh"
    created="$created $w.sh"
  fi
done

echo "==> Verifying the install (offline self-test, no network)..."
.venv/bin/python tech/tech_watcher.py --selftest >/dev/null && echo "    tech watcher  OK"
.venv/bin/python quant/job_watcher.py --selftest >/dev/null && echo "    quant watcher OK"

echo
echo "============================================================"
if [ -n "$created" ]; then
  echo " SETUP COMPLETE — created:$created"
  echo
  echo " STEP 1 — Add your email credentials."
  echo "   Open run_tech.sh and fill in the three values at the top."
  echo "   Gmail needs an APP PASSWORD (not your normal password):"
  echo "       https://myaccount.google.com/apppasswords"
  echo "       (turn on 2-Step Verification first)"
else
  echo " SETUP COMPLETE — your existing launcher was left untouched."
fi
cat <<'EOM'

 STEP 2 — Try it. These send NO email:
     ./run_tech.sh --list       # the tech companies being watched
     ./run_tech.sh --preview    # every matching role open right now

 STEP 3 — Start watching. The FIRST run seeds silently so you aren't
          flooded; after that you're emailed only NEW roles:
     ./run_tech.sh --once             # one pass now
     ./run_tech.sh --interval 180     # keep checking every 3 hours

   Want that first batch emailed to you anyway?
     ./run_tech.sh --once --notify-seed

 TO GET NEW COMPANIES LATER:
     git pull        # picks up any companies added upstream
     ./run_tech.sh --once     # you'll be emailed their open roles

   Your credentials and your "already seen" database are git-ignored,
   so pulling never overwrites them.
============================================================

EOM
