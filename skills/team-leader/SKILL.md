---
name: team-leader
description: Use when a user wants a team-leader style Codex manager that launches, tracks, reviews, and aggregates real child CLI sessions, especially when each child should keep full external CLI capabilities rather than acting like a lightweight built-in subagent.
---

# Team Leader

## Overview

This skill manages real child sessions through a provider adapter layer. The controller now ships verified adapters for `codex`, `claude`, `cursor`, and `kiro`.

Treat a subsession as a full child CLI worker with its own session, context window, tool use, and follow-up lifecycle. This is more flexible than a lightweight in-process subagent because a child session can keep working independently, be resumed later, and itself act as a manager when useful.

Use the control script at `scripts/team_leader.py` instead of ad hoc shell fragments. This path is relative to the skill itself, not the project root. In this repo that file is at `skills/team-leader/scripts/team_leader.py`, and when installed it lives under the Codex skills directory at `.../skills/team-leader/scripts/team_leader.py`. Keep your working directory at the target project unless you pass `--root` and `--cd` explicitly; do not `cd` into the skill directory just to run the controller, because the default `.team-leader/` root is derived from the current working directory. A compatibility wrapper remains at `scripts/codex_subsession_manager.py`, but the primary interface is now `team_leader.py`. The controller stores a local `.team-leader/` registry with prompts, commands, logs, last messages, PIDs, and detected session IDs. Older `.agent-subsessions/` and `.codex-subsessions/` directories are still recognized automatically.

When runs are linked to a project, the script also maintains a central markdown workspace under `.team-leader/projects/<project>/` with a default `README.md` landing page, a project brief, the latest planner launch plan, validation status, a live dashboard, task ledger, manager summary, questions for humans, a human-edited answers file, conflict-risk notes, and one child report per run. Writer runs inside Git repos are isolated into per-run worktrees, and the manager integrates them through a project integration worktree before validation runs. While any child is active, the manager refreshes those markdown files automatically in the background. Once a project settles cleanly, the manager compacts transient dashboards, question scratchpads, per-run reports, and disposable child-run artifacts into a smaller steady-state workspace.

The controller now includes conservative safety defaults aimed at avoiding runaway resource use:

- at most `8` child sessions running in parallel by default
- at most `2` new child launches per `15` seconds by default
- child `last_message.md` files truncated to a bounded size with head/tail preservation
- bounded session-id scans and bounded log tail reads
- `team-status --project` gives a compact progressive update stream that is safer in captured Codex output than a full-screen watch
- non-TTY `watch` falls back to a single snapshot unless explicitly told to stream

That project workspace is persistent state, not a temp folder. Reusing the same project name reuses the same folder and tracked history. In normal continuation, do not delete the generated markdown files by hand. The only file intended for direct human edits is `answers.md`. For a clean restart, use a new project name. If you need to compact failed or standalone runs on demand, use `python3 scripts/team_leader.py cleanup`.

Shipped providers today:

- `codex`
- `claude`
- `cursor`
- `kiro`

Common aliases are accepted anywhere the controller asks for a provider name, including `cc` or `claude-code` for `claude`, `cursor-agent` for `cursor`, `kiro-cli` for `kiro`, and `codex-cli` or `openai-codex` for `codex`.

`windsurf` and `antigravity` are not shipped adapters yet. This controller only first-classes CLIs with a documented standalone headless launch surface and an automatable resume workflow.

Keep provider-specific logic inside the adapter layer: option validation, launch command construction, session detection, and resume command generation. All shipped providers now use the same adapter contract, while Codex keeps provider-specific hooks for backend reachability and thread detection so `codex -> codex` stays behaviorally aligned with the prior flow. Read `references/provider-adapters.md` when you need to extend the script beyond Codex.

## When To Use

- Launch one or more long-running child CLI sessions from a manager session
- Split work into independent child-run tracks that may later be resumed interactively
- Run research, implementation, review, or audit workers that need full provider behavior
- Dispatch children in the background and poll status while continuing work in the manager
- Cancel or manually reattach to a child session when the work changes

