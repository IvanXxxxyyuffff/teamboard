# Changelog

## 0.3.25 — watch the file, not the process

The board answered "is the process alive". The useful question is "did anything land on disk".
A seat can hold a `doing` task, heartbeat on schedule, report 45% — and not have written a byte
in forty minutes. That is spinning, and it used to look exactly like work.

- **Write fingerprint**: `heartbeat(path=...)` (or `paths=[...]`) makes the server `stat` the
  deliverable and store the real mtime/size. Self-reported mtimes are not trusted; `producing`
  means mtime moved within 10 minutes.
- **Pace per task**: `pace_min` (default 20, `0` = no files expected). A `doing` task whose
  deliverable has not moved for longer than its pace is flagged `spinning` and pushed to nudge.
- **Active sweep**: a background loop (default 120 s) stats every `doing` deliverable and writes
  a `task.spin` event when a task crosses its pace — the board notices idle spinning without
  anyone asking it to look.
- **Reuse before spawn**: `bind_member` refuses to hand a seat to a new thread while the old
  session is still writing (tail event is a tool/thought within 90 s); pass `force=true` to
  override. Members expose `spawn_hint: reuse|new`.
- **Verify is executed, not claimed**: marking `done` runs `verify_cmd` itself (timeout 120 s,
  `verify_timeout` to adjust). Non-zero exit → 400 `need_verify_pass` with the real tail;
  `verify_out` is overwritten with the actual output.
- **Granularity nudge**: open tasks estimated over 2 h are surfaced as "split it" — small units
  mean frequent writes, and frequent writes make spinning visible.
- Member cards show "落盘 X 前" (last real write) next to utilization; task cards show the
  `空转` chip. Both are facts, not estimates.

## 0.3.24 — context budget is part of the interface

Eight items of field feedback from a 10-hour run. The one that costs the most is not a bug:
`project_status` returned every finished task, ~40 KB per call, and the lead reads it every turn.
Default is now compact — open tasks (≤20, whitelisted fields) + last 3 finished + counts; `full=true`
keeps the old payload. Measured on the reference project: 41 KB → 6 KB.

- `done` tasks no longer trip `missing_output` / `deliverables_missing`. Deleting an old APK after
  delivery is cleanup, not a violation; the check now only applies to `review` tasks.
- `PATCH /api/projects/<id>` gets an MCP front door (`update_project`) so a stale goal line can be
  rewritten from the session instead of returning `no route`.
- `POST /api/projects/<id>/criteria/run` + `run_criteria` tool: actually executes each criterion's
  `cmd` and writes red/green back (exit 0 = green, timeout 60 s = red). A reading is now stamped with
  the time it was taken, so "16/163" can't sit there for hours while the project is at 32/163.
- `slackers` only counts `working`/`idle` seats; someone who delivered and logged off is not a slacker.
- `blocker.kind` separates `resource` (device held by someone else) from `depends_on`, so a single
  emulator doesn't masquerade as a logical dependency.
- Top-level `progress` block: mainline reading, age, `stale` flag — the number you actually ask for
  no longer hides inside `criteria[0].output`.

## 0.3.23 — mid-turn commentary is not a turn ending

A `message` event is written whenever the agent says something inside a turn ("Now the patch:").
It was not on the busy list, so the seat dropped to offline for the second between that message and
the next `token_count` — measured at `silent=10s` while the transcript was being appended to.
`message` now counts as work; the terminal events still win because they are checked first.

## 0.3.22 — archived is not dead

A subagent can be running inside a rollout that lives in `~/.codex/archived_sessions/`:
`followup_task` reuses the old thread, the file keeps being written, and a board that only indexes
`~/.codex/sessions/` sees nothing. One seat was reported as "no session / 摸鱼" while its transcript
was being appended to seven minutes earlier.

**The archive is indexed.** Both roots are scanned (archive limited to the last ten days of writes),
and `session_path_of` falls back to it, so tail/digest/handover all work for a reopened thread.

**Binding is recovered, not asked for.** When `thread_id` is empty, the seat's `agent_path`
(`/root/<leaf>`) is matched against the last hour of rollouts; a match younger than ten minutes is
bound immediately with a `member.thread_rebind` event. A match that is older, or an agent with no
session at all, becomes a `rebind` nudge carrying the exact `bind_member(...)` command — instead of
being reported as an empty chair.

**A finished turn is not a working seat.** `task_complete` now wins over a stale heartbeat: a seat
whose turn ended ten minutes ago can no longer be drawn as `工作中` just because something beat on
its behalf.

## 0.3.21 — an open thread is not a running turn

Codex's subagent list counts *threads that are still open*. The board counts *turns that are
still running*. After a subagent finishes its turn the thread stays open, so the two lists
disagree for minutes and the board looks broken ("it says nobody is working, but I can see one").

**The badge says which it is.** A seat whose transcript ended with `task_complete` now reads
`僵住·回合已结束` / `摸鱼中·回合已结束`, and a seat with no session at all reads `·无会话`. No more
guessing whether "not working" means "finished" or "never started".

**A pending tool call counts as working.** When the last event is a `function_call` with no
`function_call_output` yet — a long `adb` loop, a full regression run — the seat is treated as
running for up to 30 minutes even though the transcript has not been written to. Silence is only
evidence of idleness when nothing is in flight.

## 0.3.20 — a button that does something

Two chips on the nudge and align banners ("现在该做" / "怎么修") looked like buttons and were
tooltips. Clicking them did nothing, which is worse than not having them: the one place the board
tells you what to do next was the one place that could not be used.

