# Architecture decision records

Every non-obvious design choice in the platform gets a record here, numbered in the order it
was made. A record is never edited after acceptance; a change of mind is a new record that
supersedes it. Articles summarise ADRs; the ADR is the artifact.

| # | Decision | Status | Series |
| --- | --- | --- | --- |
| [0001](0001-seven-subsystems.md) | Seven subsystems, defined by their guarantees | Accepted | A2 |
| [0002](0002-pydantic-single-source.md) | Pydantic models as the single source of the contracts | Accepted | A2 |
| [0003](0003-postgres-as-stage-0-lakehouse.md) | Postgres stands in for the lakehouse at Stage 0 | Accepted | A3 |

## Template

```markdown
# ADR-NNNN: <decision in one line>

- **Status:** Proposed | Accepted | Superseded by ADR-MMMM
- **Date:** YYYY-MM-DD
- **Deciders:** <who>
- **Scope:** <what this binds>

## Context
What forces are in play, with the numbers that matter.

## Options considered
### A. <option>
- **Fits:** …
- **Loses:** …
- **Would win when:** …

(repeat)

## Decision
Which option, and the conventions it fixes.

## Consequences
**Positive** … **Negative** … and the threshold at which to revisit.

## Follow-ups
Related records, and what to measure before the next decision.
```

Name files `NNNN-short-slug.md`. Keep the "would win when" line on every rejected option;
it is the most useful sentence in the record a year later.
