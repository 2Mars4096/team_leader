# Team Leader

A Codex skill and standalone controller that manages real child CLI sessions as a team-leader style orchestrator. It launches, tracks, reviews, and aggregates parallel workers through project dashboards, dependency-aware dispatch, and automatic markdown workspaces.

Unlike lightweight built-in subagents, each child session is a full external CLI session with its own context, tool access, and resume lifecycle.

## Skills Included

| Skill | Purpose |
|-------|---------|
| `team-leader` | Planning, dispatch, monitoring, and aggregation of child CLI sessions |
| `team-status` | Compact progress view for monitoring without opening markdown files |

## Prerequisites

- **Python 3.10+** (stdlib only, no pip packages required)
- At least one supported child CLI installed and configured: `codex`, `claude`, `cursor-agent`, or `kiro-cli`
- **Git** (for worktree-based writer isolation)

## Supported Providers

- `codex` -- native `exec` / `resume` adapter with session IDs and backend reachability checks
- `claude` -- headless `claude -p` adapter with `claude -r <session-id>` resume
- `cursor` -- headless `cursor-agent -p` adapter with `--resume <session-id>`
- `kiro` -- headless `kiro-cli chat --no-interactive` adapter with directory-scoped `--resume`

Common aliases are accepted anywhere a provider name is expected: `cc` or `claude-code` for `claude`, `cursor-agent` for `cursor`, `kiro-cli` for `kiro`, and `codex-cli` or `openai-codex` for `codex`.

`windsurf` and `antigravity` are not shipped as provider adapters yet. This controller only first-classes CLIs with a documented standalone headless launch surface and a resume story the manager can automate safely.

## Installation

### Install into Codex (recommended)

From inside a Codex session, ask Codex to install the skill:

```
Install the team-leader skill from 2Mars4096/team_leader
```

Codex uses its built-in skill installer to run:

```bash
install-skill-from-github.py --repo 2Mars4096/team_leader --path skills/team-leader
```

To also install the monitoring-only companion skill:

```bash
install-skill-from-github.py --repo 2Mars4096/team_leader --path skills/team-leader skills/team-status
```

Skills are installed into `~/.codex/skills/team-leader/` (and `~/.codex/skills/team-status/`). Restart Codex after installation to pick up new skills.

### Manual installation

Clone the repo and copy the skill directories:

```bash
git clone git@github.com:2Mars4096/team_leader.git
cp -r team_leader/skills/team-leader ~/.codex/skills/team-leader
cp -r team_leader/skills/team-status ~/.codex/skills/team-status
```

## Quick Start

### Using inside Codex CLI (recommended)

Once installed, the `$team-leader` skill is available in any Codex session. Tell Codex what you want built and ask it to use the skill:

```
Use $team-leader to refactor the checkout flow. The repo is at /path/to/repo.
Run tests with "pytest -q" to validate.
```

Codex reads the skill instructions and drives the controller automatically: it runs `intake` to capture the brief, `orchestrate` to plan and dispatch workers, and monitors progress through `status` and `team-status`. The manager can stay in Codex while child runs use another provider such as `claude`, `cursor`, or `kiro`.

For monitoring only, use the companion skill:

```
Use $team-status to check progress on the checkout-refactor project.
```

### Using the controller script directly

From your **target project directory** (not the skill directory):

```bash
# 1. Record the project goal
python3 ~/.codex/skills/team-leader/scripts/team_leader.py intake \
  --project my-project \
  --goal "Refactor checkout to reduce payment failures" \
  --repo-path . \
  --child-provider claude \
  --allow-provider codex \
  --allow-provider claude \
  --validation-command "pytest -q"

# 2. Validate provider binaries before launch
python3 ~/.codex/skills/team-leader/scripts/team_leader.py provider-check \
  --provider codex \
  --provider claude

# 2b. Optional: verify one provider end to end
python3 ~/.codex/skills/team-leader/scripts/team_leader.py provider-smoke-test \
  --provider claude \
  --timeout 30

# 3. Plan and dispatch workers
python3 ~/.codex/skills/team-leader/scripts/team_leader.py orchestrate \
  --project my-project

# 4. Check progress
python3 ~/.codex/skills/team-leader/scripts/team_leader.py status --project my-project

# 5. Watch live updates
python3 ~/.codex/skills/team-leader/scripts/team_leader.py team-status --project my-project
```

> **Important:** Always run the controller from the target project directory. The `.team-leader/` state directory is created relative to the current working directory. Do not `cd` into the skill directory to run the script.

## Repository Structure

```
skills/
├── team-leader/
│   ├── SKILL.md                          # Skill instructions for Codex
│   ├── agents/openai.yaml                # Skill UI metadata
│   ├── scripts/
│   │   ├── team_leader.py                # Main controller (~5k lines, stdlib only)
│   │   └── codex_subsession_manager.py   # Compatibility wrapper
│   └── references/
│       ├── example_manifest.json         # Batch dispatch example
│       ├── provider-adapters.md          # Guide for adding CLI adapters
│       ├── project-workspaces.md         # Workspace layout documentation
│       └── prompt-patterns.md            # Prompt templates for child sessions
└── team-status/
    ├── SKILL.md                          # Monitoring-only skill instructions
    └── agents/openai.yaml
```

**Runtime state** (created per-project, not shipped):

