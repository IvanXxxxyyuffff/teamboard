# Changelog

## 0.3.4 — sync with the subagent, not with its timestamp

The rollout file's mtime gets bumped by the event Codex appends *on the way out*
(`turn_aborted`) and on a clean finish (`task_complete`). So a team that had just been
shut down read "working" for the whole five-minute window. Liveness now reads the tail event:
a terminal last event means the seat is done, whatever the mtime says; the next real event
starts it again. Detection is event-level, not a guess about elapsed time.

The session index cache drops from 12s to 2s (a full scan costs 0.2ms) and both UIs poll at
2s, so start/stop is visible within about 3 seconds. A `blocked` seat whose executor has
finished now reads `stale` instead of `blocked`.

A subagent that is closed and reopened comes back under a **new** session id while the seat
still points at the old one. When the new session's opening text names a seat that has no live
session, that seat now takes the new session automatically (`auto_bound` in `project_state`,
audited as `member.thread_moved`). One candidate only — no guessing when ambiguous.

## 0.3.3 — a blocked task is not a blocked person

The state machine put `blocked` ahead of everything, so a seat with a blocked task read
`卡住·摸鱼中` even while its session file was being written every few seconds. Three subagents
running in Codex, board says "working 0". A live session now wins: the seat reads `working`
with `state_source=session+blocked`, and the blocker rides along on the member row
(`blocked_count` / `blocked_title` / `blocked_reason`) instead of hiding in the drawer.

**Unseated agents are now visible.** When a session shares the parent of this project's own
seats, was written in the last 10 minutes, and is bound to no project, the member list opens
with what it is: a subagent that is running but has no seat. It reports the short id and how
long ago it was last written — no name guessing. The fix is to `add_member` it, which is also
what makes it schedulable.

`session_mtimes()` now also invalidates on directory mtime, so a subagent that was just spawned
shows up immediately instead of after the 12-second cache. `TEAMBOARD_SESSIONS` overrides the
rollout directory (`~/.codex/sessions` by default) so tests can fabricate a live session
without writing into the real one.

## 0.3.2 — a rejected approval stops coming back

Two places lied after a rejection.

**The card reappeared.** The panel renders approvals from the tool-result snapshots the host
replays, and it kept a purely in-memory queue. Reject an approval, the queue drains, and the
next replayed snapshot pushes the same card back as if still pending. The panel now asks the
server before rendering (`/api/approvals/<id>`, `staff-requests`, `retros`) and drops anything
that is no longer `pending`; approvals that were decided are remembered in `localStorage`, so a
replayed snapshot cannot resurrect them. If the check itself fails, the card stays — losing a
real approval is worse than one extra prompt. A failed POST now writes the reason onto the
card in red instead of a toast that scrolls out of view, so a rejected click can no longer look
like nothing happened.

**The empty shell stayed on the home board.** A project is created before its plan is approved.
Reject the plan and you are left with a project that has zero tasks and zero members, pinned to
the front page forever. `/api/overview` now skips shells with no tasks, no members and nothing
pending. The row is kept, so resubmitting the plan still lands on the same project — the card
comes back the moment tasks exist.

## 0.3.1 — a blocked seat can be stale too

A task marked `blocked` does not mean someone is watching it. When the executor is gone
(host process dead, session silent past the window, or no heartbeat), the seat now reads
`stale` instead of `blocked`. The `blocked_reason` stays attached, so you see both
"nobody is here" and "this is what they were stuck on".

Prefix-only `thread_id`s are deliberately **not** used for this downgrade: an 8-character
prefix can match two different sessions, and guessing "dead" from an ambiguous id is worse
than showing `blocked`.

## 0.3.0 — liveness that tells the truth

The board used to show `working` for subagents that had already died with the host app.
After Codex quit, the director would come back, see "进行中", and wait on nobody.

### Added
- `GET /api/projects/<id>/resume-brief` and the `resume_brief` MCP tool: who is gone,
  for how long, which `doing` tasks are orphaned, and what to do first.
- `members.host_pid` / `host_boot` / `last_alive` / `dead_reason`: the MCP server registers
  the Codex process it was spawned from, so a dead host is detected **immediately** instead of
  after the 5–15 minute heartbeat window.
