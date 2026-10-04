# Cairn

The AI knowledge platform behind *Data Engineering for AI*, built in stages. Each stage is a
tag and a working system; the article for each stage is written from the tag.

| Tag | Stage | Article |
| --- | --- | --- |
| `stage-0` | The walking skeleton: every subsystem present, each as thin as its contracts allow, one Docker Compose file | A3 |

## Run Stage 0

You need Docker (Desktop or Engine) with Compose v2, `make`, and 16 GB of memory. Nothing
else: no Python, no API key, no GPU.

```bash
make start        # three containers: postgres, seaweedfs, api
make ingest       # the series' own docs and the Apache Iceberg documentation
make build-index  # one index table, one IndexSnapshot row
make ask Q="How does Iceberg hidden partitioning work?"
make replay REQ=<the request_id it printed>
make test         # the platform suite (16 unit + 13 seam tests) against the running containers
```

More: `make lag`, `make eval`, `make feedback REQ=... KIND=thumbs VALUE=-1`, `make stats`,
`make logs`, `make stop`, `make down` (removes the volumes too). `make help` lists everything.

Swap the generator for one question (any OpenAI-compatible endpoint, here Ollama on the host):

```bash
CAIRN_LLM_BASE_URL=http://host.docker.internal:11434/v1 CAIRN_LLM_MODEL=llama3.2 CAIRN_LLM_PROVIDER=ollama \
  make ask Q="How does Iceberg hidden partitioning work?"
make replay REQ=<first request_id> DIFF=<second request_id>
```

## Layout

```text
platform/
├── Dockerfile                    python:3.12-slim + uv; the api image
├── CHANGES-stage-0.md            what the stage adds, the invariant, the measured numbers
├── src/cairn/
│   ├── cli.py                    migrate · ingest · build-index · ask · replay · feedback · eval · lag · stats · serve
│   ├── config.py                 CAIRN_* settings
│   ├── ingestion/filesystem.py   1. walk a folder; Document.from_bytes; is_change; tombstones
│   ├── lakehouse/                2. db.py (pool, inserts from contract rows), objects.py (S3 / fs), migrate.py
│   ├── derivation/               3. chunker.py (md-raw-v1, md-struct-v1), embedder.py (fastembed, hashing), pipeline.py (the dirty set)
│   ├── indexes/                  4. build.py (pgvector table per snapshot, the live pointer), reconcile.py (the lag query)
│   ├── serving/                  5. app.py (/ask, /feedback, /metrics, /healthz), retrieval.py, generator.py, prompts/answer-v1.md
│   ├── lineage/                  5. request_log.py, replay.py (replay and --diff)
│   ├── evaluation/               6. golden.py, runner.py (recall@5 -> EvalRun)
│   └── observability/            7. logging.py (JSON lines), metrics.py (/metrics from the tables)
└── tests/
    ├── unit/                     the chunker, the hashing embedder, the generator protocol
    └── integration/              thirteen tests, one per seam, against a real Postgres
```

## Versions at Stage 0

Every one of these is written into every `Request` row; that is the Stage 0 invariant.

| Lineage field | Value |
| --- | --- |
| `parser_version` | `md-raw-v1` |
| `chunker_version` | `md-struct-v1` |
| `embedding_model_id` | `baai/bge-small-en@v1.5:384` |
| `prompt_template_version` | `answer-v1` |
| `retrieval_config_version` | `vector-k5-v1` |
| `llm_id` | `cairn/extractive@0` by default; `{provider}/{model}@{version}` with an endpoint |
| `reranker_id` | none |

## Configuration

| Variable | Default (in the api container) | Purpose |
| --- | --- | --- |
| `CAIRN_DATABASE_URL` | `postgresql://cairn:cairn@postgres:5432/cairn` | The one database |
| `CAIRN_OBJECT_STORE` | `s3` | `s3` (any S3 API) or `fs` (a directory) |
| `CAIRN_S3_ENDPOINT`, `CAIRN_S3_BUCKET` | `http://seaweedfs:8333`, `cairn` | Raw objects |
| `CAIRN_TENANT_ID` | `local` | Single tenant until A22 |
| `CAIRN_EMBEDDER` | `fastembed` | `fastembed`, or `hashing` (an offline test double) |
| `CAIRN_EMBEDDING_MODEL_ID` | `baai/bge-small-en@v1.5:384` | The namespace of every vector and index |
| `CAIRN_MODEL_CACHE` | `/models` | Where fastembed keeps the ONNX model (a volume) |
| `CAIRN_LLM_BASE_URL`, `CAIRN_LLM_MODEL`, `CAIRN_LLM_API_KEY`, `CAIRN_LLM_PROVIDER` | unset | When set, the OpenAI-compatible generator replaces the extractive one |
| `CAIRN_LLM_PRICE_IN_PER_1M`, `CAIRN_LLM_PRICE_OUT_PER_1M` | unset | USD per million tokens; overrides the small built-in price table |
| `CAIRN_PROMPT_TEMPLATE_VERSION` | `answer-v1` | Names the file under `serving/prompts/` |
| `CAIRN_RETRIEVAL_CONFIG_VERSION` | `vector-k5-v1` | Vector only, k = 5, cosine |
| `CAIRN_INDEX_RETAIN` | `2` | Retired snapshots whose tables are kept for replay and rollback |

## Developing without Docker

`make setup` installs an embedded Postgres with pgvector (`pgserver`) in the dev group, so the
integration suite also runs on the host: `make test-platform`. It uses the hashing embedder and
a temporary directory as the object store by default (no model download, no S3). Three variables
turn the thin ends real: `CAIRN_TEST_EMBEDDER=fastembed` for the model,
`CAIRN_TEST_S3_ENDPOINT=http://localhost:8333` for the compose SeaweedFS (the tests empty and reuse
+one bucket, `cairn-test`, never `cairn`), and `CAIRN_TEST_DATABASE_URL=postgresql://cairn:cairn@localhost:5432/cairn` for the
compose Postgres (the tests create and use a `cairn_test` database beside it, never the one you
ingested into). `make test` runs the suite inside the api container with all three real.
