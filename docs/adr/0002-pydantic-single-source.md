# ADR-0002: Pydantic models as the single source of the contracts

- **Status:** Accepted
- **Date:** 2026-10-02
- **Deciders:** series author
- **Scope:** `packages/schemas/`; every table, topic and API payload in the platform

## Context

The same seven entities have to exist in three places: as Avro schemas on Kafka topics, as
Iceberg tables in the lakehouse, and as JSON payloads on the API. Three hand-maintained
copies drift; the first drift is a field that exists on the topic and not in the table, and
nobody notices until a reconciliation job reads nulls.

The platform is Python-first: the serving layer, the derivation jobs and the tests are
Python. Producers are not polyglot today.

## Options considered

### A. Avro IDL as the source, generate Python

- **Fits:** Avro is already the registry format; Confluent tooling is mature.
- **Loses:** Avro has no validation beyond types. The invariants that matter here
  (`chunk_id` equals its derivation, vector length equals `dims`, ranks are contiguous) are
  not expressible, so they would live in a second layer anyway. Generated Python classes are
  awkward to extend with `from_bytes` and `build` constructors.

### B. Protobuf as the source

- **Fits:** polyglot, compact on the wire, strong tooling.
- **Loses:** same validation gap as Avro; Iceberg DDL and JSON Schema still need generators;
  the series' readers write Python and a `.proto` file is a worse teaching surface than a
  class with validators.
- **Would win when:** producers exist in Go, Java or Rust. The threshold is: the first
  non-Python producer of a Cairn row. At that point generate `.proto` from the models or
  migrate the source; the generators keep the other targets stable either way.

### C. Hand-written DDL and `.avsc`, with Python models written to match

- **Fits:** nothing to build.
- **Loses:** three copies, no check that they agree, and a review surface where a reviewer
  cannot see the whole contract in one file.

### D. Pydantic v2 models as the single source; generators for the rest (chosen)

- **Fits:** validators enforce the invariants at the point of construction, the models double
  as the API types and the test fixtures, and one file shows the whole contract. Generators
  for Avro, Iceberg DDL (Spark SQL) and JSON Schema are small (about 300 lines in total) and
  deterministic, so their output is committed and CI fails on drift.
- **Loses:** Python-only source; generators are the project's own code to maintain; a few
  storage types have no Python equivalent (float32 vectors), handled with per-field overrides.

## Decision

Option D. `cairn_schemas.models` is the source of truth. `python -m cairn_schemas.generate`
emits `generated/avro/*.avsc`, `generated/iceberg/*.sql`, `generated/jsonschema/*.json` and
`generated/manifest.json`; the outputs are committed, and `make check-schemas` fails CI if
the committed files do not match the models.

Conventions fixed by this decision:

- Every model derives from `Row` (`tenant_id`, `created_at`, `schema_version`); rows that
  touched a model also mix in `Costed` (`tokens_in`, `tokens_out`, `cost_usd`).
- Contract metadata is declared on the class (`TABLE`, `PRIMARY_KEY`, `LINEAGE_FIELDS`,
  `PARTITION_BY`) and emitted as Iceberg table properties, so MERGE statements and audits
  read the key and the lineage fields from the table itself.
- Storage-only types are declared per field with `json_schema_extra`
  (`iceberg_type`, `avro_type`); the generators honour them.
- Column order in every target is: the `Row` fields, then the model's own fields, then the
  `Costed` fields.
- A field change bumps `schema_version` on the model and ships with the regenerated files in
  the same commit.

## Consequences

**Positive**

- One pull request shows the model change and its effect on every target.
- The tests that prove the contracts use the same classes the platform uses.
- Readers of the series see the contract as code they can run.

**Negative**

- A second language producer forces either generated `.proto` or a source migration
  (threshold stated above).
- The generators are the project's own; they are covered by `test_generators.py` and by
  validating every output with its target's parser (`fastavro`, `sqlglot` Spark dialect,
  `jsonschema` draft 2020-12).
- Spark SQL is the only DDL dialect emitted; Trino and Flink DDL are generator additions if
  a chapter needs them.
