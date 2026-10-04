# Datasets

The corpora the series runs against, in tiers. Every number in an article names the tier it was
measured on.

| Tier | Where | Contents | Size (measured at `stage-0`) | Arrives |
| --- | --- | --- | --- | --- |
| small | `datasets/small/` | The Apache Iceberg documentation (`iceberg-docs/`, 49 Markdown files) plus the series' own `docs/` folder, ingested as source `docs-series` | 55 documents, 1,244 chunks, 263K tokens, 1.0 MB of raw bytes | `stage-0` (A3) |
| medium | not in the repository | About 50K documents fetched by the connectors of Chapter 2 | | Stage 1 |
| large | not in the repository | The 50M-document target the architecture is sized for | | A39 |

Golden sets live in `datasets/golden/<set>.jsonl`: one JSON object per line with a `question` and
the `expected` documents as `<source_id>/<path within the source>`. `cairn eval --set small`
runs one through `/ask` and writes an `EvalRun`. A set's version is the hash of its file, so
editing a line is a new version and runs on different versions are not compared.

Licences and attribution: `LICENSES.md`.

## Ingesting your own folder

Any folder of Markdown works. Inside the api container the repository's `datasets/` is mounted
at `/data/datasets`, so put a folder under `datasets/` and name it as a source:

```bash
docker compose -f infra/compose/base.yml --profile core exec api \
  cairn ingest my-notes=/data/datasets/my-notes
make build-index
make ask Q="..."
```

A document's identity is its path within the source (`fs://my-notes/<relative path>`), so you can
move the folder without re-ingesting everything, and editing a file is a new revision of the same
document rather than a new document.
