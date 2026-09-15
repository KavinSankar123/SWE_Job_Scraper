#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# status.sh — what, if anything, is watching for jobs on this machine.
#
# There are three watchers and two ways to run one, so "is it running?" has no
# single answer. This reports all of them in one place:
#
#   * launchd agents   — the background installers (tech, new-grad). A launchd
#                        job is usually NOT a running process: it fires --once on
#                        a timer and exits in seconds, so "loaded" is what you
#                        want to see, not "running".
#   * live processes   — a nohup'd or terminal `--interval` loop, which DOES stay
#                        resident. This is also where a stray duplicate shows up.
#   * cron             — in case anything was scheduled that way.
#   * logs             — when each watcher last actually did something.
#
#   ./status.sh
# -----------------------------------------------------------------------------
set -uo pipefail
cd "$(dirname "$0")" || exit 1

REPO="$(pwd)"
DOMAIN="gui/$(id -u)"

# watcher-name : script : log : launchd label ("-" when it has no installer)
WATCHERS=(
  "tech:tech/tech_watcher.py:tech/tech_watcher.log:com.kavin.techwatcher"
  "new-grad:newgrad/newgrad_watcher.py:newgrad/newgrad_watcher.log:com.kavin.newgradwatcher"
  "quant:quant/job_watcher.py:quant/job_watcher.log:-"
)

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
dim()  { printf '\033[2m%s\033[0m\n' "$1"; }

echo
bold "LAUNCHD AGENTS  (background timers)"
found_agent=0
for entry in "${WATCHERS[@]}"; do
  IFS=: read -r name _script _log label <<<"$entry"
  [ "$label" = "-" ] && continue

  plist="$HOME/Library/LaunchAgents/$label.plist"
  if [ ! -f "$plist" ]; then
    printf '  %-10s not installed\n' "$name"
    continue
  fi
  found_agent=1

  if info="$(launchctl list "$label" 2>/dev/null)"; then
    exit_code="$(printf '%s' "$info" | sed -n 's/.*"LastExitStatus" = \([-0-9]*\).*/\1/p')"
    pid="$(printf '%s' "$info" | sed -n 's/.*"PID" = \([0-9]*\).*/\1/p')"
    every=""
    if command -v /usr/libexec/PlistBuddy >/dev/null 2>&1; then
      secs="$(/usr/libexec/PlistBuddy -c "Print :StartInterval" "$plist" 2>/dev/null)"
      [ -n "$secs" ] && every=" every $((secs / 60)) min"
    fi

    state="loaded"
    [ -n "$pid" ] && state="loaded, running now (pid $pid)"

    case "$exit_code" in
      0|"") note="last run OK" ;;
      *)    note="LAST RUN FAILED (exit $exit_code)" ;;
    esac
    printf '  %-10s %s%s — %s\n' "$name" "$state" "$every" "$note"

    # The plist hardcodes an absolute path. If you have cloned the repo more than
    # once, the agent keeps running the checkout it was installed from — which may
    # not be the one you are standing in, editing and pulling into.
    runs="$(/usr/libexec/PlistBuddy -c "Print :WorkingDirectory" "$plist" 2>/dev/null)"
    if [ -n "$runs" ] && [ "$runs" != "$REPO" ]; then
      printf '             !! runs a DIFFERENT checkout: %s\n' "$runs"
      printf '             !! not this one:               %s\n' "$REPO"
      printf '             !! your edits and git pulls here do not affect it.\n'
      printf '             !! to point it here: ./%s/install_agent.sh install\n' \
        "$([ "$name" = tech ] && echo tech || echo newgrad)"
    elif [ -n "$runs" ]; then
      printf '             runs: %s\n' "$runs"
    fi
  else
    printf '  %-10s plist exists but is NOT loaded — run: ./%s/install_agent.sh install\n' \
      "$name" "$([ "$name" = tech ] && echo tech || echo newgrad)"
  fi
done
[ "$found_agent" -eq 0 ] && dim "  (nothing installed — a launchd agent is the 'set it and forget it' option)"

echo
bold "LIVE PROCESSES  (interval loops, nohup, or a run in another terminal)"
procs="$(ps -eo pid,lstart,etime,command 2>/dev/null \
  | grep -E 'tech_watcher\.py|newgrad_watcher\.py|job_watcher\.py' \
  | grep -v grep)"
if [ -z "$procs" ]; then
  dim "  none running right now"
  dim "  (normal: a launchd agent fires --once and exits within seconds)"
else
  printf '%s\n' "$procs" | while IFS= read -r line; do
    pid="$(printf '%s' "$line" | awk '{print $1}')"
    started="$(printf '%s' "$line" | awk '{print $2,$3,$4,$5}')"
    elapsed="$(printf '%s' "$line" | awk '{print $7}')"
    cmd="$(printf '%s' "$line" | cut -d' ' -f8- | sed 's/^ *//')"
    printf '  pid %-7s started %s   up %s\n' "$pid" "$started" "$elapsed"
    printf '      %s\n' "$cmd"
  done
  echo
  dim "  To stop one:  kill <pid>"
fi

# Two loops for the same watcher means double emails and double sheet writes.
for entry in "${WATCHERS[@]}"; do
  IFS=: read -r name script _log _label <<<"$entry"
  n="$(printf '%s\n' "$procs" | grep -c "$(basename "$script")" 2>/dev/null)"
  [ -z "$procs" ] && n=0
  if [ "$n" -gt 1 ]; then
    printf '  !! %s has %s processes running — duplicates send duplicate emails\n' "$name" "$n"
  fi
done

echo
bold "CRON"
if crontab -l 2>/dev/null | grep -E 'watcher|run_tech|run_newgrad|run\.sh' | grep -v '^#'; then
  :
else
  dim "  no crontab entries for the watchers"
fi

echo
bold "LOGS  (when each watcher last did something)"
for entry in "${WATCHERS[@]}"; do
  IFS=: read -r name _script logf _label <<<"$entry"
  if [ ! -s "$REPO/$logf" ]; then
    printf '  %-10s %s\n' "$name" "log empty or missing — has never run, or logs elsewhere"
    continue
  fi
  when="$(stat -f '%Sm' -t '%Y-%m-%d %H:%M' "$REPO/$logf" 2>/dev/null)"
  last="$(tail -n 1 "$REPO/$logf" | cut -c1-96)"
  printf '  %-10s %s\n' "$name" "$when"
  printf '             %s\n' "$last"
done

# launchd writes its own stdout/stderr separately from the watcher's log.
echo
for f in launchd.tech.out.log launchd.tech.err.log \
         launchd.newgrad.out.log launchd.newgrad.err.log; do
  if [ -s "$REPO/$f" ]; then
    when="$(stat -f '%Sm' -t '%Y-%m-%d %H:%M' "$REPO/$f" 2>/dev/null)"
    printf '  %-26s %s  %s\n' "$f" "$when" "$(tail -n 1 "$REPO/$f" | cut -c1-60)"
  fi
done

echo
dim "More detail:  ./tech/install_agent.sh status   |   ./newgrad/install_agent.sh status"
echo
