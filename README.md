# Data Engineering for AI

**The model is the easy part. Build the data systems around it.**

Production AI is a data system with a model in the loop. The model can be swapped in an
afternoon; the data platform decides whether the system is correct, current, reproducible
and affordable. This repository is the platform behind the series: **Cairn**, one AI
knowledge platform built in ten stages across ten chapters, plus the standalone flagship
projects and the shared packages they use. Every stage is a tag and a working system.

Series: https://aiteachinglabs.com/series/data-engineering-for-ai/

## What is here today

| Path | What | Series |
| --- | --- | --- |
| `packages/schemas/` | The data contracts: seven Pydantic models generating Avro, Iceberg DDL and JSON Schema; the contract tests | A2 |
| `docs/architecture/reference.md` | The reference architecture: seven subsystems, nine contracts, the lineage spine | A2 |
| `docs/architecture/diagrams/` | Diagram sources and exports | A1, A2 |
| `docs/adr/` | Decision records (0001 seven subsystems, 0002 Pydantic as the single source) | A2 |
| `platform/` | Cairn: arrives with `stage-0`, the walking skeleton | A3 |
| `projects/` | Flagship projects: arrive from Chapter 2 on | A5 onward |

## Run it

```bash
git clone https://github.com/<you>/data-engineering-for-ai
cd data-engineering-for-ai
make setup             # uv sync; needs uv (https://docs.astral.sh/uv/) and Python 3.12+
make test-contracts    # the contract tests
make schemas           # regenerate the Avro / DDL / JSON Schema from the models
```

`make start`, `make ingest` and `make ask` arrive with the `stage-0` tag.

## Layout

```text
data-engineering-for-ai/
├── Makefile              setup · schemas · check-schemas · test-contracts · lint · fmt
├── pyproject.toml        uv workspace root
├── .github/workflows/    ci.yml: lint, schema drift check, contract tests
├── packages/schemas/     cairn-schemas (see its README)
├── docs/architecture/    reference.md, diagrams/
└── docs/adr/             decision records
```

## Licence

Apache-2.0. See `LICENSE`.
