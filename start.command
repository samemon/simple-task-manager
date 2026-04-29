#!/bin/bash
# Double-click this file on macOS to launch Research Task Manager.
# First run: right-click → Open (to bypass Gatekeeper).
cd "$(dirname "$0")"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Research Task Manager"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Require Python 3
if ! command -v python3 &>/dev/null; then
    echo "✗  Python 3 not found."
    echo "   Install from https://python.org and try again."
    read -n 1 -s -r -p "Press any key to close…"
    exit 1
fi

# Create virtual environment once
if [ ! -d ".venv" ]; then
    echo "→  Creating virtual environment…"
    python3 -m venv .venv
fi

source .venv/bin/activate

echo "→  Checking dependencies…"
pip install -q -r requirements.txt

echo "→  Starting app at http://localhost:8080"
echo "   Close this window to stop."
echo ""
python app.py
