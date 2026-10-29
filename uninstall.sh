#!/usr/bin/env bash
# Remove TeamBoard from Codex. Keeps your data unless --purge is passed.
set -euo pipefail
DATA="${TEAMBOARD_HOME:-$HOME/.teamboard}"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1
CODEX="$(command -v codex || true)"
if [ -n "$CODEX" ]; then
  "$CODEX" plugin uninstall teamboard@teamboard || true
  "$CODEX" plugin marketplace remove teamboard || true
fi
pkill -f "teamboard/plugins/teamboard/server.py" 2>/dev/null || true
if [ "$PURGE" = 1 ]; then rm -rf "$DATA"; echo "purged $DATA"; else echo "kept data in $DATA (use --purge to delete)"; fi
