# team-leader

Codex-first skill for running a team-leader style manager over real child CLI sessions.

## Skill

- `skills/team-leader`

The current implementation ships with a `codex` provider adapter and a control plane that is structured so later adapters can target other CLIs without changing the run registry format.

The controller keeps provider-specific behavior at the adapter boundary: option validation, command construction, session-id detection, and resume command generation. That keeps the run registry and batch manifest format stable when a later `claude`, `cursor`, or other CLI adapter is added.

Project-linked runs maintain a central markdown workspace under `.team-leader/projects/<project>/` so the manager can track the project brief, planner output, dashboards, collected child reports, human questions, and conflict-risk notes without manually stitching together terminal output. Older `.agent-subsessions` and `.codex-subsessions` roots are still recognized automatically.

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

While child sessions are running, the manager refreshes those markdown files automatically in the background. Tasks with `depends_on` are held automatically until their prerequisites complete, then the manager launches the next wave on its own. The new default flow is:

1. record the goal, repo paths, and specs with `intake`
2. run `orchestrate`
3. let the planner child produce a launch plan
4. let the manager auto-dispatch worker children from that plan
5. answer only the questions that really need a human

Projects can now set an autonomy mode:

- `manual`: you explicitly run `orchestrate`
- `guided`: the manager runs validation commands and tracks delivery state, but does not auto-start new planner waves
- `continuous`: once the brief is present, the manager can auto-start planner waves and keep pushing until validation and completion signals say the project is delivered, or the configured planner-round limit is reached

From Codex itself, use `python3 skills/team-leader/scripts/team_leader.py status --project <project>` for the live summary. That prints the current stage, stage reason, next action, current focus, workspace path, dashboard path, active runs, blocked runs, open questions, recent answers, and conflict hints without needing to open the folder manually.

## Install After Pushing

```bash
install-skill-from-github.py --repo <owner>/<repo> --path skills/team-leader
```