## Core Difference From Built-In Subagents

Built-in subagents are lightweight delegated helpers inside the current orchestration model.

Subsessions in this skill are separate provider sessions:

- They run through the normal headless/resume surface for the selected provider
- They can use the same tools and workflows that provider would normally expose
- They can persist their own thread and be resumed later
- They can be treated as independent managers rather than one-shot helpers

## Workflow

### 1. Initialize the controller directory

```bash
python3 scripts/team_leader.py init
```

Run `init` from the target project directory. This creates `.team-leader/` in the current working directory unless an older compatible root already exists. If you must invoke the controller by an absolute skill path from somewhere else, pass `--root /path/to/project/.team-leader` explicitly.

### 2. Record a project goal and context

When the user can only give you the project goal, a few constraints, and some paths, start here:

```bash
python3 scripts/team_leader.py intake \
  --project checkout-refactor \
  --goal "Refactor checkout to simplify the flow and reduce payment failures." \
  --repo-path /path/to/repo \
  --child-provider claude \
  --allow-provider codex \
  --allow-provider claude \
  --spec-path /path/to/spec.md \
  --autonomy-mode guided \
  --validation-command "pytest -q" \
  --completion-sentinel DELIVERY_COMPLETE \
  --constraint "Keep the current API stable for mobile clients" \
  --note "The user can answer a few human questions, but does not want to hand-author worker tasks."
```

Run `intake` from the target project directory, or pass `--root` explicitly. That creates or updates `.team-leader/projects/<project>/brief.md`.

Autonomy modes:

- `manual`: you explicitly run `orchestrate`
- `guided`: the manager runs validation commands and tracks delivery status, but does not auto-start new planner waves
- `continuous`: the manager may auto-start planner waves from the brief and keep pushing until validation/completion checks pass or the planner-round limit is reached

Clarification modes:

- `auto`: the planner may ask a few targeted human questions before launching workers
- `off`: skip that gate and plan immediately

Recovery limits:

- `max_auto_fix_rounds`: caps how many validation-failure recovery waves the manager may launch on its own in `continuous` mode

### 3. Let the manager plan the child sessions

```bash
python3 scripts/team_leader.py orchestrate \
  --project checkout-refactor
```

`orchestrate` launches a manager-planner child when needed. That planner inspects `brief.md`, the repo paths, and any spec paths, then either asks a concise clarification round or emits a machine-readable launch plan. The manager parses that plan and auto-dispatches child sessions from it. Planner output can now choose `provider` and `provider_bin` per child run, subject to the project allowlist and default child provider stored in the brief. If the planner raises human questions, they land in `questions.md` and `answers-template.md`.

### 4. Dispatch a child run directly when needed

Use a direct prompt:

```bash
python3 scripts/team_leader.py dispatch \
  --provider codex \
  --name api-audit \
  --summary "Audit the API layer for auth and caching risks" \
  --full-auto \
  --sandbox read-only \
  --prompt "Audit the API layer for auth and caching risks. Do not edit files. Return findings only."
```

Use a prompt file for longer instructions:

```bash
python3 scripts/team_leader.py dispatch \
  --provider codex \
  --name ui-refactor \
  --summary "Refactor the UI flow for the checkout screen" \
  --full-auto \
  --sandbox workspace-write \
  --prompt-file /tmp/ui-refactor-prompt.md
```

Each run gets its own directory under `.team-leader/runs/`.

For project-managed work, attach the run to a project and task id:

```bash
python3 scripts/team_leader.py dispatch \
  --provider codex \
  --project payments-migration \
  --task-id review-api \
  --summary "Review API changes before implementation starts" \
  --role reviewer \
  --owned-path services/api \
  --depends-on plan-approved \
  --name api-review \
  --full-auto \
  --sandbox read-only \
  --prompt-file /tmp/api-review-prompt.md
```

That project link is what enables automatic markdown aggregation, progress visualization, and dependency-aware next-wave launching.

When you want a Codex manager to launch another CLI as the child, validate it first:

```bash
python3 scripts/team_leader.py provider-check --provider claude --provider cursor --provider kiro
```

