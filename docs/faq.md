# Team Leader FAQ: Parallel AI Coding Agents

## What is Team Leader?

Team Leader is a multi-agent orchestration controller for Codex CLI, Claude Code, Cursor Agent, and Kiro CLI. It launches full external CLI sessions, tracks task dependencies and progress, and aggregates worker reports. It ships as a skill for Codex and Claude Code, plus a standalone Python script in [2Mars4096/team_leader](https://github.com/2Mars4096/team_leader).

## Can I run Codex and Claude Code agents in the same project?

Yes. Team Leader supports a provider choice per child task. A project can use one provider for planning and another for implementation or review. Install and configure each CLI first, then use `provider-check` to check readiness. Set `--planner-provider`, `--child-provider`, and `--allow-provider` when recording the project brief. See the [quick start](../README.md#quick-start) and [provider adapter contract](../skills/team-leader/references/provider-adapters.md).

## Can Claude Code be the manager?

Yes. Copy the two skill directories into `~/.claude/skills/`, then invoke
`/team-leader` or `/team-status`. Use `TEAM_LEADER_LAUNCHER_PROVIDER=claude` on
controller commands to choose Claude planners and workers. The `intake` and
`orchestrate` path works without Codex; the nested `workflow` command still
requires Codex. See [Claude Code installation](../README.md#install-into-claude-code).

## Is Team Leader limited to DeepSeek or OpenRouter APIs?

No. `api run` accepts configurable OpenAI-compatible Chat Completions and
Anthropic Messages endpoints. Set the endpoint, credential variable, and model
explicitly; a batch can mix providers. Compatible local servers are supported.
Other protocols need an adapter. API workers return text; CLI workers provide
tools and persistent sessions. See the [API worker guide](../skills/team-leader/references/api-workers.md).

## Does Team Leader support Cursor and Kiro?

Yes. The controller ships a `cursor` adapter for the `cursor-agent` executable and a `kiro` adapter for `kiro-cli`. Kiro resume is directory-scoped; the other shipped providers use session identifiers. Windsurf and Antigravity adapters are not included. The [provider table](../README.md#supported-providers) documents the shipped integrations, and the [controller source](../skills/team-leader/scripts/team_leader.py) defines their capabilities.

## How does Team Leader differ from built-in subagents?

Team Leader starts separate external CLI processes. Each child has its own context, tool access, logs, and provider-specific resume mechanism. The controller adds dependency tracking, project dashboards, and aggregation around those processes. This comparison describes Team Leader's process model; built-in subagent capabilities vary by product.

## Do I need Codex to use the controller?

You can run the Python controller directly without using the Codex skill interface. You need Python 3.10 or later, at least one supported CLI installed and configured, and Git for worktree-based writer isolation. Select an installed provider explicitly when following the [standalone controller example](../README.md#using-the-controller-script-directly).

## How do I install Team Leader?

Clone the repository over HTTPS and copy `skills/team-leader` and, optionally, `skills/team-status` into your Codex or Claude Code skills directory. The [installation instructions](../README.md#installation) include the exact commands and the Codex skill installer alternative. The controller uses the Python standard library; provider CLIs have their own installation and account requirements.

## How are parallel code changes isolated?

Writer children in Git repositories use separate per-run worktrees. The manager integrates completed work through a project integration worktree before validation. Task dependencies and declared owned paths help coordinate overlapping work, but merge conflicts can still occur. See [operating limits](../README.md#operating-limits) and the [project workspace reference](../skills/team-leader/references/project-workspaces.md).

## How do I monitor or resume long-running agents?

Use `status` for a snapshot, `team-status` for compact progress, and `watch` for live terminal updates. Per-run logs and heartbeat metadata help identify stalled sessions. `resume-cmd` prints a provider-specific resume command. The generated project workspace contains dashboards, task reports, and manager summaries; see [CLI reference](../README.md#cli-reference).

## Can I limit how long the agent team runs?

Yes. `--max-work-seconds` sets the project's work window; `--max-run-seconds` caps an individual child run. Project orchestration also obeys concurrency and planner-round limits. A work window does not guarantee that the project will finish within it. See [operating limits](../README.md#operating-limits) and the [workspace guide](../skills/team-leader/references/project-workspaces.md).

## Is Team Leader an AI model or a hosted service?

Team Leader is a local orchestration controller, not a model or hosted inference service. Its child CLIs supply model access and execution capabilities. Provider billing, authentication, and tool permissions remain specific to those CLIs.

## Does parallel orchestration guarantee faster or better results?

No. Results depend on task independence, provider behavior, review quality, and validation. Planning and integration add overhead. Use a concrete validation command and inspect project reports to judge a run; this documentation makes no measured speedup or quality claim.

[Back to Team Leader](../README.md)
