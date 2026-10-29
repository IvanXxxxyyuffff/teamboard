# Architecture

Two processes, one SQLite file.

```
Codex host
  └─ stdio ─ mcp_server.py        (plugin, ~40 KB, stdlib only)
                │  spawns on demand (health probe → free port → TEAMBOARD_HOME)
                ├─ HTTP 127.0.0.1:8787 ─ server.py  (board API + static UI)
                │                          └─ SQLite  $TEAMBOARD_HOME/board.db
                └─ MCP App resource ─ ui/panel.html   (inline panel, data via `tb_api` tool)
```

## Why the panel does not call the API directly

MCP App panels run in a sandboxed iframe with a `connect-src` CSP. Pointing that at
`http://127.0.0.1:8787` works only if the host honours `_meta.openai/widgetCSP`; when it does not,
every `fetch` is blocked and the panel renders blank. So the panel calls the `tb_api` MCP tool,
which proxies the request from the *server* process, where no CSP applies. Same code path in every
host.

## Why autostart lives in the MCP server

A plugin cannot ship a launch agent on Linux/Windows, and asking users to run a second command is
where most installs die. The MCP server boots the board itself:

1. `TEAMBOARD_URL` set → use it, done.
2. Probe `/api/health` on `TEAMBOARD_PORT` … `+14`; the first one that answers `app=teamboard` wins
   (so a second Codex window reuses the first board instead of fighting for the port).
3. First free port wins → spawn `server.py` detached with `TEAMBOARD_DB` in `TEAMBOARD_HOME`,
   wait up to ~10 s for health, and log to `$TEAMBOARD_HOME/server.log`.

Failure is non-fatal: stdio stays alive and each tool returns the real error instead of hanging.

## Data model (SQLite, no migrations directory)

`projects · members · tasks · events · approvals · task_sessions · resource_leases · hypotheses ·
criteria · retros`. `db()` opens per request with `timeout=15` and WAL-friendly defaults; a
schema is created on first boot with `CREATE TABLE IF NOT EXISTS`.

## Versioning

`plugins/teamboard/VERSION` is the only place a version is written. `scripts/sync_version.py`
regenerates both `plugin.json` files; `mcp_server.py` and `server.py` read the file at startup and
report it through `serverInfo` and `/api/health`. CI fails if the generated manifests drift.

## What is intentionally out of scope

- No auth: the board is a local single-user tool, bound to `127.0.0.1`.
- No multi-tenant: one data dir = one user's projects.
- No scheduler: the model drives the board through MCP tools; the board never spawns agents itself.