`provider-check` exits non-zero when any requested provider is blocked, which makes it suitable for shell gating before a mixed-provider launch.

When you want to verify one provider can complete a real manager-launched child run, use:

```bash
python3 scripts/team_leader.py provider-smoke-test --provider claude --timeout 30
```

This command reuses the normal dispatch path, waits for the child to settle, and checks that the final `last_message` matches the expected text. Keep the generated temp root when it fails so you can inspect stdout, stderr, and the captured session ID.

For writer children in Git repos, the manager now isolates each writer into its own worktree and integrates completed changes through the project integration worktree. Overlapping writers are serialized by the manager instead of writing into the same checkout at the same time. Once a writer has been integrated cleanly or produces no diff, its per-run worktree is released automatically.

### 5. Track progress

```bash
python3 scripts/team_leader.py status
python3 scripts/team_leader.py show 20260324-120000-ui-refactor
python3 scripts/team_leader.py tail 20260324-120000-ui-refactor
```

`status` refreshes run metadata, including completion state and detected session IDs. The project markdown workspace also refreshes automatically while children are running, so you do not have to poll manually just to keep `dashboard.md` current. After a project settles and is compacted, the reported detail path may point to `history.md` instead of `dashboard.md`.

For a Codex-native view without opening folders, prefer:

```bash
python3 scripts/team_leader.py status --project payments-migration
```

That prints the current stage, stage reason, next action, current focus, workspace path, landing page path, dashboard path, watcher state, active runs, blocked runs, open questions, recent answers, conflict hints, and integration state directly in the terminal.

For progressive feedback with compact live updates, use:

```bash
python3 scripts/team_leader.py team-status --project payments-migration
```

That now defaults to milestone-style updates: stage transitions, child started/completed changes, new child notes, newly opened questions, conflicts, integration alerts, and warning changes. In captured environments, it caps itself by default instead of streaming forever.

If you want the fuller compact summary instead, add `--full`:

```bash
python3 scripts/team_leader.py team-status --project payments-migration --full --exit-when-settled
```

For a concise scorecard of whether the manager is actually helping, use:

```bash
python3 scripts/team_leader.py team-metrics --project payments-migration
```

That prints descriptive metrics for:

- project age
- time to first useful result
- time to validated completion
- human-touch count
- parallel overlap value
- stuck time from blocking, queuing, or other prelaunch delay

For a live terminal view:

```bash
python3 scripts/team_leader.py watch --project payments-migration
```

Use `--once` for a single render or `--exit-when-settled` when you want the view to stop after the project has no running or blocked runs. In captured or non-TTY environments like Codex terminal output, `watch` now defaults to a single snapshot unless you explicitly opt into streaming.

For project-linked runs, the manager also updates:

- `projects/<project>/README.md`
- `projects/<project>/brief.md`
- `projects/<project>/launch-plan.md`
- `projects/<project>/validation.md`
- `projects/<project>/metrics.md`
- `projects/<project>/dashboard.md`
- `projects/<project>/tasks.md`
- `projects/<project>/manager-summary.md`
- `projects/<project>/questions.md`
- `projects/<project>/answers.md`
- `projects/<project>/answers-template.md`
- `projects/<project>/conflicts.md`
- `projects/<project>/reports/<run-id>.md`

### 6. Resume the child later

Print a ready-to-run resume command:

```bash
python3 scripts/team_leader.py resume-cmd 20260324-120000-ui-refactor
```

This prints an interactive provider resume command anchored to the original working directory. Examples:

- `codex resume <thread-id>`
- `claude -r <session-id>`
- `cursor-agent --resume <session-id>`
- `kiro-cli chat --resume`

For non-interactive follow-up:

```bash
python3 scripts/team_leader.py resume-cmd 20260324-120000-ui-refactor --exec
```

### 7. Cancel when needed

```bash
python3 scripts/team_leader.py cancel 20260324-120000-ui-refactor
```

Use `--force` to send `SIGKILL` instead of `SIGTERM`.

## Batch Dispatch

For multiple children, create a JSON manifest and dispatch in one command:

