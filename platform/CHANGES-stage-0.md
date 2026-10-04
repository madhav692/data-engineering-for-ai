# Stage 0: the walking skeleton

Tag: `stage-0` · Article: A3, *Cairn Stage 0: A Walking Skeleton of the Whole Platform in One
Docker Compose* · Profile: `core` (postgres, seaweedfs, api)

## What the stage adds

Every one of the seven subsystems from A2, present as code and as thin as its contracts allow:

| Subsystem | At `stage-0` | Code |
| --- | --- | --- |
| 1 Ingestion and registry | A filesystem connector for Markdown; `Document` rows; raw bytes in an S3 bucket (SeaweedFS) under their content hash; skip-unchanged; tombstones with `--prune` | `cairn/ingestion/filesystem.py` |
| 2 Lakehouse | One Postgres database holding the seven contract tables, created from the generated Postgres DDL (ADR-0003); SeaweedFS (S3 API) for raw bytes | `cairn/lakehouse/` |
| 3 Derivation | `md-raw-v1` parser, `md-struct-v1` chunker (heading path, 400-token budget), fastembed `BAAI/bge-small-en-v1.5` on the CPU; the dirty set as two SQL anti-joins; a chunker version change is a backfill from the raw bytes | `cairn/derivation/` |
| 4 Indexes | One pgvector table per index snapshot with an HNSW index; `IndexSnapshot` rows; `status='live'` as the pointer; `index_lag_seconds` computed from the data | `cairn/indexes/` |
| 5 Serving and lineage | FastAPI `/ask`; vector retrieval against the live snapshot; the extractive generator (default) or any OpenAI-compatible endpoint; the `Request` row written before the response; `cairn replay` and `--diff` | `cairn/serving/`, `cairn/lineage/` |
| 6 Feedback and evaluation | `/feedback` with a per-tenant dedup key; a 20-question golden set; `cairn eval` writing an `EvalRun` with recall@5 | `cairn/evaluation/` |
| 7 Observability and cost | JSON logs; `/metrics` computed from the tables on every scrape | `cairn/observability/` |

Also: the fourth generator target in `packages/schemas` (`generate/postgres.py`), ADR-0003, the
small dataset tier (`datasets/small/`), the golden set (`datasets/golden/small.jsonl`), the
`integration.yml` workflow, and `make start | ingest | build-index | ask | replay | test`.

## The invariant

**Every request is logged with the versions of its inputs.** A `Request` row will not construct
without `prompt_template_version`, `retrieval_config_version`, `index_snapshot_id`,
`embedding_model_id`, `reranker_id`, `llm_id` and `llm_params`, and `/ask` writes the row before
it returns the answer. `cairn replay` proves the row is enough: retrieval is re-run against the
logged snapshot and the chunk ids are compared.

## Versions in force

| Field | Value |
| --- | --- |
| `parser_version` | `md-raw-v1` |
| `chunker_version` | `md-struct-v1` |
| `embedding_model_id` | `baai/bge-small-en@v1.5:384` |
| `prompt_template_version` | `answer-v1` |
| `retrieval_config_version` | `vector-k5-v1` |
| `llm_id` | `cairn/extractive@0` (default) |
| `reranker_id` | none |

## Measured numbers

Two columns. **Sandbox** numbers were measured while building the stage, on a 2-vCPU Intel Xeon
2.8 GHz container with 7 GB of memory and no Docker (embedded Postgres 16.2 + pgvector 0.6.2 via
`pgserver`, filesystem object store, the real fastembed model). **Laptop** numbers are the ones
the article quotes; fill them in from a clean clone on the machine the article names, using the
"How" column.

