#!/bin/sh
# Pineo v2 coding agent: tickets from the hub (ws://127.0.0.1:8787) -> live edits in mobile-app/src.
#   agent/run-hub.sh [--app <dir>] [--hub <ws url>] [--no-fast] [--no-fallback]
# Start it any time; it waits for the hub and reconnects by itself.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
exec node "$here/hub-runner.mjs" "$@"