```
.team-leader/                  # Controller state root (in the target project)
├── runs/                      # Per-run directories with prompts, logs, PIDs
└── projects/<project>/        # Per-project markdown workspace
    ├── README.md              # Landing page
    ├── brief.md               # Goal, repo paths, specs, constraints
    ├── launch-plan.md         # Planner-produced child session plan
    ├── dashboard.md           # Live run progress and child notes
    ├── tasks.md               # Assignment state and summaries
    ├── validation.md          # Validation results and delivery status
    ├── metrics.md             # Efficiency scorecard
    ├── manager-summary.md     # Aggregated manager report
    ├── questions.md           # Questions needing human answers
    ├── answers.md             # Human-edited answers (only file for manual editing)
    ├── answers-template.md    # Copy-ready answer lines
    ├── conflicts.md           # Overlap risk between writers
    └── reports/<run-id>.md    # One report per child session
```

## CLI Reference

All commands use `python3 <path-to>/team_leader.py <command> [options]`.

| Command | Description |
|---------|-------------|
| `init` | Create the `.team-leader/` state directory |
| `intake` | Record or update a project brief |
| `orchestrate` | Launch planner and auto-dispatch workers from the brief |
| `dispatch` | Launch a single child session |
| `batch` | Launch multiple children from a JSON manifest |
| `status` | Show tracked runs and project summary |
| `team-status` | Compact milestone-style progress updates |
| `team-metrics` | Efficiency scorecard (age, speed, human-touch, overlap) |
| `watch` | Live terminal view with auto-refresh |
| `show` | Show details and last message for one run |
| `tail` | Tail stdout/stderr for a run |
| `resume-cmd` | Print a resume command for a child session |
| `cancel` | Stop a child session (`--force` for SIGKILL) |
| `reconcile` | Refresh status and backfill session IDs |
| `attach-session` | Manually attach a session ID to a run |
| `providers` | List available CLI adapters |
| `provider-check` | Validate provider executable paths and basic CLI readiness |
| `provider-smoke-test` | Launch one real child run and wait for an end-to-end result |

`provider-check` exits non-zero when any requested provider is blocked, so it can gate shell scripts and CI preflight cleanly.

### Key options

- `--project <name>` -- link runs to a project workspace
- `--provider <name>` -- provider for one direct run or for the planner child
- `--provider-bin <path>` -- executable override for that provider
- `--planner-provider <name>` -- persist the planner provider in `brief.md`
- `--child-provider <name>` -- default provider for planner-produced child runs
- `--child-provider-bin <path>` -- executable override for the default child provider
- `--allow-provider <name>` -- constrain planner output to a provider allowlist
- `--sandbox read-only|workspace-write` -- child sandbox mode
- `--full-auto` -- run child in full-auto mode
- `--root <path>` -- explicit `.team-leader/` path (default: `./.team-leader`)
- `--cd <path>` -- working directory for child sessions
- `--depends-on <task-id>` -- hold a task until prerequisites complete
- `--owned-path <path>` -- declare file ownership for conflict detection
- `--dry-run` -- preview without launching

## Workflow

### Manager-first flow (recommended)

When the user provides only a goal and context:

1. **`intake`** -- capture the project brief (goal, repo paths, specs, constraints)
2. **`orchestrate`** -- launch a planner child that produces a task plan, then auto-dispatch workers
3. **Monitor** -- use `status --project` or `team-status --project` to track progress
4. **Answer questions** -- check `questions.md`, edit `answers.md` with responses
5. **Re-orchestrate** -- run `orchestrate` again if the planner needs another round after new answers

### Autonomy modes

| Mode | Behavior |
|------|----------|
| `manual` | You explicitly run `orchestrate` each time |
| `guided` | Manager runs validation and tracks delivery, but won't auto-start new planner waves |
| `continuous` | Manager auto-starts planner waves and pushes until validation passes or limits are reached |

### Clarification modes

| Mode | Behavior |
|------|----------|
| `auto` | Planner may ask targeted questions before launching workers |
| `off` | Skip clarification and plan immediately |

### Recovery limits

- `--max-auto-fix-rounds` -- caps how many validation-failure recovery waves the manager launches automatically in `continuous` mode
- `--max-planner-rounds` -- caps how many planner iterations are allowed

## Safety Defaults

- Max **8** concurrent child sessions
- Max **2** new launches per **15** seconds
- Oversized child `last_message.md` files are truncated with head/tail preservation
- `team-status` caps output in captured environments by default
- Non-TTY `watch` falls back to a single snapshot unless explicitly allowed to stream

## Architecture Notes

The controller keeps provider-specific behavior at the adapter boundary: option validation, command construction, session-ID detection, and resume command generation. The run registry and batch manifest format remain stable when adding a new adapter for another CLI (e.g., `claude`, `cursor`).

Provider choice can vary per child run. Planner output may now set `provider` and `provider_bin` for each task, so a Codex manager can launch Claude reviewers, Cursor writers, and Kiro researchers in the same project as long as their CLIs are installed and pass `provider-check`.

All shipped providers now sit on the same adapter contract. Codex still keeps its provider-specific hooks for backend reachability and thread detection so `codex -> codex` preserves the prior behavior while mixed-provider projects stay possible.

Writer children in Git repos are isolated into per-run worktrees. The manager integrates completed work through a project integration worktree before validation runs.

The project workspace (`.team-leader/projects/<project>/`) is persistent state. Reusing the same project name reuses the same folder and history. The only file intended for direct human editing is `answers.md`. For a clean restart, use a new project name.
