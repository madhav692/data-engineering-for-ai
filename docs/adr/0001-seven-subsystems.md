# ADR-0001: Seven subsystems, defined by their guarantees

- **Status:** Accepted
- **Date:** 2026-10-02
- **Deciders:** series author
- **Scope:** the structure of the Cairn platform, the repository and the forty-article series

## Context

One structure has to organise a platform, a monorepo and forty articles, and survive the
components under it being replaced at every order of magnitude (one Postgres at 10K users;
Qdrant, Kafka, Flink, Redis, Iceberg and ClickHouse at 1M; sharded fleets at 100M).

The incident that motivates the series (A1, "Day 23") happened between two boxes: the
knowledge base was correct and the index was stale, and nobody owned the gap because no
contract had been drawn across it. The structure therefore has to be defined by what each
part guarantees to its neighbours, not by which product fills the box.

## Options considered

### A. Three tiers: ingest, store, serve

- **Fits:** everyone recognises it; maps onto most existing data platforms.
- **Loses:** too coarse. Derivation (parse, chunk, embed) and the feedback loop disappear
  inside "store" and "serve", and those are exactly where the AI-specific failures live:
  model-versioned transforms, stale indexes, contaminated eval sets, unreproducible answers.
- **Would win when:** the system has no model-backed transforms and no loop, which is to say
  when it is a reporting pipeline.

### B. A tool-centric stack

Vector database, orchestration framework, model gateway, evaluation tool, observability
vendor: one layer per product category.

- **Fits:** matches how teams buy; easy to draw with logos.
- **Loses:** organises by vendor category, so the contracts fall between vendors and nobody
  owns them. The lag between the lakehouse and the vector database is not a feature of
  either product. Swapping a vendor rewrites the diagram.
- **Would win when:** the goal is a procurement document rather than a design.

### C. The ML-platform model

Feature store, training, serving, monitoring.

- **Fits:** the right instincts about consistency (point-in-time correctness, training/serving
  skew) and about monitoring.
- **Loses:** no place for unstructured derivation, for indexes as rebuildable views, or for
  the request log as data that feeds the next evaluation set and the next index. It governs
  the inputs to a model; it has no notion of "re-embed everything because the vendor retired
  a model".
- **Would win when:** the workload is tabular ML with no retrieval and no generation.

### D. Seven subsystems by guarantee (chosen)

Each subsystem is defined by one invariant it must hold for its neighbours. Components are
replaceable; invariants are not.

| # | Subsystem | Invariant |
| --- | --- | --- |
| 1 | Ingestion and registry | Every input gets a stable ID and a content hash at the door |
| 2 | Lakehouse | Single source of truth; everything else is rebuildable from it |
| 3 | Derivation | Every output carries the version of the model that made it |
| 4 | Indexes | Derived, disposable, with a measured lag against the lakehouse |
| 5 | Serving and lineage | Any response is reproducible from its `request_id` |
| 6 | Feedback and evaluation | Data changes pass the same gates as code changes |
| 7 | Observability and cost | Every silent failure has a computed alarm; every token is attributed |

## Decision

Option D. The platform, the repository (`platform/<subsystem>/`) and the series (one
chapter per subsystem or per contract between two) are organised by these seven
guarantees. The nine contracts between them are specified in
`docs/architecture/reference.md`, and every contract has a test or a named stage at which
its test arrives.

Two corollaries are decided here as well, because they follow from the invariants:

- **Versions are namespaces.** An embedding model id is part of the index name
  (`{base}--{model slug}`), never a column to filter on. A model change is a new index and a
  pointer move; rollback moves the pointer back. (Invariant 4; A16.)
- **Chunk identity is content-addressed.** `chunk_id` is derived from the document identity,
  the parser and chunker versions, the text hash and an occurrence counter, and deliberately
  excludes the document's content hash, the ordinal and the byte offsets, so an unchanged
  chunk keeps its id when the document around it changes. (Invariant 3; A10.)

## Why seven, and not five or nine

- Merging ingestion into the lakehouse hides the identity problem: stable IDs and content
  hashes are the ingestion subsystem's whole job, and without them nothing downstream is
  idempotent.
- Merging evaluation into observability hides the gate: observability watches, evaluation
  blocks. They have different owners and different failure modes.
- Splitting indexes into vector and lexical would create a contract between two things that
  must be built from the same snapshot and swapped together; they are one subsystem with
  two engines.
- Splitting serving from lineage would let the request log be an afterthought, which is the
  Day 23 failure.
- Agents (Chapter 9) are not an eighth subsystem: an agent runtime is subsystems 5 and 6
  applied to a workflow, with memory as another corpus in subsystems 1 to 4.

## Consequences

**Positive**

- Any component can be replaced without changing the contract on either side of it; the
  test of a good seam is exactly that.
- At 10K users the seven subsystems are one database and one process, so the structure
  costs nothing until it is needed.
- Every article can locate itself ("this is subsystem N; its contract with N+1 is C_k").

**Negative**

- Seven names to learn before the first line of code. The reference architecture has to be
  the second article for that reason.
- Some early components are deliberately thin (an `IndexSnapshot` record with no
  reconciliation job yet). The status column in `reference.md` keeps that honest.
- Content-addressed chunk ids mean the `chunks` table is a current-state table under
  MERGE; the history of which chunks a document version contained is in Iceberg snapshots
  and in the request log, not in a separate membership table. Revisit if a membership table
  is needed for audits (A21).

## Follow-ups

- ADR-0002: Pydantic models as the single source of the contracts.
- A future ADR when the first component is swapped (pgvector -> Qdrant at Stage 3) to record
  that the contract C4/C5 did not change.