- `members.beat_at`: only `/heartbeat` writes it. `last_seen` is also touched by dispatching and
  status edits, so using it as "is working" evidence made a subagent that died right after spawn
  look busy for 15 minutes.
- `stale` state and `state_source` (`session` / `task` / `task+stale`) surfaced in both UIs.
- `staff_requests.consumed_at`: a hire approval can only be used once.

### Changed
- A staff approval is now redeemed by id (`approval_id`), not by a `size_approved` boolean the
  caller could simply set. Renaming a seat member or changing their role is rejected too —
  that is a `replace` request.
- `board_overview` / `project_status` prepend a warning when `doing` tasks have no live owner.

## 0.2.0 — first public cut

Board service synced with the development branch: seat-stable rosters, approval-gated hiring,
priority quotas, resource leases, evidence gates, deadline/critical-path math.

### Added
- `GET /api/health` → `{app, version, db, pid, uptime_sec}` (probes + autostart + CI).
- `scripts/build_oss.sh` — regenerates the published tree from the development sources;
  `--check` fails when the tree is stale (run it in CI).
- `_autostart.py` — the MCP server boots the board on demand (probe → next free port), so the
  open-source build needs no launch agent.
- `docs/ROADMAP.md` — known gaps, with the reasoning for what is deliberately out of scope.

### Changed
- `members` gained a stable seat number and `roster_state`; a subagent restarting is a *session*
  change (`bind_member`), never a new employee.
- Adding someone to a formed roster now requires a staff request the user approves
  (`POST /api/projects/<id>/staff-requests` → `POST /api/staff-requests/<id>/decide`).
  Vacant seats do not count against the cap.
- `update_task` validates assignee / dependency against the task's own project (same as `add_task`),
  and rejects self-dependencies.
- Write endpoints reject non-object JSON bodies with `400` instead of a 500 traceback;
  `?limit=` is clamped instead of crashing.

### Fixed
- **Windows: the board died before serving.** The startup banner contained `→`; hosted Windows
  runners print through cp1252, so `UnicodeEncodeError` killed the process. Both entry points now
  reconfigure stdio to UTF-8 with `errors="replace"`, and the MCP child's stdio is decoded as UTF-8.
- **macOS/Windows: `127.0.0.1` was routed through `HTTP_PROXY`.** Those runners export proxy vars;
  urllib sent loopback probes to the proxy and every health check timed out while the board was
  actually up. Proxies are disabled for the health probe, the MCP client and the test helpers.
- **Slow DNS stalled startup.** `HTTPServer.server_bind()` reverse-resolves the bind address; on a
  machine without working DNS that blocks before `serve_forever()`. Binds through `TCPServer`
  directly and prints the banner first, so a startup hang is visible.
- `POST /api/projects` returns 400 (not 201) on validation failure.
- Panel API base is resolved through the MCP proxy (`tb_api`) instead of injected `window.__TB_API__`.
- Portable defaults everywhere: `TEAMBOARD_HOME`, `TEAMBOARD_PORT`, `TEAMBOARD_DB`,
  `TEAMBOARD_CATALOG`, `TEAMBOARD_PYTHON`. No absolute paths in the tree.
- Tests: `tests/test_api.py` (15 checks), `tests/test_mcp_stdio.py` (13 checks) — temp port, temp DB,
  zero side effects, no pytest required.

## 0.1.0 — first open-source cut

- Bundled the board service inside the plugin; the MCP server now starts it on demand
  (health probe → free-port fallback → `TEAMBOARD_HOME` for db/log), so there is no launch agent
  and no "start this first" step.
- `VERSION` is the single source of truth; both manifests are generated by `scripts/sync_version.py`.
- `POST /api/projects` returns 400 (not 201) on validation failure.
- `GET /api/health` added (`app`, `version`, `db`, `pid`) for probes and tests.
- Panel API base is injected at read time (`window.__TB_API__`) instead of being hard-coded.
- Portable defaults: `TEAMBOARD_HOME`, `TEAMBOARD_PORT`, `TEAMBOARD_DB`, `TEAMBOARD_CATALOG`,
  `TEAMBOARD_PYTHON`. No `/Users/...` anywhere.
- Tests: `tests/test_api.py`, `tests/test_mcp_stdio.py` (temp port + temp DB, zero side effects).
- MIT LICENSE, README, install/uninstall scripts, CI.
