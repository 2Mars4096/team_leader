# Cost-aware multi-model orchestration

Use this mode only when the user asks for external model delegation, model
assignment, or parallel model work. Codex remains the planner, permission
boundary, verifier, and final synthesizer.

## Decomposition rules

- Create tasks with one testable outcome and enough context to work without
  filesystem access.
- Parallelize only tasks that do not consume one another's outputs. Execute
  dependencies in later waves.
- Use the narrowest profile that matches the task: `pdf-reading`,
  `code-generation`, `code-review`, `agent-orchestration`, `web-research`,
  `reasoning`, `data-analysis`, `writing`, `translation`, or `general`.
- Set difficulty to `simple`, `standard`, `complex`, or `critical`. Higher
  difficulty narrows the acceptable quality band before cost is minimized.
- Preserve an exact user-designated model with the task's `model` field.
- Put only necessary instructions and excerpts in each prompt file. Never send
  raw leaderboard data, credentials, unrelated repository content, or private
  files outside the user's authorized scope.

## Manifest contract

```json
{
  "objective": "One-sentence overall outcome",
  "strategy": "balanced",
  "max_parallel": 4,
  "tasks": [
    {
      "id": "bounded-task-id",
      "title": "Human-readable task",
      "profile": "code-generation",
      "difficulty": "standard",
      "prompt_file": "prompts/bounded-task-id.txt",
      "pdfs": [],
      "requirements": [],
      "reasoning_effort": "medium",
      "estimated_input_tokens": 8000,
      "max_output_tokens": 4000,
      "max_estimated_cost_usd": 0.10
    }
  ]
}
```

`strategy` may be `economy`, `balanced`, or `quality`. For fixed source data,
the planner is deterministic. It first establishes a capability floor from the
best task-specific quality score, adjusted by difficulty and strategy, then
selects the least expensive model above that floor. Estimated cost assumes the
full input estimate and maximum output allocation; actual billing may differ.

Local PDFs may be listed under `pdfs`. OpenRouter's parser can make a PDF
available to a model even when the catalog does not advertise native file
input, so request `files` compatibility only when native file handling is a
hard requirement.

## Execution and review

`run_workers.py` defaults to a no-cost dry run. Paid execution requires all of:

- `--execute`;
- `OPENROUTER_API_KEY` in the process environment;
- `--max-estimated-cost-usd` at least as large as the plan estimate; and
- an explicit `--output-dir`.

The runner uses bounded concurrency, does not retry failed calls, and writes
results in manifest order after workers finish. A failure in one worker does
not authorize rerunning it automatically because retries can incur additional
cost. Codex reviews successful outputs, checks important claims against source
material or local tests, and decides whether a later wave is warranted.
