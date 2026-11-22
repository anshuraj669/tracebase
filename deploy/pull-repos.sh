#!/usr/bin/env bash
# Keep the indexed repos up to date: `git pull --ff-only` every git repo found (2 levels deep) under the folder.
# Tracebase re-indexes changed files on its own. Installed by setup.sh as a cron job (every 15 minutes).
#   tracebase-pull-repos /srv/tracebase/repos
set -uo pipefail
ROOT="${1:-/srv/tracebase/repos}"
find "$ROOT" -mindepth 1 -maxdepth 3 -type d -name .git -prune 2>/dev/null | while read -r g; do
  repo="$(dirname "$g")"
  [[ -n "$(git -C "$repo" -c safe.directory="$repo" remote 2>/dev/null)" ]] || continue   # local-only repo
  if out="$(git -C "$repo" -c safe.directory="$repo" pull --ff-only --quiet 2>&1)"; then
    :
  else
    echo "$(date '+%F %T') $repo: $out"
  fi
done
