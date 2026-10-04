# cairn-schemas

The data contracts of the Cairn platform: seven Pydantic models that are the single source
of truth, and generators that emit the same contracts as **Avro** (Kafka), **Iceberg DDL**
(the lakehouse), **JSON Schema** (the API) and **Postgres DDL** (the Stage 0 lakehouse
stand-in, ADR-0003). Series articles: A2, *Reference Architecture: Seven Subsystems and the
Contracts Between Them*; A3, *Cairn Stage 0*.

## What is here

```text
packages/schemas/
├── src/cairn_schemas/
│   ├── base.py          Row (tenant_id, created_at, schema_version + contract metadata), Costed
│   ├── ids.py           derived identifiers: doc_id, chunk_id, index_snapshot_id, raw_object_key, uuid7
│   ├── versions.py      EmbeddingModelId, ModelRef, VersionTag, Sha256 value types
│   ├── models.py        Document, Chunk, Embedding, IndexSnapshot, Request, Feedback, EvalRun
│   └── generate/        _fields.py (type walker), avro.py, iceberg.py, jsonschema.py, postgres.py, __main__.py
├── generated/           committed outputs; `make check-schemas` fails CI if they drift
│   ├── avro/<table>.avsc
│   ├── iceberg/<table>.sql
│   ├── jsonschema/<Model>.json
│   ├── postgres/<table>.sql   applied by `cairn migrate` at Stage 0
│   └── manifest.json    table, primary key, lineage fields, partition spec, per model
└── tests/               ids, model invariants, contracts (C1, C3, C6), generators
```

## The lineage spine

```text
Document -> Chunk -> Embedding -> IndexSnapshot -> Request -> Feedback -> EvalRun
```

| Model | Table | Primary key | Lineage fields (the version that made it) |
| --- | --- | --- | --- |
| `Document` | `documents` | `doc_id, revision` | `content_sha256` |
| `Chunk` | `chunks` | `chunk_id` | `content_sha256, parser_version, chunker_version` |
| `Embedding` | `embeddings` | `chunk_id, embedding_model_id` | `embedding_model_id` |
| `IndexSnapshot` | `index_snapshots` | `index_snapshot_id` | `embedding_model_id, lakehouse_snapshot_id` |
| `Request` | `requests` | `request_id` | `prompt_template_version, retrieval_config_version, index_snapshot_id, embedding_model_id, reranker_id, llm_id` |
| `Feedback` | `feedback` | `feedback_id` | `event_schema_version` |
| `EvalRun` | `eval_runs` | `run_id` | `eval_set_id, eval_set_version` |

Every row carries `tenant_id`, `created_at` and `schema_version`. Rows that touched a model
(`Embedding`, `Request`, `EvalRun`) also carry `tokens_in`, `tokens_out`, `cost_usd`.

## ID conventions

| Identifier | Derived from | Why |
| --- | --- | --- |
| `doc_id` | `source_id` + canonical URI | Identity survives edits; a refetch is the same document |
| `content_sha256` | raw bytes | The document's version; one changed byte is a new version |
| `chunk_id` | `doc_id`, `parser_version`, `chunker_version`, `text_sha256`, `occurrence` | Unchanged text keeps its id across revisions and position shifts, so only changed chunks are re-embedded |
| `index_snapshot_id` | `index_name`, `embedding_model_id`, `lakehouse_snapshot_id` | An index is a function of one snapshot and one model, and says which |
| `index_name` | `{base}--{model slug}` | Versions are namespaces: two models never share an index |
| `request_id`, `feedback_id`, `run_id`, `job_id` | minted (UUIDv7) | Opaque and time-ordered; the hot request log stays append-friendly |

## Use

```bash
make setup            # uv sync
make schemas          # regenerate packages/schemas/generated/
make check-schemas    # what CI runs: fail on drift
make test-contracts   # pytest packages/schemas/tests
```

```python
from cairn_schemas import Document, Chunk

doc = Document.from_bytes(
    tenant_id="acme",
    source_id="docs-site",
    uri="https://docs.example.com/policies/refunds",
    data=raw_bytes,
    content_type="text/markdown",
)
chunk = Chunk.build(
    document=doc,
    parser_version="md-1.0",
    chunker_version="para-v1",
    ordinal=0,
    byte_start=0,
    byte_end=42,
    text=first_paragraph,
    token_count=9,
)
```

## Changing a contract

1. Edit the model in `models.py`; bump `schema_version` on it if any field changes shape.
2. Run `make schemas` and commit the regenerated files with the model change, in one commit.
3. Run `make test-contracts`; add or adjust the test that proves the guarantee you changed.
4. If a guarantee in `docs/architecture/reference.md` changed, update the row. If the change is
   a design decision, add an ADR under `docs/adr/`.

Consumers own the tests; producers own the schema. Both live in this package so a change to
either is one pull request with both halves visible.
