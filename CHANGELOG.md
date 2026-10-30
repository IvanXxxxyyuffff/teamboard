# Changelog

## 0.3.14 — the board has to speak first

Gates only bite when someone reaches for them. A director who never looks at the board simply
stops the project, and nothing in 0.3.13 could tell him that. `project_state.nudge` closes that:
it turns "what must someone do right now" into a list — `who`, `what`, and the exact `say`
sentence to act on — and **every MCP tool result** now starts with it (`⏰ 催办 N 条`).

Sources: a task whose owner stopped running, a member running off-board, work with no session,
idle workers while backlog exists, a `doing` task untouched for 30+ minutes, plan derail, an
unacknowledged decision, and no usable autopilot heartbeat.

There is **no mute**: no ack endpoint, no snooze. The list shrinks only when the work is done,
and `level=shout` (idle / misaligned / unacked) also triggers the 403s — urging and blocking are
two faces of one mechanism. The autopilot prompt now starts at step 0: empty `nudge` first, or
the turn was wasted.

Tests: off-board work raises `nudge.level=shout` with `unlogged` and `unassigned`; registering
the task removes both items.

## 0.3.13 — "written" is not "done"

Two remaining holes on the delivery surface, both reachable from the API.

**A task nobody ever ran could be marked done.** `done` checked evidence, evidence path,
verify command and deliverable — all of which a model can produce without ever starting the
task. Execution trace is now a prerequisite: no `task_sessions` row, `time_spent == 0` and
`doing_since == 0` means nobody ever worked on it, so `PATCH status=done` returns
`need_execution` (400). Importing historical work is still possible with `force=true`.

**A task could be born done or born in-progress.** `POST /tasks {status:"done"}` skipped every
`done` check; `{status:"doing"}` skipped `ack_gate` and dependency order. Both are now rejected:
create in `backlog`, assign it, then finish it through the normal path.

Net effect: the delivery surface has exactly one legal route —
**assign → start → execution record → close with evidence**.

Tests: create-as-done rejected; `PATCH done` without execution returns `need_execution`;
`doing` → evidence → done passes.

## 0.3.12 — a decision the director never read is not a decision

Two gaps, both measured on a live project.

**Approval cards never reached the conversation.** The card only existed in `/api` and in
the panel's own queue, so if the director did not call `approval_inbox`, the user saw
nothing — "I submitted three rename requests" / "I don't see them". The MCP layer now
attaches every pending plan, retro and staff request to the `structuredContent` of *every*
tool result, so the card surfaces on the next call regardless of what the model remembers.

**A decision could be ignored.** The user clicked approve, that turn ended, and the next
turn proceeded on the old roster and old plan as if nothing had happened. Decisions and
receipts are now separate records: `need_ack` lists everything the user decided and the
director has not acknowledged. While it is non-empty, `update_task(status=…)`,
`propose_plan` and report export return 403 `need_ack`; `progress` updates still pass,
because reporting progress is not the same as advancing. New MCP tool `ack`; the `decide`
result now says "reply 收到 now, then ack".

Existing databases grandfather every already-decided row when `ack_at` is added — old
decisions are not re-litigated.

Tests: decide → `need_ack` appears → status change 403 → `ack` → gate opens → not counted
twice.

## 0.3.11 — everyone has to be on the board

`idle` answers "is anyone working". It cannot answer **"which task is that person working on"**.
Four sessions can be busy while the board is blank: the idle signal stays green and every
number on the page — progress, hours, blockers — is fiction. That is the institutional version
of "the director never checks what the team is doing".

`project_state.align` now exposes three lists:

| list | meaning | only correct fix |
|---|---|---|
| `unlogged` | session is running, no `doing/blocked/review` task under its name (≥10 min) | `update_task(status="doing", assignee_id=...)` + `member_duty(on=true)` |
| `ghost` | task sits in `doing` while its owner stopped running (≥2 min) | reclaim to `backlog`, or respawn + `bind_member` |
| `nosession` | member holds work but its seat has no session thread | `bind_member(thread_id="<full UUID>")` |

