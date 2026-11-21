#!/usr/bin/env bash
# One-command start: creates the venv on first run, then launches the app.
cd "$(dirname "$0")"
[ -d venv ] || { python3 -m venv venv && ./venv/bin/pip install -q -r requirements.txt; }
# Existing installations also need the SSFlix player dependency.
./venv/bin/python -c 'import playwright' 2>/dev/null || ./venv/bin/pip install -q playwright
./venv/bin/python -m playwright install chromium || exit 1
[ -f .env ] && { set -a; . ./.env; set +a; }  # optional: ANTHROPIC_API_KEY=... for Claude-written titles
echo "Open http://127.0.0.1:8765"
exec ./venv/bin/python main.py
