#!/bin/sh
# Start the Pineo hub (port 8787). Usage: hub/run.sh [--port N]
cd "$(dirname "$0")/.." || exit 1
exec .venv/bin/python -m hub "$@"
