#!/bin/sh
# Baseline of mobile-app/src for demo runs. Nothing here runs by itself.
#   agent/reset-app.sh snapshot   record the current src/ as the clean baseline
#   agent/reset-app.sh restore    put src/ back to the baseline (only changed files are rewritten,
#                                 so a running Vite dev server hot-reloads just those)
#                                 and forget the hub runner's memory of earlier changes, so a first
#                                 "undo" at the demo cannot reach into a rehearsal
#   agent/reset-app.sh diff       list files that differ from the baseline
#   agent/reset-app.sh clear-history   only forget the runner's memory (agent/.state/history-*.json)
# PINEO_APP_DIR overrides the app folder (default: pineo/mobile-app).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
app=${PINEO_APP_DIR:-"$here/../mobile-app"}
archive="$here/baseline/$(basename "$app")-src.tgz"

[ -d "$app/src" ] || { echo "reset-app: no src/ in $app" >&2; exit 1; }

state=${PINEO_AGENT_STATE:-"$here/.state"}
clear_history() {
  n=$(ls "$state"/history-*.json 2>/dev/null | grep -c . || true)
  rm -f "$state"/history-*.json
  echo "runner memory cleared ($n history file(s) removed); a running hub runner picks this up at its next ticket"
}

unpack() {
  [ -f "$archive" ] || { echo "reset-app: no baseline yet; run: agent/reset-app.sh snapshot" >&2; exit 1; }
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  tar -xzf "$archive" -C "$tmp"
}

case "${1:-}" in
  snapshot)
    mkdir -p "$here/baseline"
    tar -czf "$archive" -C "$app" src
    echo "baseline recorded: $(tar -tzf "$archive" | grep -vc '/$') files from $app/src"
    ;;
  restore)
    unpack
    changed=$(rsync -rc --delete --itemize-changes "$tmp/src/" "$app/src/" | grep -c '^[>*]' || true)
    echo "src/ is back at the baseline ($changed file(s) restored)"
    clear_history
    ;;
  clear-history)
    clear_history
    ;;
  diff)
    unpack
    # ">" = content differs or missing, "*deleting" = not in the baseline. Attribute-only lines are skipped.
    out=$(rsync -rcn --delete --itemize-changes "$tmp/src/" "$app/src/" | grep '^[>*]' | sed 's/^[^ ]* */  /' || true)
    if [ -n "$out" ]; then echo "$out"; else echo "src/ matches the baseline"; fi
    ;;
  *)
    echo "usage: agent/reset-app.sh snapshot | restore | diff | clear-history" >&2
    exit 2
    ;;
esac
