#!/bin/bash
# Wrapper for the launchd dashboard job: keeps the web app running.
cd "$(dirname "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
source .venv/bin/activate 2>/dev/null
exec python app.py >> dashboard.log 2>&1
