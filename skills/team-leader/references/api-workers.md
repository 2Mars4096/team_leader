# Third-party API workers

Use `api run` for explicit models on configurable inference endpoints. Supported
wire formats are OpenAI-compatible Chat Completions and Anthropic Messages.
This supports compatible hosted gateways and local inference servers; arbitrary
REST APIs, native Gemini, Responses-only endpoints, and cloud-specific signing
are outside this runner's contract. No SDK or model catalog is required.

The active manager can be Codex or Claude Code. Workers receive supplied text and
return proposals; the manager reviews results, edits files, and runs tests.
API workers have no tools, CLI session, dashboard integration, or resume support.
The separate `openrouter plan/run` commands retain cached model selection and PDF
handling. Use the generic runner when you already know the endpoint and model.

## Configuration and example

Create `prompts/review.txt` containing the context the worker should review, then
save this plan as `api-plan.json`:

```json
{
  "schema": "team-leader-api-plan/v1",
  "providers": {
    "gateway": {
      "protocol": "openai-compatible",
      "base_url": "https://your-provider.example/v1",
      "api_key_env": "VENDOR_API_KEY"
    }
  },
  "max_parallel": 2,
  "estimated_max_cost_usd": 0.05,
  "tasks": [{
    "id": "review",
    "provider": "gateway",
    "model": "your-provider-model-id",
    "prompt_file": "prompts/review.txt",
    "max_output_tokens": 1500,
    "estimated_max_cost_usd": 0.05
  }]
}
```

Replace the placeholder endpoint and model. `base_url` is the API prefix; the
runner appends `/chat/completions` or `/messages`. Each task names a provider and
model, so one plan can mix gateways, direct providers, and local servers. Optional
`system_file` supplies system instructions. Prompt and system paths resolve
relative to the plan file. Task IDs must be unique filenames; `run` is reserved.

| Endpoint type | Configuration |
| --- | --- |
| Compatible gateway, including OpenRouter | `protocol: "openai-compatible"`; use its documented API prefix (OpenRouter: `https://openrouter.ai/api/v1`) |
| Direct DeepSeek | `protocol: "openai-compatible"`, `base_url: "https://api.deepseek.com"`; choose an available model explicitly |
| Anthropic Messages | `protocol: "anthropic"`, `base_url: "https://api.anthropic.com/v1"`, `api_key_env: "ANTHROPIC_API_KEY"` |
| Local compatible server | For example `http://127.0.0.1:8000/v1`; omit `api_key_env` if it needs no authentication |

These configurations follow the [DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/),
[OpenRouter API](https://openrouter.ai/docs/api-reference/overview), and
[Anthropic Messages](https://platform.claude.com/docs/en/api/messages/create)
contracts. Availability and model names depend on the endpoint. Set provider
`token_parameter: "max_completion_tokens"` if its Chat Completions model requires
that field instead of `max_tokens`. Provider-specific reasoning controls,
attachments, streaming, and tool calling are not implemented here.

Put the named key in `~/.config/team-leader/.env` or export it in the environment.
`--env-file PATH` overrides the shared file. Only credential variable names
referenced by the plan are read; nonempty file values take precedence over the
environment. Files contain literal assignments and are never executed. Keep
secret values out of plans. HTTPS is required except for loopback servers;
redirects are rejected so credentials stay with the selected endpoint.

## Preview and execute

From the target project, use the installed skill's controller path:

```bash
python3 /path/to/team-leader/scripts/team_leader.py api run api-plan.json
python3 /path/to/team-leader/scripts/team_leader.py api run api-plan.json \
  --execute --max-estimated-cost-usd 0.05 \
  --output-dir .team-leader/api/wave-1
```

Preview validates configuration and prints assignments without loading keys,
reading prompts, or making requests. Execution validates all credentials and
input files before the first request. Output must be a new directory. Inspect
`run.json` and individual task JSON files; failures return a nonzero exit code.
HTTP errors record status codes without server bodies. Successful results retain
text, usage, and finish reason; inspect truncation before accepting an answer.

Estimate every task's cost using the selected endpoint's pricing and supplied
context; the plan total must equal the task sum. The guard checks estimates,
not actual billing. A provider credit limit is needed for a hard spending cap.
`--max-parallel` overrides plan concurrency (capped at 32); `--timeout` sets the
HTTP socket timeout in seconds. Requests are not retried automatically. Review
failures before choosing another paid wave.
