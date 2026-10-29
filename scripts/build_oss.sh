#!/usr/bin/env bash
# Regenerate the published plugin tree from the development sources.
#
#   src/teamboard/            board service + web UI (source of truth)
#   src/teamboard-plugin/     MCP server, inline panel, skills, manifests
#   oss/teamboard-oss/        this repo (English packaging layer on top)
#
# Code and UI are overwritten; packaging (README, plugin.json, CHANGELOG,
# SKILL.md, VERSION, .mcp.json.in, LICENSE, assets) is never touched here —
# those are translated/maintained in this repo.
#
#   ./scripts/build_oss.sh            # sync
#   ./scripts/build_oss.sh --check    # exit 1 if the tree is stale (CI)
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
SRC="${TEAMBOARD_SRC:-$ROOT/../teamboard}"
PLG="${TEAMBOARD_PLUGIN_SRC:-$ROOT/../teamboard-plugin/plugins/teamboard}"
DST="$ROOT/plugins/teamboard"
CHECK=0
[ "${1:-}" = "--check" ] && CHECK=1

[ -f "$SRC/server.py" ] || { echo "missing $SRC/server.py (set TEAMBOARD_SRC)" >&2; exit 1; }
[ -f "$PLG/mcp_server.py" ] || { echo "missing $PLG/mcp_server.py (set TEAMBOARD_PLUGIN_SRC)" >&2; exit 1; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# 1) board service: port the one line that turns on the self-start shim
python3 - "$SRC/server.py" "$tmp/server.py" <<'PY'
import sys
src, out = sys.argv[1], sys.argv[2]
s = open(src, encoding="utf-8").read()
# OSS: data dir defaults next to the plugin (portable), not the repo root
old_db = 'DB_PATH = os.environ.get("TEAMBOARD_DB") or os.path.join(BASE, "board.db")'
new_db = ('_HOME = os.environ.get("TEAMBOARD_HOME", "").rstrip("/")\n'
          'DB_PATH = (os.environ.get("TEAMBOARD_DB")\n'
          '           or (_HOME + "/board.db" if _HOME else os.path.join(BASE, "board.db")))')
assert s.count(old_db) == 1, "server.py 的 DB_PATH 行变了，同步 build_oss.sh"
s = s.replace(old_db, new_db, 1)
open(out, "w", encoding="utf-8").write(s)
PY

# 2) MCP server: same file, BASE resolved by the autostart shim
python3 - "$PLG/mcp_server.py" "$tmp/mcp_server.py" <<'PY'
import sys
src, out = sys.argv[1], sys.argv[2]
s = open(src, encoding="utf-8").read()
old = 'BASE = os.environ.get("TEAMBOARD_URL", "http://127.0.0.1:8787").rstrip("/")'
new = ('from _autostart import boot as _boot\n'
       'BASE = _boot()        # 探测已在跑的看板，没有就起一个（开源版无 launch agent）')
assert s.count(old) == 1, "mcp_server.py 的 BASE 行变了，同步 build_oss.sh"
open(out, "w", encoding="utf-8").write(s.replace(old, new, 1))
PY

sync() {   # sync <src> <dst>
  if [ "$CHECK" = 1 ]; then
    cmp -s "$1" "$2" || { echo "STALE: ${2#$ROOT/}"; return 1; }
  else
    mkdir -p "$(dirname "$2")"
    cp "$1" "$2"
    echo "updated ${2#$ROOT/}"
  fi
}

rc=0
sync "$tmp/server.py"                 "$DST/server.py"                 || rc=1
sync "$tmp/mcp_server.py"             "$DST/mcp_server.py"             || rc=1
sync "$SRC/static/home.html"          "$DST/static/home.html"          || rc=1
sync "$SRC/static/index.html"         "$DST/static/index.html"         || rc=1
sync "$SRC/static/theme.css"          "$DST/static/theme.css"          || rc=1
sync "$PLG/ui/panel.html"             "$DST/ui/panel.html"             || rc=1

if [ "$CHECK" = 1 ]; then
  [ "$rc" = 0 ] && echo "OK: published tree matches sources"
  exit "$rc"
fi
echo "done. now: python3 -m py_compile plugins/teamboard/*.py && python3 -m pytest tests -q"
