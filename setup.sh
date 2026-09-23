#!/bin/sh
# Rebuild the venv from scratch. Safe to re-run.
#
#   ./setup.sh           full install
#   ./setup.sh --slim    skip the `mcp` package (~30 MB smaller on a pip host)
#
# garminconnect is pinned to 0.3.2 deliberately: that is the version the MCP
# server actually resolves to, and several tools (schedule_week, set_heart_rate_zones)
# call `client.post` / `client.request` directly, which 0.3.x reshuffled. Newer
# is not better here - it is untested against these 148 tools.
#
# Windows: use setup.ps1 instead.
set -e
cd "$(dirname "$0")"

SLIM=0
[ "$1" = "--slim" ] && SLIM=1

MCP="garmin-mcp @ git+https://github.com/Taxuspt/garmin_mcp"

if command -v uv >/dev/null 2>&1; then
  uv venv --python 3.12 .venv
  uv pip install --python .venv/bin/python "$MCP"
  uv pip install --python .venv/bin/python "garminconnect==0.3.2"
else
  # No uv (a plain VPS, a CI box). Stdlib venv + pip gets to the same place.
  python3 -m venv .venv
  .venv/bin/pip -q install --upgrade pip
  .venv/bin/pip -q install "$MCP"
  .venv/bin/pip -q install "garminconnect==0.3.2"
fi

if [ "$SLIM" = "1" ]; then
  # `mcp` is imported by garmin_mcp/__init__.py and used by nothing this script
  # calls, so garmin.py stubs it when it is missing. Dropping it also drops
  # pydantic, starlette, uvicorn and the rest of the server-side tree.
  echo "slim: removing the unused mcp dependency tree"
  PKGS="mcp pydantic pydantic-core pydantic-settings anyio jsonschema \
        jsonschema-specifications referencing rpds-py attrs click sse-starlette \
        starlette uvicorn httpx httpx-sse httpcore h11 python-multipart"
  if command -v uv >/dev/null 2>&1; then
    uv pip uninstall --python .venv/bin/python $PKGS >/dev/null 2>&1 || true
  else
    .venv/bin/pip -q uninstall -y $PKGS >/dev/null 2>&1 || true
  fi
fi

.venv/bin/python test_garmin.py
