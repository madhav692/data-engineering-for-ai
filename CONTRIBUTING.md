# Contributing

## Branches and commits

- `main` is protected: changes land by pull request with a green `ci` check.
- Branch names: `feat/<slug>`, `fix/<slug>`, `docs/<slug>`, `chore/<slug>`.
- Commit messages follow Conventional Commits with a scope:

  ```text
  feat(schemas): content-addressed chunk ids
  test(schemas): C3 edit changes exactly one chunk
  docs(architecture): nine contracts table in reference.md
  docs(adr): 0002 pydantic as the single source
  ci: schema drift check on every pull request
  chore: scaffold the uv workspace
  ```

  Types: `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`, `chore`.
  Scopes: `schemas`, `platform`, `architecture`, `adr`, a project name, or none.
- Pull requests are squash-merged; the PR title becomes the commit message, so write it as
  a conventional commit too.

## Changing a contract

Contracts live in `packages/schemas/`. A change is one pull request containing all of:

1. The model change in `src/cairn_schemas/models.py`, with `schema_version` bumped on the
   model if a field changed shape.
2. The regenerated files under `packages/schemas/generated/` (`make schemas`). CI runs
   `make check-schemas` and fails if they are missing.
3. The test that proves the guarantee you changed (`packages/schemas/tests/`).
4. The row in `docs/architecture/reference.md` if a guarantee, test or status changed.
5. An ADR under `docs/adr/` if the change is a design decision rather than a field.

Consumers own the tests; producers own the schema. Both are in the same package so a
reviewer sees both halves.

## Changing the platform

`platform/` is one subpackage per subsystem; a change stays inside its subsystem unless it
changes a contract, in which case see above. Every seam between two subsystems has an
integration test under `platform/tests/integration/`, named for what it proves
(`test_edit_reembeds_exactly_one_chunk`), and runs against a real Postgres: on the host through
`make test-platform` (embedded Postgres, hashing embedder), in the api container through
`make test` (compose Postgres, the real model). A new behaviour at a seam is a new test there.
Measured numbers for an article go into `platform/CHANGES-stage-N.md`, with the command that
produced them.

## Before you push

```bash
make fmt              # ruff format + fix
make lint
make check-schemas
make test-contracts
make test-platform    # the platform suite on an embedded Postgres; needs the dev group
```

## Tags

Tags are stage tags (`stage-0` … `stage-9`, `v1.0`), cut only when CI is green and the
stage's article is ready to publish. Nothing else is tagged.
