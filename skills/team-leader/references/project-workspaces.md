# Project Workspaces

Use project workspaces when the manager should maintain a durable folder of markdown state instead of relying on terminal output alone.

## Folder Layout

Project-linked runs are synced into:

`<controller-root>/projects/<project>/`

The default controller root is now `.team-leader/`. Older `.agent-subsessions/` and `.codex-subsessions/` roots are still recognized automatically.

Keep your working directory at the target project when you rely on the default root. If you invoke the controller via an absolute path from the installed skill directory, pass `--root` explicitly instead of changing directories into the skill folder.

The manager keeps these files current while a project is active:

- `README.md`: default landing page and file map
- `brief.md`: project goal, repo paths, spec paths, notes, and constraints
- `launch-plan.md`: latest planner-produced child launch plan
- `validation.md`: validation-command results and machine-evaluable delivery status
- `dashboard.md`: live run table, watcher status, and recent output preview
- `tasks.md`: task-oriented ledger
- `manager-summary.md`: concise aggregate snapshot
- `questions.md`: human-facing questions and blockers
- `answers.md`: human-edited answers keyed by question id
- `answers-template.md`: copy-ready answer lines for open questions
- `conflicts.md`: owned-path overlap risk
- `reports/<run-id>.md`: one markdown report per child run

Once a project settles cleanly, the manager compacts the workspace:

- `history.md` replaces the per-run `reports/` directory with a single compact run history
- transient files such as `dashboard.md`, `questions.md`, `answers-template.md`, and `conflicts.md` are removed
- disposable run artifacts such as prompts, runner scripts, and raw stdout or stderr logs are removed from settled child run directories

For Git-backed writer runs, the manager also uses:

- per-run worktrees under `projects/<project>/worktrees/`
- a shared integration worktree under `projects/<project>/integration/`

Per-run worktrees are released automatically once the writer has been integrated cleanly or produced no diff. The shared integration worktree is retained as the manager-owned combined checkout for validation and final inspection.

Validation commands run in the integration worktree when one exists, so delivery gates evaluate the manager-owned combined result instead of a stale source checkout.

This project folder is persistent manager state. Reusing the same project name reuses the same folder and tracked history. In normal continuation, do not delete the generated markdown files by hand. The intended human-edited file is `answers.md`. For a clean restart, use a new project name. If you want to compact failed or standalone runs explicitly, use `python3 scripts/team_leader.py cleanup`.

## Recommended Metadata

For manager-first planning, start with:

- `intake --project ... --goal ...`
- `orchestrate --project ...`

For directly-dispatched child runs, set:

- `--project`
- `--task-id`
- `--summary`
- `--role`
- `--owned-path` for any writer
- `--depends-on` for tasks that should not start yet

Without that metadata, the dashboard is still usable, but the task ledger and conflict-risk reporting are weaker.

## Manager Behavior

Treat the markdown files as the control surface:

- read `README.md` first as the default landing page
- read `brief.md` to understand the captured goal and constraints
- read `launch-plan.md` to see what the manager-planner most recently proposed
- read `dashboard.md` first for live progress while the project is active
- read `tasks.md` for assignment state
- read `questions.md` before asking the human anything
- read `answers.md` after the human responds
- read `conflicts.md` before launching overlapping writers
- read `reports/<run-id>.md` when a specific child needs closer review

After the project settles and is compacted:

- read `history.md` for the compact run history
- read `manager-summary.md` for the latest aggregate view
- treat the missing live dashboard or per-run reports as a sign that the project has already been compacted

While children are active, the manager refreshes these files automatically in the background. Tasks with `--depends-on` stay blocked until their prerequisites complete, then the manager launches the next wave automatically.

When the user only knows the goal and a few paths, the intended flow is:

1. capture the goal and context in `brief.md`
2. run `orchestrate`
3. let the manager-planner produce `launch-plan.md`
4. let the manager auto-dispatch worker children from that plan
5. answer only the questions that surface in `questions.md`

If you want more self-driving behavior, add delivery settings to the brief:

- `autonomy_mode`: `manual`, `guided`, or `continuous`
- `clarification_mode`: `auto` or `off`
- `validation_commands`: shell commands that act as delivery gates
- `completion_sentinel`: optional text marker that means “done”
- `max_planner_rounds`: hard stop for automatic replanning
- `max_auto_fix_rounds`: cap for automatic validation-failure recovery waves

If you want to stay inside Codex instead of opening the folder, run:

`python3 scripts/team_leader.py status --project <project>`

That prints the same high-signal view directly in the terminal, including the current stage, stage reason, next action, current focus, and exact file paths for the workspace, landing page, and dashboard.

For compact progressive updates that work better in captured Codex output, use:

`python3 scripts/team_leader.py team-status --project <project>`

That emits change-based updates for stage, progress, active child summaries, latest child notes, queued or blocked work, open questions, and warnings.

For a live terminal view, use:

`python3 scripts/team_leader.py watch --project <project>`

Add `--once` for a single render or `--exit-when-settled` if you want it to stop after the project has no running or blocked runs.

## Conflict Caveat

`conflicts.md` detects unresolved overlap from declared ownership plus integration issues from the manager’s worktree flow. Conflict resolution stays with the manager; it does not auto-merge arbitrary conflicting file edits.

If two write runs touch the same area, the current safe behavior is:

1. isolate each writer in its own worktree
2. let the manager serialize overlapping writers and integrate them into the project integration worktree
3. if integration still fails, narrow ownership or escalate the decision in `questions.md`