**They open a sheet now.** The full list — every nudge item with the command it wants, or every
un-aligned seat with the exact three-step fix (handover → reassign with a note → or abandon with a
reason) — and a copy button per row.

**Copy, not edit.** The board stays read-only for the person watching it. The only useful verb on
this screen is "take the command and give it to the director", so that is the button.

## 0.3.19 — take the work over, do not sweep it away

A stalled seat is not junk. Recycling its task to `backlog` is irreversible: whatever it already
figured out — the branch it ruled out, the half-finished patch, the reason it was stuck — is gone
the moment the row says `backlog`, because nobody wrote it down.

**Handover before recycle.** Moving a task from `doing` to `backlog` now requires a `resume_note`
(unless `force=true` is passed with a reason), exactly like re-opening a half-done task does. The
error message carries what the stalled worker said last, so the note can be written from evidence
instead of memory.

**`GET /api/tasks/<id>/handover`.** One call answers "is any of this worth keeping": the task's
progress and checkpoint, the seat and agent that held it, the last four tools it ran, and the last
thing it said (read from the transcript tail, cached by size+mtime), plus the four next moves —
verify the artifact, continue with a note, reassign, or abandon with a reason.

**The nudge says so too.** A ghost item no longer reads "recycle it and reassign"; it names the
worker's last sentence and points at the handover endpoint first. Recycle stays available; it just
stops being the reflex.

## 0.3.18 — an empty chair is not a headcount

Two things the nudge was getting wrong about idle capacity.

**Vacant seats are now named.** A seat with no bound thread, no doing task and no load is not
"quiet", it is a chair nobody is sitting in — it consumes a slot from the roster while producing
nothing, and it is invisible in every other view because there is no session to look stale.

**New work goes to whoever is actually free.** The nudge used to hand the next backlog item to
the first non-working member, which could be a seat already stuck with an unreclaimed `doing`
task. Adding another task to a blocked pipe is not scheduling; it just makes the board look
staffed. The first pick is now a member with nothing in hand, and only if none exists does it
fall back to the merely idle.

## 0.3.17 — the hook pays rent

The PreToolUse guard used to only take: it read the board on every write tool and either allowed
or denied. Now it gives something back.

**A seated member beats by working.** Every allowed write tool from a member that already holds a
`doing` task also refreshes that member's heartbeat (20s throttle). Subagents never call
`heartbeat` on their own, which is why a worker running commands for twenty minutes could still
be drawn as `摸鱼中`. The hook is the one place every tool call must pass through, so it is the
only place this can be fixed for good.

**Every decision is logged** to `/tmp/teamboard-hook.log` (`pass:lead` / `pass:seated` /
`pass:unregistered` / `deny` / `pass:board-down`, plus tool and session). Whether a host hook is
actually being invoked cannot be proven from the outside; this file is the proof, and the first
thing to check when a guard "does not seem to work".

**State no longer flickers between working and stuck.** A subagent that ends one turn and starts
the next a few seconds later used to flip `working → stale → working` on every poll, because a
`task_complete` tail immediately meant "nobody is here". Terminal events now get a 20s grace
window before they count as a dead seat — measured on a real team where one seat flipped twice in
four seconds.

## 0.3.16 — never start over

A task that is half done is an asset, and restarting it from zero throws that asset away. Three
things now make that impossible to do quietly.

**Checkpoints.** Tasks carry `checkpoint` / `checkpoint_at`: where the last run stopped, which
paths are already ruled out, what comes next.

**A resume note is required.** Re-opening a task that already has progress or a checkpoint —
whether it is the same member coming back or a different one taking over — returns 400
`need_resume_note` until the caller writes what they reviewed. The note becomes the new checkpoint.

**Progress only moves up.** Dropping it requires `confirm_restart=true` plus a `restart_reason`,
and the restart is logged as an event (`from`, `to`, `why`) so "who decided to throw the work away"
is answerable later.

**Host-level gate (optional, recommended).** `hooks/pretooluse.py` runs before Bash / apply_patch:
it asks the board whether this session is a seat and whether that seat holds a task. Unregistered
work is refused with the exact `update_task(...)` call. The lead, unknown sessions, non-work tools
and any error pass through — a broken gate must never brick the app. See README for the hooks.json
snippet; Codex asks you to trust the hook once.

Tests: half-done restart without note 400 → with note 200 and checkpoint stored; progress rollback
400 → documented restart 200; `/api/threads/<tid>` resolves a seat and answers `found=false`
otherwise.

## 0.3.15 — "continue" is not "carry on"

Quit the app mid-task and come back: the `doing` rows are still there, but the agents behind them
are gone. Saying "continue" and immediately assigning new work stacks fresh tasks on an empty
ledger — progress climbs, every cell of it fiction. `PATCH status=doing` on a task that is not
already in progress now returns 403 `need_resume` while any leftover `doing` has a stopped owner.

The way out is the repair itself, not an acknowledgement: `resume_brief`, then reclaim to
`backlog` / hand it to a live member / respawn + `bind_member` / mark `blocked`. Clear the
leftovers and the gate opens by itself.

Also enforced: a task cannot enter `doing` without an owner (`need_assignee`) — an unowned
in-progress task cannot be chased by anyone, so it is just a row that looks busy. And `add_task`
now accepts `assignee` (member id or name) as an alias for `assignee_id`, so the gate cannot be
sidestepped by using the other spelling.

MCP: every tool result opens with `🔁 续跑未对齐` while leftovers exist.

Tests: backdate a `doing` task with a stopped owner → `resume.pending` → new work 403 → reclaim →
allowed; unowned `doing` refused.

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