```bash
python3 scripts/team_leader.py batch --file references/example_manifest.json --dry-run
```

Read `references/prompt-patterns.md` when you need prompt templates for research, implementation, reviewer, or manager-style child sessions. Read `references/project-workspaces.md` when you want the central-folder workflow and automatic markdown collection behavior. Use `python3 scripts/team_leader.py providers` to inspect the adapters currently available in the script, or `providers --json` when you need machine-readable provider capability details. Use `provider-check` before a mixed-CLI wave when you need to verify executable paths and basic CLI readiness.

## Recommended Manager-First Flow

When the user gives only the goal and a few pieces of context:

1. run `intake` to capture the project brief
2. run `orchestrate` to launch the manager-planner child
3. monitor with `status --project <project>`
4. answer anything in `questions.md`
5. rerun `orchestrate --project <project>` if the planner needs a fresh round after new human answers

That keeps the “who should do what?” decision inside the manager instead of forcing the user to enumerate child sessions manually.

If you want more self-driving behavior, set:

- `--autonomy-mode continuous`
- `--clarification-mode auto`
- one or more `--validation-command`
- optionally `--completion-sentinel`
- optionally `--max-planner-rounds`
- optionally `--max-auto-fix-rounds`

Then the manager can keep pushing toward delivery instead of stopping after one batch.

## Prompting Guidance

Every child prompt should include:

- Objective: the concrete outcome
- Summary title: one short line the manager can show in tables and dashboards
- Scope: exact files, modules, or investigation area
- Write boundary: whether the child may edit files, and if so which ones
- Validation: tests, checks, or evidence expectations
- Return contract: what summary or artifact the child must produce
- Human questions: tell the child to put unresolved decisions under a `Questions` or `Questions For Humans` heading when needed

When running multiple writers in parallel, assign disjoint file ownership. If that is not possible, turn some children into read-only researchers or reviewers instead of concurrent editors.

Populate `--owned-path` when a child is allowed to write. The project workspace uses those paths to flag conflict risk in `conflicts.md`.

## Operational Guidance

- Prefer `--sandbox read-only` for research-only children
- Prefer `--sandbox workspace-write --full-auto` for normal autonomous implementation children
- Prefer `intake` + `orchestrate` when the user has only a project goal, paths, and a few constraints
- Prefer `guided` or `continuous` autonomy only when you also have meaningful validation commands or a clear completion signal
- Prefer `clarification-mode auto` when the user gives only a goal and a few paths; let the planner ask the smallest useful question set first
- Prefer linking every meaningful child to `--project`, `--task-id`, `--summary`, and `--role` so the markdown dashboard remains useful
- Use `--depends-on` when a task should wait for another task to complete; the manager now keeps blocked tasks parked and launches them automatically when their prerequisites finish
- Prefer the new `watch --project <project>` view when you want live progress without opening markdown files
- Use `--add-dir` for extra writable paths outside the main repo root
- Use `--skip-git-repo-check` if a child must run outside a Git repository
- Use `attach-session` if auto-detection misses a session ID and you need a stable resume handle
- Use `attach-thread` as a Codex-specific compatibility alias
- Use `reconcile` to backfill session IDs after runs finish
- When adding another CLI later, preserve the registry and run commands; only add a new adapter and keep provider branching out of the shared lifecycle code
- Use `README.md` as the default landing page for a project workspace, then check `brief.md` for the captured goal, `launch-plan.md` for the latest manager plan, and `validation.md` for delivery status before moving into `dashboard.md`, `tasks.md`, `questions.md`, `answers.md`, and `manager-summary.md`

## Guardrails

- Each child session consumes normal Codex usage. Use only as many concurrent children as the task justifies.
- The manager now isolates Git-backed writers into separate worktrees and integrates them through the project integration worktree, but it still does not auto-resolve arbitrary merge conflicts.
- `conflicts.md` reports unresolved overlap or integration issues that still need manager or human judgment.
- This skill manages child provider sessions, not arbitrary background jobs. Keep the current workflow centered on real CLI session primitives rather than custom task shims.
