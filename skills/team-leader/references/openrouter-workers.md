# Optional OpenRouter workers

Use only when the user asks for third-party workers. Native Codex/OpenAI remains
the default. The API runner returns text/code proposals; Codex owns planning,
tool use, edits, tests, follow-up waves, and the final answer.

The model-intelligence implementation is bundled under
`scripts/model_intelligence/`, imported from the user's local custom skill.
It includes task-specific cached model selection and parallel API execution.
It does not include private caches, credentials, or raw benchmark snapshots.

## Configuration

Commands below run from the target project. Resolve `scripts/team_leader.py`
relative to the installed team-leader skill (in this repository,
`skills/team-leader/scripts/team_leader.py`).

Team-leader automatically loads `~/.config/team-leader/.env` from every project.
This is the shared credential file on this Mac; do not ask the user for a key or
credential path before trying the controller with its defaults. Keep credentials
outside the installed skill so reinstalling the skill preserves them.

Set `OPENROUTER_API_KEY='your-key'` in that shared file. An explicit
`openrouter --env-file PATH` selects a different file. Nonempty values from the
selected file override inherited environment values; missing or empty settings
fall back to the environment. Project `.env` files are not discovered implicitly.
Files are parsed as literal assignments, never executed as shell code. Only the
allowlisted model-intelligence settings are loaded. Credentials never select the
worker provider automatically.

## Plan from cached evidence

Decompose work into independent tasks with explicit excerpts in UTF-8 prompt
files. External workers cannot discover files from path names. Send only
user-authorized context, never credentials or unrelated repository content.

Create a manifest, for example:

```json
{
  "objective": "Review a proposed implementation",
  "strategy": "economy",
  "max_parallel": 2,
  "tasks": [{
    "id": "review",
    "title": "Review the supplied code",
    "profile": "code-review",
    "difficulty": "standard",
    "prompt_file": "prompts/review.txt",
    "estimated_input_tokens": 8000,
    "max_output_tokens": 2000,
    "max_estimated_cost_usd": 0.10
  }]
}
```

The default OpenRouter model is `deepseek/deepseek-v4.1-flash`. Selection precedence
is task `model`, manifest `default_model`, `MODEL_INTELLIGENCE_OPENROUTER_MODEL`
from the shared/explicit `.env` file or environment, then this bundled default. Set manifest
`default_model` to `auto` to select priced models above a task-specific quality
floor instead. Explicit task models still take precedence. `economy`,
`balanced`, and `quality` control selection. Prompt paths are relative to the
manifest. Dependent tasks belong in later waves after Codex reviews results.

```bash
python3 scripts/team_leader.py openrouter plan manifest.json --output assigned.json
python3 scripts/team_leader.py openrouter run assigned.json
```

The default cache is `.team-leader/model-intelligence`; `--cache-dir PATH` before
the action or `MODEL_INTELLIGENCE_DATA_DIR` selects an existing cache. For this
user's existing cache, that path is `~/.codex/skills/model-intelligence/data`.
Do not inspect raw snapshots as model context. Query compact evidence with
`openrouter query --task code-review --openrouter-only --format context`.
Disclose the evidence date and stale/sparse evidence when selecting models.

If the cache is missing, bootstrap it explicitly with
`openrouter refresh --all`. This retrieves public model/benchmark metadata;
optional authenticated sources use environment credentials. Refreshes do not
call inference models. For an existing cache, refresh separately from an
in-progress recommendation so selection uses a consistent snapshot.

## Execute and review

Show task/model assignments and estimated costs before paid execution unless
the user's existing instruction already authorizes them. Then:

```bash
python3 scripts/team_leader.py openrouter run assigned.json \
  --execute --max-estimated-cost-usd 0.10 \
  --output-dir .team-leader/openrouter/review-wave
```

Execution is opt-in; `run` without `--execute` makes no API calls. The runner
checks the plan estimate against the supplied limit, caps concurrency and output
tokens, and does not retry failures. This is an estimate gate, not a guaranteed
billing cap: input estimates, changed prices, reasoning and PDF parsing charges
can affect actual costs. Use a provider-side credit limit for a billing cap.

Read `run.json` and per-task result JSON, check successful outputs against local
evidence/tests, and integrate in Codex. Retain failure information and decide
explicitly whether another paid wave is useful. Store each wave in a distinct
output directory. Results are separate from CLI session dashboards and cannot
be resumed with `resume-cmd`.

The nested [orchestration reference](../scripts/model_intelligence/references/orchestration.md)
documents additional manifest fields; the
[delegation reference](../scripts/model_intelligence/references/delegation.md)
covers PDF inputs and the external worker boundary. API contract:
[OpenRouter API reference](https://openrouter.ai/docs/api_reference/overview).
