#!/usr/bin/env bash
# One-command setup. Run this once:   ./setup.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Checking Python..."
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found. Install Python 3.10+ and re-run ./setup.sh"
  echo "  macOS:  brew install python3     (or https://www.python.org/downloads/)"
  exit 1
fi
python3 --version

echo "==> Creating virtual environment (.venv)..."
python3 -m venv .venv

echo "==> Installing dependencies..."
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt

if [ ! -f run_tech.sh ]; then
  cp run_tech.sh.example run_tech.sh
  chmod 700 run_tech.sh
  echo "==> Created run_tech.sh from the template."
else
  echo "==> run_tech.sh already exists — leaving your settings alone."
fi

echo "==> Verifying the install (offline self-test, no network)..."
.venv/bin/python tech_watcher.py --selftest

cat <<'EOM'

============================================================
 SETUP COMPLETE
============================================================

STEP 1 — Add your email credentials.
  Open run_tech.sh and fill in the three values at the top.
  Gmail needs an APP PASSWORD (not your normal password):
      https://myaccount.google.com/apppasswords
      (turn on 2-Step Verification first)

STEP 2 — Try it. These send NO email:
      ./run_tech.sh --list       # the 46 companies being watched
      ./run_tech.sh --preview    # every matching role open right now

STEP 3 — Start watching. The FIRST run seeds silently so you
         aren't flooded; after that you're emailed only NEW roles:
      ./run_tech.sh --once             # one pass now
      ./run_tech.sh --interval 180     # keep checking every 3 hours

  Want that first batch emailed to you anyway?
      ./run_tech.sh --once --notify-seed

See README.md for everything else.
============================================================

EOM