`ok=false` blocks `propose_plan` / `add_member` / report export with 403 `need_align` plus
`next_actions`. The unlocking actions (`update_task`, `member_duty`, `bind_member`,
`heartbeat`) are never gated — closing the exit turns a gate into a deadlock.

The grace windows are deliberate: 10 minutes for `unlogged` (the gap between spawn and the
first `update_task` always exists, and a noisy gate gets bypassed with `force`, which is the
same as no gate), 2 minutes for `ghost` (a stopped agent whose task still reads `doing` is
exactly the bug users keep reporting).

`align.derail` is reported, never gated: a derailed director's one correct move is to replan,
and blocking that locks the car in the ditch.

Surfaced in `health.summary`, the first line of `project_status`, `resume_brief.verdict`,
both UI banners, and the autopilot heartbeat prompt.

## 0.3.10 — the plan is a constraint, not a wall chart

Two holes let a director skip the plan while looking productive.

**Dependency order was advisory.** `blocked_upstream` was painted red on the card and nothing
else — you could start the tail of the chain, finish it, and hand over "13 tasks done" while the
head sat untouched. `status -> doing|done` now returns 400 with `need_force` when the upstream is
not `done`, naming the task that blocks it. `force=true` still exists for genuinely parallel work,
but it has to be argued for.

**Nobody could see that the plan itself was stale.** `board.silent_sec` measures time since the
last write that actually touches the plan — `task.*`, `member.update`, `criteria.upsert`,
`retro.submitted`, `resource.*`, `hypothesis.added`. Deliberately excluded: `member.rebind`
(automatic session binding), `autopilot.*` (config) and `project.updated` (renames, deadlines).
Those would report "just now" for a director who has not opened the plan in thirteen hours —
measured on the live project, 13h01m, while the health summary said everything lined up.

It shows up in `health.summary`, the first line of `project_status`, and an amber banner in the
panel, but only when `derail` is true.

## 0.3.9 — "0 working" must never read as "all fine"

`idle` is now two signals instead of one. `warn` means nobody on the team is working and
tasks are open — it lights immediately. `alert` means that has lasted 15 minutes — it is the
hard gate on `propose_plan` / `add_member` / report export.

Splitting them fixes the exact window that made the board lie right after a restart: Codex
appends a `turn_aborted` to every session on exit, which bumps mtime to "just now". Four seats,
zero working, and the board reported `全部对得上` for another fifteen minutes. Measured
2026-10-29 22:38 on the live project.

