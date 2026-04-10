# Provider Adapters

Use this note when extending the controller beyond the currently shipped providers (`codex`, `claude`, `cursor`, `kiro`).

## Goal

Keep the manager stable while swapping the child CLI.

That means:

- Keep the registry format generic
- Keep run lifecycle commands generic
- Keep provider-specific branching inside adapter methods only

The current script uses a shared exec-style adapter contract for all shipped providers. Codex still adds provider-specific hooks for backend checks and thread detection, but it no longer needs a separate launch/resume contract.

## Adapter Responsibilities

Each provider adapter should own four things:

1. Validate provider-specific flags
2. Build the non-interactive launch command
3. Detect the child session ID from provider artifacts
4. Build interactive and non-interactive resume commands
5. Expose a cheap preflight command for `provider-check` and launch gating

The shared manager should not need `if provider == ...` checks outside adapter registration and compatibility aliases.

## Current Shared Contract

The script already assumes each run record can stay generic:

- `provider`
- `provider_bin`
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
- Prefer per-run `provider_bin` overrides over provider-specific path fields
- Keep alias normalization separate from adapter registration so canonical provider ids stay stable in run records
- Preserve `attach-thread` only as a Codex compatibility alias
- Keep batch manifest fields stable where possible
- If a provider needs extra fields, add them in a way that does not break existing Codex runs

## Recommended Next Port

When porting to another CLI:

1. Add a new adapter class
2. Prefer wiring it through `ExecProviderAdapter` first if it is just another command-line agent
3. Define its capability record
4. Implement command construction
5. Implement session detection from its machine-readable logs or local state
6. Implement resume command generation
7. Implement a cheap `provider-check` health command
8. Validate with one real live child run before broadening the docs

The shipped `provider-smoke-test` command is the preferred validation surface for step 8 because it exercises the same dispatch, polling, last-message capture, and terminal-status logic used by normal manager launches.

Do not mark a provider as supported until a real child run has been verified end to end.
