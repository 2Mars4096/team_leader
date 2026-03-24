---
name: codex-subsession-manager
description: Use when a user wants Codex to launch, track, resume, cancel, or coordinate real child Codex sessions as managed subsessions, especially when each child should keep full Codex capabilities rather than acting like a lightweight built-in subagent.
---

# Codex Subsession Manager

## Overview

This skill manages real child sessions through a provider adapter layer, with `codex` implemented first via `codex exec` and `codex resume`.

Treat a subsession as a full Codex worker with its own thread, context window, tool use, and follow-up lifecycle. This is more flexible than a lightweight in-process subagent because a child session can keep working independently, be resumed later, and itself act as a manager when useful.

Use the control script in `scripts/codex_subsession_manager.py` instead of ad hoc shell fragments. It stores a local `.agent-subsessions/` registry with prompts, commands, logs, last messages, PIDs, and detected session IDs. If an older `.codex-subsessions/` directory already exists, the script will keep using it.

When runs are linked to a project, the script also maintains a central markdown workspace under `.agent-subsessions/projects/<project>/` with a dashboard, task ledger, manager summary, questions for humans, conflict-risk notes, and one child report per run.

Today the only shipped provider is `codex`. The control plane is intentionally shaped so later adapters can target other CLIs without rewriting the registry, batch manifests, or lifecycle commands.

Keep provider-specific logic inside the adapter layer: option validation, launch command construction, session detection, and resume command generation. Read `references/provider-adapters.md` when you need to extend the script beyond Codex.

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

This creates `.agent-subsessions/` in the current working directory unless a legacy `.codex-subsessions/` directory already exists.

### 2. Dispatch a child run

Use a direct prompt:

```bash
python3 scripts/codex_subsession_manager.py dispatch \
  --provider codex \
  --name api-audit \
  --full-auto \
  --sandbox read-only \
  --prompt "Audit the API layer for auth and caching risks. Do not edit files. Return findings only."
```

Use a prompt file for longer instructions:

```bash
python3 scripts/codex_subsession_manager.py dispatch \
  --provider codex \
  --name ui-refactor \
  --full-auto \
  --sandbox workspace-write \
  --prompt-file /tmp/ui-refactor-prompt.md
```

Each run gets its own directory under `.agent-subsessions/runs/`.

For project-managed work, attach the run to a project and task id:

```bash
python3 scripts/codex_subsession_manager.py dispatch \
  --provider codex \
  --project payments-migration \
  --task-id review-api \
  --role reviewer \
  --owned-path services/api \
  --depends-on plan-approved \
  --name api-review \
  --full-auto \
  --sandbox read-only \
  --prompt-file /tmp/api-review-prompt.md
```

That project link is what enables automatic markdown aggregation and progress visualization.

### 3. Track progress

```bash
python3 scripts/codex_subsession_manager.py status
python3 scripts/codex_subsession_manager.py show 20260324-120000-ui-refactor
python3 scripts/codex_subsession_manager.py tail 20260324-120000-ui-refactor
```

`status` refreshes run metadata, including completion state and detected session IDs.

For project-linked runs, the manager also updates:

- `projects/<project>/dashboard.md`
- `projects/<project>/tasks.md`
- `projects/<project>/manager-summary.md`
- `projects/<project>/questions.md`
- `projects/<project>/conflicts.md`
- `projects/<project>/reports/<run-id>.md`

### 4. Resume the child later

Print a ready-to-run resume command:

```bash
python3 scripts/codex_subsession_manager.py resume-cmd 20260324-120000-ui-refactor
```

This prints an interactive provider resume command anchored to the original working directory. For the current Codex adapter, that command is `codex resume <thread-id>`.

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

Read `references/prompt-patterns.md` when you need prompt templates for research, implementation, reviewer, or manager-style child sessions. Read `references/project-workspaces.md` when you want the central-folder workflow and automatic markdown collection behavior. Use `python3 scripts/codex_subsession_manager.py providers` to inspect the adapters currently available in the script, or `providers --json` when you need machine-readable provider capability details.

## Prompting Guidance

Every child prompt should include:

- Objective: the concrete outcome
- Scope: exact files, modules, or investigation area
- Write boundary: whether the child may edit files, and if so which ones
- Validation: tests, checks, or evidence expectations
- Return contract: what summary or artifact the child must produce

When running multiple writers in parallel, assign disjoint file ownership. If that is not possible, turn some children into read-only researchers or reviewers instead of concurrent editors.

Populate `--owned-path` when a child is allowed to write. The project workspace uses those paths to flag conflict risk in `conflicts.md`.

## Operational Guidance

- Prefer `--sandbox read-only` for research-only children
- Prefer `--sandbox workspace-write --full-auto` for normal autonomous implementation children
- Prefer linking every meaningful child to `--project`, `--task-id`, and `--role` so the markdown dashboard remains useful
- Use `--add-dir` for extra writable paths outside the main repo root
- Use `--skip-git-repo-check` if a child must run outside a Git repository
- Use `attach-session` if auto-detection misses a session ID and you need a stable resume handle
- Use `attach-thread` as a Codex-specific compatibility alias
- Use `reconcile` to backfill session IDs after runs finish
- When adding another CLI later, preserve the registry and run commands; only add a new adapter and keep provider branching out of the shared lifecycle code
- Use the project markdown files as the manager-facing UI: `dashboard.md` for progress, `tasks.md` for assignment state, `questions.md` for escalation, and `manager-summary.md` for the latest aggregate snapshot

## Guardrails

- Each child session consumes normal Codex usage. Use only as many concurrent children as the task justifies.
- Do not run concurrent writers against the same files unless the user explicitly accepts merge risk.
- `conflicts.md` reports overlap risk from declared ownership. It does not automatically merge conflicting file edits.
- This skill manages child provider sessions, not arbitrary background jobs. Keep the current workflow centered on real CLI session primitives rather than custom task shims.
