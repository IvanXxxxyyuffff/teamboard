#!/usr/bin/env bash
# TeamBoard installer. Idempotent; safe to re-run.
#   ./install.sh                 # install for the current Codex user
#   ./install.sh --prefix DIR    # install the data dir somewhere else
#   ./install.sh --dry-run       # print every action, change nothing
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PLUGIN="$ROOT/plugins/teamboard"
DATA="${TEAMBOARD_HOME:-$HOME/.teamboard}"
DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) DATA="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

run() { if [ "$DRY" = 1 ]; then echo "+ $*"; else "$@"; fi; }

# 1) interpreter: any python3 >= 3.8 will do; TEAMBOARD_PYTHON overrides
PY="${TEAMBOARD_PYTHON:-$(command -v python3 || true)}"
if [ -z "$PY" ]; then echo "python3 not found — install Python 3.8+ first" >&2; exit 1; fi
"$PY" - <<'PYCHK'
import sys
assert sys.version_info >= (3, 8), f"need Python 3.8+, got {sys.version.split()[0]}"
PYCHK
echo "python: $PY ($("$PY" -V 2>&1))"

# 2) render .mcp.json (absolute paths, because MCP hosts do not set cwd)
render() {
  local out="$1"
  sed -e "s|__PYTHON__|$PY|g" -e "s|__PLUGIN_DIR__|$PLUGIN|g" -e "s|__HOME__|$DATA|g" \
      "$PLUGIN/.mcp.json.in" > "$out"
  echo "wrote $out"
}
if [ "$DRY" = 1 ]; then echo "+ render $PLUGIN/.mcp.json"; else render "$PLUGIN/.mcp.json"; fi

# 3) data dir
run mkdir -p "$DATA"

# 4) register the marketplace + install the plugin (Codex CLI, if present)
CODEX="$(command -v codex || true)"
if [ -n "$CODEX" ]; then
  run "$CODEX" plugin marketplace add "$ROOT" || true
  run "$CODEX" plugin install teamboard@teamboard || true
  "$CODEX" plugin list 2>/dev/null | grep -F "teamboard@teamboard " || true
else
  echo "codex CLI not on PATH — add this marketplace by hand:"
  echo "  codex plugin marketplace add \"$ROOT\""
fi

# 5) smoke test: the board must boot on its own
PORT="${TEAMBOARD_PORT:-8787}"
if [ "$DRY" = 0 ]; then
  ( cd "$PLUGIN" && TEAMBOARD_HOME="$DATA" TEAMBOARD_PORT="$PORT" "$PY" - <<'PYSMOKE'
import os, sys, time, urllib.request, json
sys.path.insert(0, ".")
import mcp_server as m
base = m.ensure_server()
with urllib.request.urlopen(base + "/api/health", timeout=5) as r:
    print("health:", json.loads(r.read()))
PYSMOKE
  ) || { echo "smoke test failed — see $DATA/server.log" >&2; exit 1; }
fi

cat <<EOF

TeamBoard installed.
  plugin : $PLUGIN
  data   : $DATA  (board.db, server.log)
Next: restart the Codex app, then open the "TeamBoard" entry from the ... menu.
The board service is started on demand — nothing else to run.
EOF
