#!/bin/sh
# Windows-safe version of setup.sh — uv on native Windows uses .venv/Scripts,
# not .venv/bin, so the original script's hardcoded /bin/python path fails here.
set -e
cd "$(dirname "$0")"
echo "== working directory =="
pwd

PY=".venv/Scripts/python.exe"

if [ ! -f "$PY" ]; then
    echo "== creating venv =="
    uv venv --python 3.12 .venv
fi

echo "== installing garmin-mcp =="
uv pip install --python "$PY" "garmin-mcp @ git+https://github.com/Taxuspt/garmin_mcp"

echo "== installing garminconnect =="
uv pip install --python "$PY" "garminconnect==0.3.2"

echo "== self-test =="
"$PY" test_garmin.py || true

echo ""
echo "DONE. Install finished (a 'No usable Garmin tokens' message above is expected — you haven't logged in yet)."
echo "Next (and only remaining manual step): run this to log in to Garmin:"
echo "  .venv/Scripts/python.exe garmin.py login"
