# Natural-language workflows (Codex only)

Team Leader compiles natural language into explicit control flow, then executes it through the existing Codex runner. The controller owns iteration state and scheduling. A logical step is a separate recorded run, but related runs can resume the same Codex conversation.

## Run from natural language

```bash
python3 skills/team-leader/scripts/team_leader.py workflow start \
  --name model-review --cd /path/to/project \
  --max-workers 3 \
  --pool-file skills/team-leader/references/workflow-pool.example.json \
  --prompt 'Discover the parts and available reference views. For each part, establish its defining features. For each relevant perspective, compare contours, lines and surfaces against the reference; if discrepancies are supported, revise and verify affected views. Finally review the whole model. Use reviewer for comparisons and editor for revisions. Do not invent missing reference views.'
```

The planner is a read-only Codex run. Its output must pass structural validation before execution. It may discover actual loop collections in preparatory steps. Missing references and uncertain predicates block execution. The runtime never evaluates generated Python or shell control-flow expressions.

The original instruction, compiled workflow, loop scopes, results and session identities live in the controller index. A readable summary and compiled JSON are also written under `.team-leader/workflows/<name>/`. Inspect these plus the ordinary run logs to understand what was compiled and executed.

## Configure the worker pool

`--max-workers N` limits outstanding worker executions for this workflow, including queued work. The controller's existing root-wide concurrency cap and launch throttle also apply; the effective limit is the stricter cap. Set `TEAM_LEADER_MAX_PARALLEL_SESSIONS` to change that root-wide cap (default 8).

`--pool-file` accepts a JSON object keyed by worker type. Types are task roles/instruction presets, all backed by Codex. Each entry supports:

| Field | Meaning | Default |
|---|---|---|
| `model` | Exact Codex model identifier | `--model`, otherwise CLI configuration |
| `role` | Human-readable role | Type name |
| `instructions` | Instructions added to each scoped prompt | Empty |
| `sandbox` | `read-only` or `workspace-write` | `read-only` |
| `max_parallel` | Outstanding executions of this type | 1 |
| `max_session_steps` | Start a fresh session after this many related steps | 12 |
| `max_run_seconds` | Hard per-step timeout | 900 |

`--model` sets the fallback for types without an explicit model. `--planner-model` overrides the compiler model independently. No model availability or pricing assumptions are built in. `--codex-bin` selects the executable. Without a pool file, one `default` worker type is created with the workflow's pool limit.

Example configuration with user-selected models:

```json
{
  "reviewer": {"model": "YOUR_REVIEW_MODEL", "max_parallel": 2},
  "editor": {"model": "YOUR_EDIT_MODEL", "sandbox": "workspace-write", "max_parallel": 1}
}
```

## Control-flow format

A workflow is `{"inputs": {...}, "body": NODE}`. The supported nodes are:

- `do`: `prompt`, optional `worker`, `save_as`, `session`, and `locks`.
- `sequence`: ordered `steps`.
- `for_each`: `var`, `items`, `body`, optional `parallel` (default false) and `save_as`.
- `if`: `condition`, `then`, optional `else`.
- `repeat_until`: `body`, `condition`, positive `max_iterations`. Executes at least once.

`items` is a literal list or `{"ref":"discovery.data.parts"}`. Conditions are `{"ref":"check.verdict","equals":"pass"}`. Prompts, session keys and resource locks interpolate `{{part}}` or dotted paths such as `{{check.evidence}}`.

A worker returns exactly:

```json
{"verdict":"pass","evidence":"Specific measurements, checked version and artifact paths","data":{}}
```

Verdicts are `pass`, `fail`, or `uncertain`. Exit code zero alone does not satisfy the return contract. An `uncertain` result blocks, and an uncertain predicate never selects a false branch. A failed predicate at the repeat limit also blocks. A `fail` result can intentionally feed a repair branch; workflow completion means the declared control flow finished, so include an explicit final acceptance check/branch when quality acceptance is required.

Each `for_each` iteration gets its own environment, and inner steps can read outer variables. A loop's `save_as` collects completed iteration environments for later synthesis. `sequence`, `if`, and `repeat_until` share the enclosing environment. `iteration` is the repeat's 1-based counter. To avoid shadowing, use distinct variable/result names for nested constructs.

`session` groups related steps within the same worker type and workflow. Only one execution uses a session at a time. At the configured step limit the next task starts fresh with saved scoped inputs/results. Session count and execution count are distinct: 60 checks can reuse a small number of conversations, but still require 60 executions.

## Scheduling, restart and writes

Loops expand lazily within a pool-sized window. Results and run identities are persisted with atomic index replacement. Restarting adopts already registered runs and skips accepted steps. Resume does not automatically rerun failed workers or repeat limits; inspect the failed run and repair its underlying issue. There is no automatic retry of potentially side-effecting work.

`locks` serialize named resources. Within a workflow, writers run exclusively against readers and other writers, using the supplied working directory. This makes later comparisons observe earlier edits and avoids trying to merge binary models. Workflow writers do not use the project planner's Git integration worktrees. Use a dedicated checkout for a workflow; unrelated manual edits or separate workflows in the same directory are outside its scheduling boundary. Evidence should identify the exact asset version assessed; version freshness is currently a worker responsibility rather than an automatic geometry/hash validator.

```bash
python3 skills/team-leader/scripts/team_leader.py workflow status --name model-review
python3 skills/team-leader/scripts/team_leader.py workflow pause --name model-review
python3 skills/team-leader/scripts/team_leader.py workflow resume --name model-review
```

Pause prevents new launches; in-flight steps finish. Resume continues from saved state. Ordinary `status`, `show`, `tail`, and `cancel` remain available for individual runs. Workflow runs are retained by cleanup so the execution ledger remains recoverable.

## Small runnable example

The example runs four synthetic comparisons, two at a time, and reuses one conversation per part. It does not need a model asset or claim to perform a real visual comparison.

```bash
python3 skills/team-leader/scripts/team_leader.py workflow start \
  --name nested-example --max-workers 2 \
  --pool-file skills/team-leader/references/workflow-pool.example.json \
  --file skills/team-leader/references/workflow.example.json
```

Add `--dry-run` to validate and preview without launching a compiler or workers. With natural-language input, dry-run previews the compiler configuration; compilation itself requires a Codex execution.
