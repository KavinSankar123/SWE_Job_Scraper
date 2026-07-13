#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# push.sh — stage, safety-check, commit, and push in one step.
#
# It refuses to push if a secret launcher (run.sh, run_tech.sh) or a real-looking
# Gmail app password ever ends up staged, so credentials can't leak into the repo.
# Both launchers are git-ignored, so they are never staged in the first place —
# this is a belt-and-suspenders backstop.
#
# Usage:
#   ./push.sh "your commit message"     stage everything, commit, and push
#   ./push.sh                           same, with a default message
#   ./push.sh -n ["message"]            dry run: stage + safety-check only,
#                                       then unstage (no commit, no push)
# -----------------------------------------------------------------------------
set -euo pipefail
cd "$(dirname "$0")" || exit 1

DRY=0
if [ "${1:-}" = "-n" ] || [ "${1:-}" = "--dry-run" ]; then DRY=1; shift; fi
MSG="${*:-Update job watcher}"

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "Not a git repository."; exit 1; }

git add -A

if git diff --cached --quiet; then
  echo "Nothing to commit — working tree matches HEAD."
  exit 0
fi

echo "Staged files:"
git diff --cached --name-only | sed 's/^/  /'
echo

# ---------------------------------------------------------------------------- #
# SAFETY GATE — never let the secret launcher or a real credential through.
# ---------------------------------------------------------------------------- #
fail=0
# Both launchers hold a real Gmail app password (run.sh -> quant, run_tech.sh -> tech).
for secret in run.sh run_tech.sh; do
  [ -e "$secret" ] || continue
  git check-ignore -q "$secret" || { echo "  !! $secret is NOT git-ignored"; fail=1; }
  git diff --cached --name-only | grep -qx "$secret" && { echo "  !! $secret is staged"; fail=1; }
done

# Any ADDED line assigning an app-password-shaped value (four 4-letter groups),
# ignoring the doc placeholders (xxxx / aaaa / abcd efgh ijkl mnop).
if git diff --cached -U0 | grep -E '^\+' \
     | grep -iE 'APP_PASSWORD.*[a-z]{4} [a-z]{4} [a-z]{4} [a-z]{4}' \
     | grep -viqE 'xxxx|aaaa|abcd|efgh|ijkl|mnop|placeholder'; then
  echo "  !! a real-looking app password appears in the staged diff"; fail=1
fi

if [ "$fail" -ne 0 ]; then
  echo
  echo "ABORTED — unstaging; nothing was committed or pushed."
  git reset -q
  exit 1
fi
echo "Safety checks passed -- no secrets staged."

if [ "$DRY" -eq 1 ]; then
  echo "(dry run) would commit as: \"$MSG\"  then push. Unstaging now."
  git reset -q
  exit 0
fi

git commit -q -m "$MSG"
echo "Committed: $(git log --oneline -1)"

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
GIT_TERMINAL_PROMPT=0 git push origin "$BRANCH"
echo "Pushed to origin/$BRANCH"