| Measure | Sandbox | Laptop | How |
| --- | --- | --- | --- |
| Clone to first answer, cold (no cached images) | n/a (no Docker) | | `time (git clone … && make start && make ingest && make build-index && make ask Q=…)` |
| Image pulls | n/a | | `docker images` (pgvector, seaweedfs, cairn-api) |
| Embedding model download, once | 65 MB (`BAAI/bge-small-en-v1.5`, quantised ONNX) | | size of the `model-cache` volume |
| Corpus: documents · chunks · tokens · raw bytes | 55 · 1,244 · 262,667 · 1,013,202 B | | `cairn stats` |
| Ingest + derivation, cold | 257.6 s (dominated by embedding) | | printed by `cairn ingest` |
| Embedding throughput, CPU | 4.9 chunks/s (2 vCPU) | | printed by `cairn ingest` |
| `make build-index` | 0.3 s for 1,244 rows | | printed by `cairn build-index` |
| Retrieval latency, 21 requests, k = 5 | P50 12 ms · P95 25 ms | | `cairn_request_latency_ms` on `/metrics` |
| Golden-set recall@5 · MRR (20 questions) | 0.95 · 0.76 | | `cairn eval` |
| Database size after ingest + 21 requests | 18 MB | | `cairn stats` |
| Memory at rest and during ingest | n/a | | `docker stats --no-stream` |
| Platform suite (16 unit + 13 seam tests), hashing embedder | 4.2 s | | `make test-platform` |
| Seam tests, real model | 8.6 s | | `make test` (laptop: in the api container) |

Notes on the sandbox run: the one golden-set miss is "What are branches and tags on an Iceberg
table used for?", for which retrieval ranked the table spec's *Snapshot References* section (which
defines branches and tags) above `branching.md`; the set says `branching.md`, so it is a miss. Twenty
questions is a smoke test. Retrieval of the article's question returned `partitioning.md ›
Iceberg's hidden partitioning` at rank 1 with cosine similarity 0.843; replay reproduced 5 of 5
chunk ids in the same order and the response hash matched.

## Failure drills

Run on the tagged build; paste the output into A3.

| Drill | Expected | Status |
| --- | --- | --- |
| `docker kill cairn-postgres` during `make ingest`; `make start`; `make ingest` | The second run registers only what the first did not reach; no duplicates | to run on the laptop |
| Edit one paragraph of one article; `make ingest`; `make lag`; `make build-index` | One new revision, one new chunk, one new embedding; lag positive until the build | covered by `test_edit_reembeds_exactly_one_chunk`, `test_ingest_after_build_raises_lag` |
| Delete a file; `make ingest PRUNE=1`; `make build-index` | A deleted revision; the index has fewer rows; `make ask` no longer cites it | covered by `test_removed_file_becomes_a_tombstone` |
| Set `CHUNKER_VERSION = "md-struct-v2"`; `make restart`; `make ingest` | Every chunk gets a new id; every document is re-derived from its raw bytes; old rows remain | covered by `test_chunker_version_change_is_a_backfill` |
| Post the same feedback twice | One row | covered by `test_duplicate_feedback_counts_once` |
| `UPDATE chunks SET text = text \|\| 'x' WHERE chunk_id = …`; `make replay REQ=…` | The `Chunk` model refuses to construct; replay reports the row as inconsistent | covered by `test_replay_reproduces_retrieved_chunk_ids` |

## What is deliberately thin

No Iceberg, no Kafka, no crawler, no hybrid retrieval, no reranker, no cache, no multi-tenancy,
no evaluation gate in CI, no language model unless you point `CAIRN_LLM_BASE_URL` at one. Each
thickens at a named stage; the map is in A3 and in `docs/architecture/reference.md`.

## Decisions recorded

- ADR-0003: Postgres stands in for the lakehouse at Stage 0.
- The object store is SeaweedFS, not MinIO: MinIO's community Docker images stopped in October
  2025 and the project was archived in 2026. The code depends on the S3 API only (through the
  `minio` client library); the product behind it is a compose detail.
- Index tables are named per snapshot (`idx_<index name>_<12 hex of the snapshot id>`) and the
  last two retired snapshots' tables are kept (`CAIRN_INDEX_RETAIN`), so replay searches exactly
  what a request searched and a rollback is a pointer flip. The build-then-rename variant was
  dropped: it could not keep that promise.
- The extractive generator's `llm_params` are configuration (temperature 0, `max_tokens` 400 =
  one chunk), not per-request data, so an `EvalRun.config` equals every request's lineage (C8).
- The chunker reads the live parser and chunker versions at call time, and the pipeline re-derives
  any document that lacks chunks under them from its raw bytes, so a version bump needs no flag.
- The dirty-set and index-build queries filter on the live `parser_version` and `chunker_version`:
  after a chunker change the old rows are inert, not duplicated into the index.
