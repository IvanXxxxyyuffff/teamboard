# TeamBoard

![ci](https://github.com/IvanXxxxyyuffff/teamboard/actions/workflows/ci.yml/badge.svg)
![license](https://img.shields.io/badge/license-MIT-black)

An in-chat kanban for AI teams. Open one panel and see **which teams you are running, who is on
each one, what every member is doing right now, how far along it is, and where it is stuck.**

Built as a Codex plugin: one MCP server (stdlib Python), one inline MCP App panel, one SQLite file.
No cloud, no accounts, no npm install.

```
┌ TeamBoard ───────────────────────────────── 71% ─┐
│ 目标  ship the offline build · 截止 09:50 · 剩 6h │
│ ───────────────────────────────────────────────── │
│ 工作中 2  摸鱼 3   待办 5  进行中 3  待验收 1    │
│                                                   │
│ ● 主线程 /root   项目总监   工作中  4m           │
│ ● battle_srv     服务端     摸鱼    45m          │
│ ● equip_ui       客户端     工作中  15m          │
└───────────────────────────────────────────────────┘
```

## 30-second install

```bash
git clone https://github.com/IvanXxxxyyuffff/teamboard
cd teamboard
./install.sh            # renders .mcp.json, registers the marketplace, smoke-tests the board
```

Restart the Codex app once (plugin manifests are read at process start), then open
`⋯ → TeamBoard` in any chat. Nothing else to start — the board service boots on demand.

Requirements: **Python 3.8+** (stdlib only). macOS / Linux / Windows (WSL) all fine.

> **UI language:** the panel and the browser board are Simplified Chinese today; docs and tool
> descriptions are English. Translation is tracked in `docs/ROADMAP.md` — the strings are all in
> `plugins/teamboard/ui/panel.html` and `static/*.html`, no build step.

Manual route, if you prefer doing it yourself:

```bash
codex plugin marketplace add "$PWD"
codex plugin install teamboard@teamboard
```

## What you get

| Surface | What it does |
|---|---|
| Panel (`⋯ → TeamBoard`) | Teams, members, live task, weighted progress, deadline countdown, dependency flow, blockers, resource leases |
| 25 MCP tools | `board_overview`, `project_status`, `propose_plan`, `add_task`, `update_task`, `set_member_model`, `claim_resource`, `add_hypothesis`, … |
| Plan approval | A team never starts silently: the director submits members + tasks + estimates, you approve, then work begins (5 members max by default) |
| Evidence discipline | A task cannot be marked done without `evidence` + a deliverable path; hypotheses are recorded with the command that refuted them |

## Everyday use

```
You: 拉起一个团队，把 <project> 在周五前做完
Model: create_project(deadline=…) → propose_plan(4 members, 9 tasks, estimates)
       → [approval card] → you approve → spawn agents → set_member_thread → update_task as work lands
```

Ask "我的团队现在在做什么 / 谁是卡点 / 打开面板" and the skill routes to the right tools.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `TEAMBOARD_HOME` | `~/.teamboard` | data dir (`board.db`, `server.log`) |
| `TEAMBOARD_PORT` | `8787` | preferred port; the next 14 free ports are tried if taken |
| `TEAMBOARD_URL` | — | point at an already-running board and skip autostart |
| `TEAMBOARD_DB` | `$TEAMBOARD_HOME/board.db` | explicit database path |
| `TEAMBOARD_CATALOG` | `~/.codex/magpie-desktop-models.json` | optional model catalog for the model picker |
| `TEAMBOARD_PYTHON` | current interpreter | interpreter used to spawn the board |

## Repo layout

```
.
├── .agents/plugins/marketplace.json      # marketplace entry (codex plugin marketplace add .)
├── plugins/teamboard/
│   ├── .codex-plugin/plugin.json         # Codex manifest (generated from VERSION)
│   ├── plugin.json                       # portable manifest (generated from VERSION)
│   ├── .mcp.json.in                      # rendered to .mcp.json by install.sh
│   ├── mcp_server.py                     # MCP stdio server + panel hosting + autostart
│   ├── server.py                         # board HTTP API + SQLite (stdlib)
│   ├── static/                           # full-page board (open in a browser if you like)
│   ├── ui/panel.html                     # inline MCP App panel
│   ├── skills/teamboard/SKILL.md         # when/how the model should use the tools
│   ├── assets/logo.svg
│   └── VERSION                           # single source of truth for the version
├── tests/                                # pytest-free smoke tests (python3 tests/test_*.py)
├── install.sh  uninstall.sh
└── docs/ARCHITECTURE.md
```

## Development

```bash
python3 tests/test_api.py            # board API contract (temp port + temp DB)
python3 tests/test_mcp_stdio.py      # "clone and run": MCP boots its own board
python3 plugins/teamboard/server.py --port 8787 --host 127.0.0.1   # standalone board UI
```

Bump `plugins/teamboard/VERSION`, then:

```bash
python3 scripts/sync_version.py      # rewrites both plugin.json files from VERSION
```

## Security notes

- The board binds `127.0.0.1` only and has no auth. Anything running as your user can read/write it.
- Mutating requests only accept loopback `Origin`s (`127.0.0.1` / `localhost` / `::1`), and `Origin: null`
  is rejected — otherwise any `file://` page or sandboxed iframe could create and delete projects.
  Requests with no `Origin` header (curl, MCP) pass. Run the board on a machine you trust.
- `TEAMBOARD_URL` lets you point at a remote board; that board has no auth either — put it behind a
  tunnel with its own auth if you do.

## 中文速览

`./install.sh` → 重启 Codex → 侧边栏 `⋯` 里点「TeamBoard」。看板服务会在第一次调用工具时自动拉起，
数据在 `~/.teamboard/board.db`。拉起团队走「总监提计划 → 你审批 → 派活」，默认不超过 5 名员工。

## Limitations (honest list)

- Single-user, single-machine. No accounts, no auth, no multi-tenant separation.
- SQLite with WAL; fine for one board, not for a shared server.
- Progress is a number the model writes; the board cannot verify it. What it *can* enforce is the
  gate — no `evidence` + existing deliverable path, no `done`.
- Task estimates are self-reported; the burn-down is only as honest as the estimates.
- `docs/REVIEW-*.md` is not shipped; known P2 issues are listed in `docs/ROADMAP.md`.

MIT licensed. PRs welcome.

## Optional: block "work first, report later" at the host layer

The board can only judge what it is told. A member that never calls the plugin can still read code
and run commands — the ledger stays empty. Codex fires a `PreToolUse` hook *before* a tool runs,
which is the only place to catch that.

`plugins/teamboard/hooks/pretooluse.py` answers one question per command: **is this session a seat
on the board, and does it hold a task right now?** Members without an in-progress task are refused,
with the exact `update_task(...)` call they need. The lead, unregistered sessions, non-work tools
and any error are let through — a broken gate must never brick the app.

Register it once in `~/.codex/hooks.json` (Codex will ask you to trust it):

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Bash",
        "hooks": [{ "type": "command",
                    "command": "python3 /absolute/path/to/plugins/teamboard/hooks/pretooluse.py" }] },
      { "matcher": "apply_patch",
        "hooks": [{ "type": "command",
                    "command": "python3 /absolute/path/to/plugins/teamboard/hooks/pretooluse.py" }] }
    ]
  }
}
```
