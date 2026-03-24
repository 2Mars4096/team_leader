# Project Workspaces

Use project workspaces when the manager should maintain a durable folder of markdown state instead of relying on terminal output alone.

## Folder Layout

Project-linked runs are synced into:

`<controller-root>/projects/<project>/`

The manager keeps these files current:

- `README.md`: default landing page and file map
- `dashboard.md`: live run table, watcher status, and recent output preview
- `tasks.md`: task-oriented ledger
- `manager-summary.md`: concise aggregate snapshot
- `questions.md`: human-facing questions and blockers
- `answers.md`: human-edited answers keyed by question id
- `answers-template.md`: copy-ready answer lines for open questions
- `conflicts.md`: owned-path overlap risk
- `reports/<run-id>.md`: one markdown report per child run

## Recommended Metadata

For every meaningful child run, set:

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
- read `dashboard.md` first for live progress
- read `tasks.md` for assignment state
- read `questions.md` before asking the human anything
- read `answers.md` after the human responds
- read `conflicts.md` before launching overlapping writers
- read `reports/<run-id>.md` when a specific child needs closer review

While children are active, the manager refreshes these files automatically in the background. Tasks with `--depends-on` stay blocked until their prerequisites complete, then the manager launches the next wave automatically.

## Conflict Caveat

`conflicts.md` detects overlap from declared ownership. Conflict resolution stays with the manager; it does not auto-merge conflicting file edits.

If two write runs touch the same area, the current safe behavior is:

1. narrow ownership
2. convert one child into a reviewer
3. escalate the decision in `questions.md`
