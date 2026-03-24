# team-leader

Codex-first skill for running a team-leader style manager over real child CLI sessions.

## Skill

- `skills/team-leader`

The current implementation ships with a `codex` provider adapter and a control plane that is structured so later adapters can target other CLIs without changing the run registry format.

The controller keeps provider-specific behavior at the adapter boundary: option validation, command construction, session-id detection, and resume command generation. That keeps the run registry and batch manifest format stable when a later `claude`, `cursor`, or other CLI adapter is added.

Project-linked runs maintain a central markdown workspace under `.agent-subsessions/projects/<project>/` so the manager can track dashboards, collected child reports, human questions, and conflict-risk notes without manually stitching together terminal output.

The default landing page for each project is `.agent-subsessions/projects/<project>/README.md`. From there:

- `dashboard.md` shows live run progress, active child notes, and watcher status
- `tasks.md` shows assignment state and summary titles
- `manager-summary.md` shows the latest manager aggregation
- `questions.md` shows human decisions and blockers
- `conflicts.md` shows overlap risk between writers
- `reports/<run-id>.md` stores one markdown report per child session

While child sessions are running, the manager refreshes those markdown files automatically in the background.

## Install After Pushing

```bash
install-skill-from-github.py --repo <owner>/<repo> --path skills/team-leader
```
