#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# build_zip.sh — build the shareable zip of the tech watcher for someone else.
#
# It always packages the CURRENT ../tech/tech_watcher.py, so you can never send a
# stale copy. It refuses to build if a real credential or any local state (your
# launcher, your database, your logs) would end up in the archive.
#
# Usage:
#   ./dist/build_zip.sh                 -> ~/Downloads/tech-job-watcher.zip
#   ./dist/build_zip.sh /path/out.zip   -> custom destination
# -----------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")"

OUT="${1:-$HOME/Downloads/tech-job-watcher.zip}"
TMP="$(mktemp -d)"
STAGE="$TMP/tech-job-watcher"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$STAGE"

# The live watcher + everything in package/ (which IS the zip's contents).
# Allowlist only — nothing is copied that we didn't name here, so local state
# (your launcher, database, logs) cannot leak in.
cp ../tech/tech_watcher.py "$STAGE/"
cp package/setup.sh package/run_tech.sh.example package/README.md package/requirements.txt "$STAGE/"
chmod +x "$STAGE/setup.sh"

# ---- safety gate ------------------------------------------------------------
fail=0
if grep -rh 'EMAIL_APP_PASSWORD="' "$STAGE" | grep -qv 'xxxx xxxx xxxx xxxx'; then
  echo "  !! a real-looking app password is in the package"; fail=1
fi
for bad in "run.sh" "run_tech.sh" "*.sqlite3" "*.log"; do
  if find "$STAGE" -name "$bad" -print -quit | grep -q .; then
    echo "  !! $bad must never be packaged"; fail=1
  fi
done
if [ "$fail" -ne 0 ]; then
  echo "ABORTED — nothing was written."; exit 1
fi

rm -f "$OUT"
(cd "$TMP" && zip -qr "$OUT" tech-job-watcher -x '*.DS_Store')

echo "Safety checks passed — no credentials, database, or logs included."
echo
echo "Built: $OUT"
unzip -l "$OUT" | sed -n '3,12p'
cat <<'EOM'

Send that file. Tell them:
  1. unzip it, then:  cd tech-job-watcher && ./setup.sh
  2. put their Gmail address + APP PASSWORD in run_tech.sh
  3. ./run_tech.sh --preview            # see what they'd get, no email
     ./run_tech.sh --once --notify-seed # scrape + email everything open now
EOM
