#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# install_agent.sh — run the new-grad watcher in the background, with no terminal
# open, using macOS launchd.
#
# Unlike `nohup ... &`, a LaunchAgent survives closing the terminal, logging out,
# and rebooting. A run missed while the laptop was asleep fires once on wake.
#
#   ./newgrad/install_agent.sh install [--interval-hours N]   # default 2
#   ./newgrad/install_agent.sh status
#   ./newgrad/install_agent.sh uninstall
#   ./newgrad/install_agent.sh run-now      # trigger one pass immediately
# -----------------------------------------------------------------------------
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.kavin.newgradwatcher"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
TEMPLATE="$REPO/newgrad/com.kavin.newgradwatcher.plist.example"
LAUNCHER="$REPO/run_newgrad.sh"
DOMAIN="gui/$(id -u)"

usage() { awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$0"; exit "${1:-0}"; }

cmd="${1:-}"; shift || true

interval_hours=2
while [ $# -gt 0 ]; do
  case "$1" in
    --interval-hours) interval_hours="${2:?--interval-hours needs a number}"; shift 2;;
    -h|--help) usage 0;;
    *) echo "Unknown option: $1"; usage 1;;
  esac
done

case "$cmd" in
install)
  # --- refuse to install a watcher that can't send mail --------------------- #
  # Without this the agent would fail silently every couple of hours forever.
  if [ ! -f "$LAUNCHER" ]; then
    echo "ERROR: $LAUNCHER does not exist."
    echo "       Run ./setup.sh first, then put your Gmail app password in it."
    exit 1
  fi
  if grep -q 'xxxx xxxx xxxx xxxx' "$LAUNCHER"; then
    echo "ERROR: run_newgrad.sh still has the placeholder app password."
    echo "       Open it and fill in EMAIL_USER / EMAIL_APP_PASSWORD / EMAIL_TO,"
    echo "       then re-run this command. (Gmail needs an App Password:"
    echo "       https://myaccount.google.com/apppasswords )"
    exit 1
  fi
  if ! [ "$interval_hours" -gt 0 ] 2>/dev/null; then
    echo "ERROR: --interval-hours must be a positive whole number."; exit 1
  fi

  # --- refuse to install from a macOS TCC-protected folder ------------------- #
  # A LaunchAgent has no privacy grant, so it CANNOT open pre-existing files in
  # ~/Downloads, ~/Documents or ~/Desktop. Your Terminal can (it has been granted
  # access), so the launcher runs fine by hand and then fails from launchd with
  # "Operation not permitted" / exit code 126 — silently, on every fire, forever.
  # This bit us for real on 2026-08-31 with the repo in ~/Downloads.
  case "$REPO/" in
    "$HOME"/Downloads/*|"$HOME"/Documents/*|"$HOME"/Desktop/*)
      echo "ERROR: this repo lives in a macOS privacy-protected folder:"
      echo "         $REPO"
      echo
      echo "       A LaunchAgent cannot execute files in ~/Downloads, ~/Documents"
      echo "       or ~/Desktop. It would install fine and then fail every run with"
      echo "       'Operation not permitted' (exit 126), writing nothing to the log."
      echo
      echo "       Move the repo somewhere unprotected, rebuild the venv, reinstall:"
      echo "         mv \"$REPO\" ~/Projects/\$(basename \"$REPO\")"
      echo "         cd ~/Projects/\$(basename \"$REPO\") && rm -rf .venv && ./setup.sh"
      echo "         ./newgrad/install_agent.sh install --interval-hours $interval_hours"
      echo
      echo "       (Granting Full Disk Access to /bin/bash would also work, but it"
      echo "        weakens every script on the machine — moving the repo is safer.)"
      exit 1;;
  esac

  mkdir -p "$HOME/Library/LaunchAgents"
  sed -e "s#__REPO__#$REPO#g" \
      -e "s#__INTERVAL__#$((interval_hours * 3600))#g" \
      "$TEMPLATE" > "$PLIST"
  chmod 644 "$PLIST"

  # Replace any previous copy. bootout on a not-loaded agent is not an error here.
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  if ! launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null; then
    launchctl load "$PLIST"          # older macOS
  fi

  echo "Installed $LABEL — checking every $interval_hours hour(s)."
  echo "  plist : $PLIST"
  echo "  logs  : $REPO/launchd.newgrad.out.log"
  echo
  echo "It runs once now (RunAtLoad), then every $interval_hours hour(s) — including"
  echo "after you close this terminal, log out, or reboot."
  echo "Watch the first run with:  tail -f $REPO/launchd.newgrad.out.log"
  ;;

uninstall)
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Uninstalled $LABEL. Your database and credentials were left untouched."
  ;;

status)
  if [ ! -f "$PLIST" ]; then
    echo "NOT INSTALLED — no $PLIST"
    echo "Install it with: ./newgrad/install_agent.sh install"
    exit 0
  fi
  echo "plist: $PLIST"
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    echo "state: LOADED"
    launchctl print "$DOMAIN/$LABEL" \
      | grep -E '^\s*(state|last exit code|run interval) ' | sed 's/^[[:space:]]*/  /' || true
  else
    echo "state: NOT LOADED (plist exists but launchd isn't running it)"
  fi
  echo
  echo "Recent log lines:"
  tail -n 5 "$REPO/launchd.newgrad.out.log" 2>/dev/null | sed 's/^/  /' || echo "  (no log yet)"
  ;;

run-now)
  launchctl kickstart -k "$DOMAIN/$LABEL"
  echo "Triggered one pass. Follow it with:"
  echo "  tail -f $REPO/launchd.newgrad.out.log"
  ;;

""|-h|--help) usage 0;;
*) echo "Unknown command: $cmd"; usage 1;;
esac
