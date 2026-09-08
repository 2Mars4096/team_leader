# Repository discovery: SEO and GEO maintenance

## Prepared GitHub metadata

Repository: https://github.com/2Mars4096/team_leader

Description:

> Multi-agent orchestration for Codex CLI, Claude Code, Cursor Agent, and Kiro CLI. Run parallel AI coding agents with Git worktree isolation, dependency tracking, and live dashboards. Python controller + Codex skills.

Apply the description and relevant topics with an authenticated GitHub CLI account that can administer this repository:

```bash
gh repo edit 2Mars4096/team_leader \
  --description 'Multi-agent orchestration for Codex CLI, Claude Code, Cursor Agent, and Kiro CLI. Run parallel AI coding agents with Git worktree isolation, dependency tracking, and live dashboards. Python controller + Codex skills.' \
  --add-topic multi-agent \
  --add-topic agent-orchestration \
  --add-topic ai-agents \
  --add-topic coding-agents \
  --add-topic codex \
  --add-topic codex-cli \
  --add-topic codex-skills \
  --add-topic claude-code \
  --add-topic cursor-agent \
  --add-topic kiro \
  --add-topic git-worktree \
  --add-topic workflow-automation \
  --add-topic python

gh repo view 2Mars4096/team_leader \
  --json description,repositoryTopics
```

This command adds topics while preserving existing ones. GitHub allows up to 20 topics; inspect existing topics before applying if the combined set could exceed that limit. [GitHub topic documentation](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/classifying-your-repository-with-topics).

## Content approach

The README identifies the project, supported products, concrete use cases, and installation path before the detailed reference. The linked FAQ provides self-contained answers backed by repository documentation and source. Keep these descriptions consistent when provider support changes.

These changes target useful discovery queries such as “Codex multi-agent orchestration,” “run Claude Code agents in parallel,” “Codex and Claude Code together,” and “AI coding agents Git worktrees.” These are relevance targets, not measured search-volume or ranking claims.

Google's guidance for AI search features emphasizes accessible text, internal links, and useful, reliable content. It does not require special AI files or schema. Accordingly, this repository uses ordinary linked Markdown rather than treating `llms.txt` as a ranking mechanism. [Google Search guidance](https://developers.google.com/search/docs/appearance/ai-features).

## Publishing updates

Commit and push documentation updates to make them available on the public repository. Repository descriptions and topics are separate GitHub settings: pushing these files does not apply them. If the GitHub CLI returns HTTP 401, authenticate with `gh auth login`, inspect the current topics, and then apply the prepared command above.

## Measure after publication

Record a baseline on the publication date, then repeat after two and four weeks:

| Surface | Record | Interpretation |
|---------|--------|----------------|
| GitHub repository traffic | Views, unique visitors, clones, and available referral sources | Compare equal time windows; traffic changes alone do not establish causation. |
| GitHub search | Query, date, sort order, and whether this repository appears | Check the target queries consistently; results and ordering can vary. |
| Web search | Exact query, date, region, and repository URL visibility | Separate index visibility from ranking position. |
| AI search | Exact prompt, service/model, date, cited URLs, and factual accuracy | A mention without a source link is different from a citation. Repeat prompts because answers vary. |

Example AI-search evaluation prompts:

- What tools can coordinate Codex CLI and Claude Code workers in one project?
- How can I run parallel AI coding agents in separate Git worktrees?
- Is there a Python controller for Codex, Claude Code, Cursor Agent, and Kiro CLI?

Retain full responses and citations instead of reporting a single favorable answer as a ranking improvement. Add future examples or performance claims only when supported by reproducible public evidence.
