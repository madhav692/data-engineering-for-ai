"""Cairn: one AI knowledge platform, built in stages.

Stage 0 (A3) is the walking skeleton: every one of the seven subsystems from A2 present as
code, each as thin as its contracts allow, running from one Docker Compose file.

    ingestion/      1. Ingestion and registry     filesystem connector, Document rows, raw bytes
    lakehouse/      2. Lakehouse                  Postgres (ADR-0003) + an S3 store; migrations
    derivation/     3. Derivation                 Markdown chunker, embedder, the dirty set
    indexes/        4. Indexes                    pgvector tables, IndexSnapshot, the lag query
    serving/        5. Serving and lineage        FastAPI /ask, retrieval, generators
    lineage/        5. Serving and lineage        the request log and replay
    evaluation/     6. Feedback and evaluation    the golden set and cairn eval
    observability/  7. Observability and cost     JSON logs and /metrics

The invariant Stage 0 establishes: every request is logged with the versions of its inputs.
"""

__version__ = "0.1.0"
