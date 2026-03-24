# codexd

Codex-first skills for running real child CLI sessions as managed subsessions.

## Skill

- `skills/codex-subsession-manager`

The current implementation ships with a `codex` provider adapter and a control plane that is structured so later adapters can target other CLIs without changing the run registry format.

The controller keeps provider-specific behavior at the adapter boundary: option validation, command construction, session-id detection, and resume command generation. That keeps the run registry and batch manifest format stable when a later `claude`, `cursor`, or other CLI adapter is added.

Project-linked runs now also maintain a central markdown workspace under `.agent-subsessions/projects/<project>/` so the manager can track dashboards, collected child reports, human questions, and conflict-risk notes without manually stitching together terminal output.

## Install After Pushing

```bash
install-skill-from-github.py --repo <owner>/<repo> --path skills/codex-subsession-manager
```

# team_leader
