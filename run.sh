#!/usr/bin/env bash
# One-command start: creates the venv on first run, then launches the app.
cd "$(dirname "$0")"
[ -d venv ] || { python3 -m venv venv && ./venv/bin/pip install -q -r requirements.txt; }
echo "Open http://127.0.0.1:8765"
exec ./venv/bin/python main.py
