# Project Workspaces

Use project workspaces when the manager should maintain a durable folder of markdown state instead of relying on terminal output alone.

## Folder Layout

Project-linked runs are synced into:

`<controller-root>/projects/<project>/`

The default controller root is now `.team-leader/`. Older `.agent-subsessions/` and `.codex-subsessions/` roots are still recognized automatically.

The manager keeps these files current:

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
- read `dashboard.md` first for live progress
- read `tasks.md` for assignment state
- read `questions.md` before asking the human anything
- read `answers.md` after the human responds
- read `conflicts.md` before launching overlapping writers
- read `reports/<run-id>.md` when a specific child needs closer review

While children are active, the manager refreshes these files automatically in the background. Tasks with `--depends-on` stay blocked until their prerequisites complete, then the manager launches the next wave automatically.

When the user only knows the goal and a few paths, the intended flow is:

1. capture the goal and context in `brief.md`
2. run `orchestrate`
3. let the manager-planner produce `launch-plan.md`
4. let the manager auto-dispatch worker children from that plan
5. answer only the questions that surface in `questions.md`

If you want more self-driving behavior, add delivery settings to the brief:

- `autonomy_mode`: `manual`, `guided`, or `continuous`
- `validation_commands`: shell commands that act as delivery gates
- `completion_sentinel`: optional text marker that means “done”
- `max_planner_rounds`: hard stop for automatic replanning

If you want to stay inside Codex instead of opening the folder, run:

`python3 scripts/team_leader.py status --project <project>`

That prints the same high-signal view directly in the terminal, including the current stage, stage reason, next action, current focus, and exact file paths for the workspace, landing page, and dashboard.

## Conflict Caveat

`conflicts.md` detects overlap from declared ownership. Conflict resolution stays with the manager; it does not auto-merge conflicting file edits.

If two write runs touch the same area, the current safe behavior is:

1. narrow ownership
2. convert one child into a reviewer
3. escalate the decision in `questions.md`
