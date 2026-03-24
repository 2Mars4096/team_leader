# Project Workspaces

Use project workspaces when the manager should maintain a durable folder of markdown state instead of relying on terminal output alone.

## Folder Layout

Project-linked runs are synced into:

`<controller-root>/projects/<project>/`

The manager keeps these files current:

- `project.md`: project metadata and file map
- `dashboard.md`: live run table and recent output preview
- `tasks.md`: task-oriented ledger
- `manager-summary.md`: concise aggregate snapshot
- `questions.md`: human-facing questions and blockers
- `conflicts.md`: owned-path overlap risk
- `reports/<run-id>.md`: one markdown report per child run

## Recommended Metadata

For every meaningful child run, set:

- `--project`
- `--task-id`
- `--role`
- `--owned-path` for any writer
- `--depends-on` for tasks that should not start yet

Without that metadata, the dashboard is still usable, but the task ledger and conflict-risk reporting are weaker.

## Manager Behavior

Treat the markdown files as the control surface:

- read `dashboard.md` first for live progress
- read `tasks.md` for assignment state
- read `questions.md` before asking the human anything
- read `conflicts.md` before launching overlapping writers
- read `reports/<run-id>.md` when a specific child needs closer review

## Conflict Caveat

`conflicts.md` detects overlap from declared ownership. It does not automatically merge conflicting file edits.

If two write runs touch the same area, the current safe behavior is:

1. narrow ownership
2. convert one child into a reviewer
3. escalate the decision in `questions.md`
