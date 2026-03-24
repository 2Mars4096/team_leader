# Provider Adapters

Use this note when extending the controller beyond Codex.

## Goal

Keep the manager stable while swapping the child CLI.

That means:

- Keep the registry format generic
- Keep run lifecycle commands generic
- Keep provider-specific branching inside adapter methods only

## Adapter Responsibilities

Each provider adapter should own four things:

1. Validate provider-specific flags
2. Build the non-interactive launch command
3. Detect the child session ID from provider artifacts
4. Build interactive and non-interactive resume commands

The shared manager should not need `if provider == ...` checks outside adapter registration and compatibility aliases.

## Current Shared Contract

The script already assumes each run record can stay generic:

- `provider`
- `session_id`
- `cwd`
- `prompt_path`
- `stdout_path`
- `stderr_log`
- `last_message_path`

For Codex, `thread_id` is still mirrored for backward compatibility, but `session_id` is the canonical field.

## Capability Boundary

Provider differences should be described as capabilities, not spread through the shared flow.

Current capability areas:

- supported sandbox values
- search support
- skip-git-repo-check support
- ephemeral session support
- full-auto support
- dangerous mode support
- model override support
- profile override support
- extra writable directory support
- config passthrough support
- enable/disable feature flag support
- image attachment support
- non-interactive resume support

If a future CLI differs, adjust the adapter capability record and validation logic first.

## Migration Rules

- Do not change the run registry just because a new provider uses different CLI flags
- Prefer generic names such as `session_id` over provider-specific names
- Preserve `attach-thread` only as a Codex compatibility alias
- Keep batch manifest fields stable where possible
- If a provider needs extra fields, add them in a way that does not break existing Codex runs

## Recommended Next Port

When porting to another CLI:

1. Add a new adapter class
2. Define its capability record
3. Implement command construction
4. Implement session detection from its machine-readable logs or local state
5. Implement resume command generation
6. Validate with one real live child run before broadening the docs

Do not mark a provider as supported until a real child run has been verified end to end.
