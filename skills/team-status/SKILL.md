---
name: team-status
description: Use when a user wants a compact live progress view for a team-leader managed project, especially to see active child Codex sessions, stage changes, and latest child notes inside Codex without opening project markdown files.
---

# Team Status

Use this skill when the user wants monitoring only, not planning or dispatch.

This skill is a thin alias around the `team-leader` controller's `team-status` subcommand.

## What To Do

1. Find the `team-leader` controller script.
2. Keep the working directory at the target project unless `--root` is explicitly given.
3. Run the controller with `team-status --project <project>`.
4. Report the current stage, progress, active child summaries, latest child notes, blocked or queued work, open questions, conflicts, and warnings.

## Controller Path

Check in this order:

- `../team-leader/scripts/team_leader.py` relative to this skill
- `skills/team-leader/scripts/team_leader.py` relative to the current repo, if present

If neither exists, say that `team-leader` is not installed or not present in the repo, and do not guess.

## Commands

Single update:

```bash
python3 ../team-leader/scripts/team_leader.py team-status --project <project> --once
```

Progressive updates until the project settles:

```bash
python3 ../team-leader/scripts/team_leader.py team-status --project <project> --exit-when-settled
```

If the user wants a non-streaming high-signal snapshot instead, use:

```bash
python3 ../team-leader/scripts/team_leader.py status --project <project>
```

## Output Style

- Prefer `team-status` over `watch` inside Codex.
- Summarize the stage change first.
- Then list active children and their latest notes.
- Call out blocked or queued runs, open questions, conflicts, and warnings.
- If the project is settled, say whether it completed, failed, or is waiting on a human answer.
