# External worker boundary

`delegate_openrouter.py` is a narrow API adapter. It submits one task and returns
one model response. It is not a Codex-native subagent: it cannot inspect the
workspace, call Codex tools, edit files, or continue autonomously.

## Safe orchestration contract

1. Query the historical model-intelligence packet first when model selection is
   needed. Never attach raw snapshots or database exports.
2. Use the exact OpenRouter model slug selected by the user or returned in the
   recommendation's `OR=` field.
3. Create a bounded task file containing only the instructions and excerpts the
   external worker needs. Do not include secrets or unrelated private context.
4. Attach a PDF only when the user placed that PDF in scope for third-party
   processing. Local PDFs are base64 encoded in memory and are not copied into
   the skill or cache.
5. Review the response inside Codex. Codex owns edits, tests, tool calls, and the
   final answer.

The wrapper requests temperature 0 and seed 0 by default for repeatability, but
provider/model inference is not guaranteed to be bitwise deterministic. The
data ingestion, normalization, ranking, and context-packet scripts are fully
deterministic for fixed source payloads.

PDF processing defaults to OpenRouter's `cloudflare-ai` parser. `mistral-ocr`
may incur a per-page charge; `native` requires native file support. The API key
is read from `OPENROUTER_API_KEY`, used only in the Authorization header, and is
never printed or persisted by the adapter.
