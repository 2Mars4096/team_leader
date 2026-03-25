# team-leader

Codex-first skill for running a team-leader style manager over real child CLI sessions.

## Skill

- `skills/team-leader`
- `skills/team-status`

The current implementation ships with a `codex` provider adapter and a control plane that is structured so later adapters can target other CLIs without changing the run registry format.

The controller keeps provider-specific behavior at the adapter boundary: option validation, command construction, session-id detection, and resume command generation. That keeps the run registry and batch manifest format stable when a later `claude`, `cursor`, or other CLI adapter is added.

Invoke the controller by script path, but keep your working directory at the target project unless you pass `--root` and `--cd` explicitly. The default `.team-leader/` root is derived from the current working directory, so running the controller from the installed skill directory is the wrong default.

Project-linked runs maintain a central markdown workspace under `.team-leader/projects/<project>/` so the manager can track the project brief, planner output, dashboards, collected child reports, human questions, and conflict-risk notes without manually stitching together terminal output. Writer runs inside Git repos are isolated into per-run worktrees, and the manager integrates them through a project integration worktree before validation runs. Older `.agent-subsessions` and `.codex-subsessions` roots are still recognized automatically.

The current safety defaults are intentionally conservative:

- no more than `8` child sessions running at once
- no more than `2` new child launches every `15` seconds
- oversized child `last_message.md` files are truncated with head/tail preservation
- `status --project` and project markdown now surface artifact-size warnings
- `team-status --project` provides a compact progressive update stream that is safer to use inside captured Codex output
- non-TTY `watch` falls back to a single snapshot unless explicitly allowed to stream

The default landing page for each project is `.team-leader/projects/<project>/README.md`. From there:

- `brief.md` records the project goal, repo paths, spec paths, notes, and constraints
- `launch-plan.md` shows the latest planner-produced child-session launch plan
- `validation.md` shows validation-command results and machine-evaluable delivery status
- `dashboard.md` shows live run progress, active child notes, and watcher status
- `tasks.md` shows assignment state and summary titles
- `manager-summary.md` shows the latest manager aggregation
- `questions.md` shows human decisions and blockers
- `answers.md` is the human-edited answer file the manager reads
- `answers-template.md` gives copy-ready lines for open questions
- `conflicts.md` shows overlap risk between writers
- `reports/<run-id>.md` stores one markdown report per child session

The project folder is persistent manager state. Reusing the same project name reuses the same folder and tracked history. In normal continuation, do not delete the generated markdown files by hand; let the manager refresh them. The only file intended for direct human editing is `answers.md`. For a clean restart, use a new project name.

While child sessions are running, the manager refreshes those markdown files automatically in the background. Tasks with `depends_on` are held automatically until their prerequisites complete, then the manager launches the next wave on its own. The new default flow is:

1. record the goal, repo paths, and specs with `intake`
2. let the manager ask a short clarification round first when the brief is still thin
3. run `orchestrate`
4. let the planner child produce a launch plan
5. let the manager auto-dispatch worker children from that plan
6. answer only the questions that really need a human
7. let validation failures trigger focused fixer/replan waves in `continuous` mode until delivery or the auto-fix budget is exhausted

Projects can now set both an autonomy mode and a clarification mode:

- `clarification_mode=auto`: the planner may ask a few targeted questions before launching workers
- `clarification_mode=off`: skip that gate and plan immediately

Projects can also cap automatic recovery work:

- `max_auto_fix_rounds`: how many validation-failure recovery waves the manager may launch on its own

Autonomy modes:

- `manual`: you explicitly run `orchestrate`
- `guided`: the manager runs validation commands and tracks delivery state, but does not auto-start new planner waves
- `continuous`: once the brief is present, the manager can auto-start planner waves and keep pushing until validation and completion signals say the project is delivered, or the configured planner-round limit is reached

From the target project root, use `python3 skills/team-leader/scripts/team_leader.py status --project <project>` for the live summary in this repo. When the skill is installed elsewhere, call that installed script path while keeping the working directory anchored to the target project, or pass `--root` and `--cd` explicitly. That prints the current stage, stage reason, next action, current focus, workspace path, dashboard path, active runs, blocked runs, open questions, recent answers, and conflict hints without needing to open the folder manually.

For progressive feedback inside Codex, prefer `python3 skills/team-leader/scripts/team_leader.py team-status --project <project>`. That prints compact change-based updates with the current stage, progress, active child summaries, latest child notes, blocked or queued work, open questions, and warnings. When stdout is captured, it automatically caps itself unless you override `--max-updates`.

If you want a lighter event-style feed, add `--milestones`. That only emits meaningful changes such as stage transitions, child lifecycle changes, new child notes, newly opened questions, conflicts, integration issues, and warning changes.

If you want a dedicated monitoring skill chip in Codex, install `skills/team-status` as well. That gives you a `$team-status` skill entry for project progress checks while keeping `$team-leader` focused on planning and dispatch.

For a live terminal panel, use `python3 skills/team-leader/scripts/team_leader.py watch --project <project>`. That repeatedly refreshes the project summary plus per-run lines, including integration state and the latest child note. In captured terminal environments, `watch` now defaults to one snapshot unless you explicitly opt into streaming.

## Install After Pushing

```bash
install-skill-from-github.py --repo <owner>/<repo> --path skills/team-leader
```
