# Roadmap

Everything here is a known gap, not a promise with a date. Ordered by "how much it changes
a user's decisions", not by how fun it is to write.

## Next

1. **i18n** — panel + browser board are Simplified Chinese. Strings live in
   `plugins/teamboard/ui/panel.html` and `static/{home,index}.html`; a `static/i18n/{zh,en}.json`
   plus a `t()` helper is enough, no build step.
2. **Goal score ≠ decoration** — `goal_score` currently measures how many tasks are *tagged* with a
   goal, not how much of the goal is met. Replace with weighted completion of `goal_criteria`
   (the criteria already carry a shell command; run it and turn it green/red).
3. **Blocked propagation** — a task whose upstream is blocked should read "waiting on X" and be
   excluded from the critical path, not counted as parallel work.
4. **Remaining-vs-deadline row** — `Σ(estimate × (100−progress)/100)` against `deadline − now`,
   red when over. All fields exist; only the arithmetic is missing.
5. **Event payloads** — `task.updated` events store no `old → new`, so "who changed 6820 to
   Disable" is unanswerable after the fact.

## Later

- `compute_schedule()` ignores per-member `capacity`; overload only shows as a chip.
- `project_state()` opens one SQLite connection per in-flight task (N+1).
- `scan_threads()` globs the whole sessions tree; the director's rollout can be ~100 MB.
- Two countdown implementations (`home.html` / `index.html`) drift by up to one poll interval.
- Abandoned tasks still render the "overdue" chip.
- No `paused_sec` shown per task, though it is recorded.

## Deliberately not doing

- **Multi-user / auth.** The board is loopback-only on purpose. Adding accounts would turn a
  5-minute install into a deployment.
- **Editing from the panel.** The board is read-only for a reason: the only actor allowed to change
  plans is the director (the model), and the only thing the human does is approve.
- **A Gantt view.** One line of "what is the current single blocker" beats a chart nobody reads.
