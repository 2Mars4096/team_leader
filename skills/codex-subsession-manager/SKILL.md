---
name: codex-subsession-manager
description: Use when a user wants Codex to launch, track, resume, cancel, or coordinate real child Codex sessions as managed subsessions, especially when each child should keep full Codex capabilities rather than acting like a lightweight built-in subagent.
---

# Codex Subsession Manager

## Overview

This skill manages real child Codex sessions driven by `codex exec` and `codex resume`.

Treat a subsession as a full Codex worker with its own thread, context window, tool use, and follow-up lifecycle. This is more flexible than a lightweight in-process subagent because a child session can keep working independently, be resumed later, and itself act as a manager when useful.

Use the control script in `scripts/codex_subsession_manager.py` instead of ad hoc shell fragments. It stores a local `.codex-subsessions/` registry with prompts, commands, logs, last messages, PIDs, and detected thread IDs.

## When To Use

- Launch one or more long-running Codex child sessions from a manager session
- Split work into independent Codex-run tracks that may later be resumed interactively
- Run research, implementation, review, or audit workers that need full Codex behavior
- Dispatch children in the background and poll status while continuing work in the manager
- Cancel or manually reattach to a child session when the work changes

## Core Difference From Built-In Subagents

Built-in subagents are lightweight delegated helpers inside the current orchestration model.

Subsessions in this skill are separate Codex sessions:

- They run through the normal `codex exec` / `codex resume` surface
- They can use the same tools and workflows a normal Codex session would use
- They can persist their own thread and be resumed later
- They can be treated as independent managers rather than one-shot helpers

## Workflow

### 1. Initialize the controller directory

```bash
python3 scripts/codex_subsession_manager.py init
```

This creates `.codex-subsessions/` in the current working directory.

### 2. Dispatch a child run

Use a direct prompt:

```bash
python3 scripts/codex_subsession_manager.py dispatch \
  --name api-audit \
  --full-auto \
  --sandbox read-only \
  --prompt "Audit the API layer for auth and caching risks. Do not edit files. Return findings only."
```

Use a prompt file for longer instructions:

```bash
python3 scripts/codex_subsession_manager.py dispatch \
  --name ui-refactor \
  --full-auto \
  --sandbox workspace-write \
  --prompt-file /tmp/ui-refactor-prompt.md
```

Each run gets its own directory under `.codex-subsessions/runs/`.

### 3. Track progress

```bash
python3 scripts/codex_subsession_manager.py status
python3 scripts/codex_subsession_manager.py show 20260324-120000-ui-refactor
python3 scripts/codex_subsession_manager.py tail 20260324-120000-ui-refactor
```

`status` refreshes run metadata, including completion state and detected thread IDs.

### 4. Resume the child later

Print a ready-to-run resume command:

```bash
python3 scripts/codex_subsession_manager.py resume-cmd 20260324-120000-ui-refactor
```

This prints an interactive `codex resume <thread-id>` command anchored to the original working directory.

For non-interactive follow-up:

```bash
python3 scripts/codex_subsession_manager.py resume-cmd 20260324-120000-ui-refactor --exec
```

### 5. Cancel when needed

```bash
python3 scripts/codex_subsession_manager.py cancel 20260324-120000-ui-refactor
```

Use `--force` to send `SIGKILL` instead of `SIGTERM`.

## Batch Dispatch

For multiple children, create a JSON manifest and dispatch in one command:

```bash
python3 scripts/codex_subsession_manager.py batch --file references/example_manifest.json --dry-run
```

Read `references/prompt-patterns.md` when you need prompt templates for research, implementation, reviewer, or manager-style child sessions.

## Prompting Guidance

Every child prompt should include:

- Objective: the concrete outcome
- Scope: exact files, modules, or investigation area
- Write boundary: whether the child may edit files, and if so which ones
- Validation: tests, checks, or evidence expectations
- Return contract: what summary or artifact the child must produce

When running multiple writers in parallel, assign disjoint file ownership. If that is not possible, turn some children into read-only researchers or reviewers instead of concurrent editors.

## Operational Guidance

- Prefer `--sandbox read-only` for research-only children
- Prefer `--sandbox workspace-write --full-auto` for normal autonomous implementation children
- Use `--add-dir` for extra writable paths outside the main repo root
- Use `--skip-git-repo-check` if a child must run outside a Git repository
- Use `attach-thread` if auto-detection misses a thread ID and you need a stable resume handle
- Use `reconcile` to backfill thread IDs after runs finish

## Guardrails

- Each child session consumes normal Codex usage. Use only as many concurrent children as the task justifies.
- Do not run concurrent writers against the same files unless the user explicitly accepts merge risk.
- This skill manages child Codex sessions, not arbitrary background jobs. Keep the workflow centered on `codex exec` and `codex resume`.
