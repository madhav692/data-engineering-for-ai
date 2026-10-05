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
`integration.yml` workflow, and `make start | ingest | build-index | ask | requests | replay | test`.

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

Measured on 2026-10-05 on a MacBook Pro (Apple M3 Pro, 12 cores, 36 GB) running macOS 27.0.1,
Docker Desktop 29.4.0 with a 12-CPU, 17.5 GiB VM, Compose 5.1.2, from an empty directory with no
Cairn images and no build cache: one run of `scripts/measure-stage-0.sh --cold` at commit
`59b5f9b`, which clones the repository, times the reader's sequence step by step and then collects
every row below. These are the numbers A3 quotes.

| Measure | Measured | How |
| --- | --- | --- |
| Clone to first answer, cold (no images, no build cache) | 111 s: clone 2 s · `make start` 49 s (three image pulls, the api image build, the model download, health checks) · `make ingest` 59 s · `make build-index` under 1 s · `make ask` 1 s | the script's wall clock per step |
| Image sizes | `pgvector/pgvector:pg16` 466 MB · `chrislusf/seaweedfs:4.48` 502 MB · `cairn-api:stage-0` 469 MB (built locally); 1.44 GB together | `docker images` |
| Embedding model download, once | 65 MB (`BAAI/bge-small-en-v1.5`, quantised ONNX; 66,465,124 B) | `du -sh /models` in the api container |
| Corpus: documents · chunks · tokens · raw bytes | 55 · 1,246 · 263,458 · 1,015,674 B | `cairn stats` |
| Ingest + derivation, cold | 59 s wall clock; the derivation step 49.0 s, dominated by embedding | printed by `cairn ingest` |
| Embedding throughput, CPU | 26.9 chunks/s with the api container at about 11.5 cores busy | printed by `cairn ingest`; `docker stats` |
| `make build-index` | 0.2 s for 1,246 rows | printed by `cairn build-index` |
| Retrieval latency, 21 requests, k = 5 | P50 6 ms · P95 14 ms | `cairn_request_latency_ms` on `/metrics` |
| Golden-set recall@5 · MRR (20 questions) | 0.95 · 0.76 | `cairn eval` |
| Database size after ingest + 21 requests | 18 MB | `cairn stats` |
| Memory at rest | api 202 MiB · seaweedfs 84 MiB · postgres 27 MiB, about 314 MiB together | `docker stats --no-stream` after `make start` |
| Memory during ingest | api peaks at 1.76 GiB (the ONNX runtime on all 12 cores); seaweedfs 115 MiB; postgres 38 MiB | `docker stats` sampled every 10 s |
| Platform suite (16 unit + 13 seam tests), hashing embedder, embedded Postgres, on the host | 29 passed in 4.8 s | `make test-platform` |
| The same suite in the api container: real model, compose Postgres, SeaweedFS S3 | 29 passed; 5 s wall clock for `make test`, including the compose health wait | `make test` |

Notes on the run. The one golden-set miss is "What are branches and tags on an Iceberg table used
for?": retrieval ranks the table spec's *Snapshot References* section, which defines branches and
tags, above `branching.md`, and the set says `branching.md`, so it is a miss. Thirteen of the twenty
questions hit at rank 1. Twenty questions is a smoke test, not an evaluation. The article's question
returned `iceberg-docs/partitioning.md › Partitioning > What does Iceberg do differently? >
Iceberg's hidden partitioning` at rank 1 with cosine similarity 0.843 (request
`req_01a10c37a77476f2ac3c88da814aab9f`, snapshot `idx_8c8f4429e749c165b062f09c07a0c3ff`); replay
reproduced 5 of 5 chunk ids in the same order and the response hash matched. For scale: the same
ingest on a 2-vCPU Intel Xeon container without Docker (the build sandbox) ran at 4.9 chunks/s and
took 258 s. Embedding is CPU-bound and scales with cores; everything else in Stage 0 is seconds.

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