The panel banner follows `warn`: amber while the drop is fresh ("0 个员工在干活 · 最后一次动静
N 分钟前"), red once the gate is armed ("全员停摆 N 分钟"). `health.summary` and the first line
of `project_status` say the same thing in the same words.

## 0.3.8 — the team that stops without saying so

The real failure mode was never "hard task". It was **four workers idle and nobody noticing**.
The director's attention is welded to its own thread; the roster is something it glances at and
leaves. That cannot be fixed with a reminder, only with a mechanism.

**`idle` — the aggregate nobody can misread.** `project_state` now returns how long the whole
team has been dead: `{sec, human, alert, workers, working, open_tasks, silent[]}`, with `silent[]`
sorted by who stopped longest ago. The lead is excluded — its job is dispatch, and counting it as
labour makes a stalled team undetectable forever. At 15 minutes with open tasks,
`propose_plan` / `add_member` / report export return **403 `need_idle_fix`**. The way out stays
open: `update_task`, `heartbeat`, `bind_member`, `member_duty` are never blocked.

**`autopilot` — who wakes the director when you say nothing.** Everything in this project was
triggered by your message; silence meant standstill. The plugin now reads
`~/.codex/automations/*/automation.toml`, finds the heartbeat aimed at the lead's thread, and
reports three things: does it exist, does its prompt actually tell the director to look at the
board, and is it firing often enough. Real case that motivated it: a 20-minute heartbeat existed
and the board still sat idle for 45 minutes, because the prompt said `list_agents` — which only
lists sessions that are *running*. Stopped ones vanish from that list, so the director saw
"all clear" while nobody worked. `POST /api/projects/<pid>/autopilot
{action: enable|patch|pause}` creates the heartbeat, or appends only the missing paragraph
without touching the rest of your prompt.

`project_status` and `board_overview` now open with these two lines, ahead of schedule and
progress: both numbers improve while you work alone, and only this pair does not.

## 0.3.7 — identity comes from the session, not the prompt

`s​ession_meta` already carries `payload.source.subagent.thread_spawn.{agent_path,
agent_nickname}`. The board now reads the first 8KB and uses it as the primary name —
`quest10_device (Copernicus) · seat battle_srv` — with the hand-written seat name demoted to
a subtitle. Renaming on rebind costs no approval, because what changed is which session sits
in the chair, not the roster. "The board says one name, the agent runs under another" is now
structurally impossible: the name is reported by the session itself.

Two constants were making the board lie. `SESSION_LIVE_WINDOW` (300s) kept stopped
subagents lit as "working" for five minutes; liveness now uses `SESSION_SILENT_SEC = 90`.
The session index only covered two days, so a thread opened three days ago and still being
written today was simply invisible (`silent = -1` → permanently offline); it now covers ten.

`blocked_dead` also required nothing of `session_live`, so a live session with an old
heartbeat was ruled dead (measured: `tail=reasoning`, silence 0s, state "stalled"). It now
requires `not session_live` first.

`GET /api/projects/<pid>/health` returns `{silent_sec, stale[], vacant[], unseated[],
orphan_tasks[], summary}` so the panel, `board_overview` and the director's own checks read
one set of numbers. `vacant` (session dead 2h+, nothing in hand) states a fact and does not
consume a roster-change approval.

## 0.3.6 — done means "I can show you how I checked"

Three gaps that a board with 23 tasks and 9 hours left actually hit:

**A finished task must carry a command.** `update_task(status=done)` now requires
`verify_cmd` — one command anyone can re-run to see pass/fail — plus an optional
`verify_out` (max 5 lines) and an automatic `verified_at` stamp. An `evidence_path` only
proves a file exists; it does not prove anyone ran it. Tasks finished with `force=true`
are counted as `done_without_evidence` and marked ⚠ on the card, so "13 of 13 finished,
none reproducible" is visible instead of implied.

**Critical path remaining, in hours.** `critical_hours` is the tail task's ETA minus now —
*not* the sum of every `eta_left` on the chain, which double-counts overlapping work and
inflates the answer (A 0–2h plus B 2–5h is 5h, not 7h). `critical_gap = critical_hours −
hours_left`; a positive number means scope has to go, not that the team needs to hurry.
Shown in the countdown block and as a KPI chip.

**Lease renewal and overdue visibility.** Claiming the same task again renews (`renewed:
true`) — a long job must be able to extend, otherwise the TTL expires and another seat can
legitimately take the device while both believe they hold it. An expired lease that nobody
released now reports `overdue_min` and renders red.

`resume_brief` also lists every `abandoned` task with its reason, so whoever takes over sees
which paths are already ruled out before re-walking them.

## 0.3.5 — a written file is not a running agent

The previous fix closed the exit path (`turn_aborted` / `task_complete`) but left the entry
path open: restarting Codex appends `session_meta` / `turn_context` / `compacted`, and sending
a message appends `user_message`. All of them bump the mtime, so a team that had not started
anything yet read "working" again. Liveness now requires a **busy** tail event —
`function_call`, `function_call_output`, `reasoning`, `token_count`, `item_*`, `agent_message`,
`token_usage_record`. Anything else means the file was touched, not that someone is working.

`user_message` is reported separately as `session_awaiting`: the user just pressed send and the
agent has not started yet. Same idea applies to the `unseated` scan — a session that only has
its opening metadata is no longer reported as an agent that is burning tokens without a seat.

Registration is now a hard step, not advice: `board_overview` and `resume_brief` lead with
`⚠ N sessions are running but have no seat`, and both SKILL.md files spell out the two calls
that must follow every `spawn_agent` (`bind_member` with the full UUID, then `member_duty`).

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
