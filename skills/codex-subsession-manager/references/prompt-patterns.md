# Prompt Patterns

Use these as starting points when dispatching Codex subsessions.

## Research Child

Use when the child should inspect and report without editing:

```text
Goal:
Investigate the request below and return a concise findings report.

Scope:
- Read only these areas: <paths or modules>
- Do not edit files

Validation:
- Cite exact files or commands checked
- Call out uncertainty explicitly

Return:
- Findings ordered by severity
- Open questions
- Suggested next actions for the manager
```

## Implementation Child

Use when the child owns a disjoint write scope:

```text
Goal:
Implement the requested change in the owned files below.

Owned write scope:
- <file or directory list>

Constraints:
- Do not modify files outside the owned scope unless strictly necessary
- If blocked by another file, report the blocker instead of improvising a wide refactor

Validation:
- Run the smallest relevant tests or checks you can

Return:
- Files changed
- What was implemented
- Validation performed
- Risks or deferred work
```

## Reviewer Child

Use when the child should audit a diff or code area:

```text
Goal:
Review the target area and identify bugs, regressions, or missing tests.

Scope:
- Focus on <files or subsystem>
- Prefer findings over summaries

Return:
- Findings ordered by severity
- File references
- Assumptions or unanswered questions
```

## Manager-Style Child

Use when a child may itself coordinate further work:

```text
Goal:
Act as an independent Codex manager for this subproblem.

Scope:
- Own the following area end-to-end: <subproblem>

Authority:
- You may plan, inspect code, run checks, and make bounded edits needed to complete the subproblem
- Keep the work within the assigned area

Return:
- Outcome summary
- Files changed or investigated
- Validation
- What the parent manager should do next
```
