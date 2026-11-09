#!/bin/bash
# Wrapper for the launchd scan job: sets PATH, activates the venv, runs one scan.
cd "$(dirname "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
source .venv/bin/activate 2>/dev/null
python scan_all.py >> scan.log 2>&1
