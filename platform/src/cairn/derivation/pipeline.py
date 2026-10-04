"""The derivation pipeline: parse, chunk, write chunks, embed the dirty set.

Writes are keyed as the contracts say. Chunks are upserted on ``chunk_id``, which refreshes
``content_sha256``, ordinal and offsets for a chunk that survived an edit. Embeddings are
inserted with ``ON CONFLICT (chunk_id, embedding_model_id) DO NOTHING``: a vector for the same
text under the same model is the same vector, so a retry can never produce a different row.

The dirty set is a query rather than a memory, at two levels. Documents whose latest active
version has no chunks under the live parser and chunker versions are re-derived from their raw
bytes in the object store (this is what a chunker version change triggers: every document is
dirty, every chunk id is new, and the old rows stay under the old version). Chunks of the live
versions with no embedding under the live model are embedded.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from cairn_schemas import ids
from cairn_schemas.models import Chunk, Document, Embedding

from cairn.derivation.chunker import chunk_document, live_versions, parse
from cairn.derivation.embedder import Embedder
from cairn.lakehouse.db import Db
from cairn.lakehouse.objects import ObjectStore

# The dirty set for the live model: chunks of the live versions with no vector yet.
DIRTY_SET_SQL = """
SELECT c.chunk_id, c.tenant_id, c.text, c.text_sha256, c.token_count
FROM chunks c
JOIN latest_documents d ON d.doc_id = c.doc_id AND d.content_sha256 = c.content_sha256
LEFT JOIN embeddings e ON e.chunk_id = c.chunk_id AND e.embedding_model_id = %(model_id)s
WHERE d.status = 'active'
  AND c.parser_version = %(parser_version)s
  AND c.chunker_version = %(chunker_version)s
  AND e.chunk_id IS NULL
ORDER BY c.doc_id, c.ordinal
"""

# Latest active document versions with no chunks under the live parser and chunker versions.
UNDERIVED_SQL = """
SELECT d.*
FROM latest_documents d
WHERE d.status = 'active'
  AND NOT EXISTS (
    SELECT 1 FROM chunks c
    WHERE c.doc_id = d.doc_id AND c.content_sha256 = d.content_sha256
      AND c.parser_version = %(parser_version)s AND c.chunker_version = %(chunker_version)s)
ORDER BY d.source_id, d.uri
"""

UPSERT_CHUNK = """
ON CONFLICT (chunk_id) DO UPDATE SET
  content_sha256 = EXCLUDED.content_sha256,
  ordinal = EXCLUDED.ordinal,
  byte_start = EXCLUDED.byte_start,
  byte_end = EXCLUDED.byte_end,
  metadata = EXCLUDED.metadata,
  acl = EXCLUDED.acl
"""

Progress = Callable[[int, int], None]


@dataclass
class DerivationReport:
    documents: int = 0
    rederived: int = 0  # documents re-chunked from their raw bytes (a version change)
    chunks: int = 0
    embedded: int = 0
    embed_seconds: float = 0.0
    elapsed_seconds: float = 0.0

    @property
    def chunks_per_second(self) -> float:
        return self.embedded / self.embed_seconds if self.embed_seconds > 0 else 0.0

    def line(self) -> str:
        backfill = f"   re-derived {self.rederived} docs" if self.rederived else ""
        return (
            f"derivation            chunks {self.chunks:<6} embedded {self.embedded:<6} "
            f"({self.chunks_per_second:.1f} chunks/s on CPU)   elapsed {self.elapsed_seconds:.1f}s"
            f"{backfill}"
        )


def write_chunks(db: Db, chunks: Sequence[Chunk]) -> int:
    with db.transaction() as tx:
        for chunk in chunks:
            tx.insert(chunk, on_conflict=UPSERT_CHUNK)
    return len(chunks)


def derive_documents(
    db: Db, embedder: Embedder, versions: Sequence[tuple[Document, bytes]]
) -> tuple[int, int]:
    """Parse and chunk changed versions; write the chunks. Returns (documents, chunks)."""
    total = 0
    for document, data in versions:
        if document.status != "active":
            continue
        chunks = chunk_document(document, parse(data), embedder.count_tokens)
        total += write_chunks(db, chunks)
    return len(versions), total


def rederive_missing(db: Db, store: ObjectStore, embedder: Embedder) -> tuple[int, int]:
    """Chunk every active document that has no chunks under the live versions, from its raw bytes.

    Returns (documents re-derived, chunks written). Nothing to do on an ordinary run; after a
    parser or chunker version change this is the backfill, and it needs no flag.
    """
    documents = [Document.model_validate(r) for r in db.rows(UNDERIVED_SQL, live_versions())]
    total = 0
    for document in documents:
        data = store.get(document.raw_object_key)
        chunks = chunk_document(document, parse(data), embedder.count_tokens)
        total += write_chunks(db, chunks)
    return len(documents), total


def dirty_set(db: Db, model_id: str) -> list[dict]:
    return db.rows(DIRTY_SET_SQL, {"model_id": model_id, **live_versions()})


def embed_dirty(
    db: Db,
    embedder: Embedder,
    *,
    job_id: str | None = None,
    batch_size: int = 64,
    progress: Progress | None = None,
) -> tuple[int, float]:
    """Embed every chunk in the dirty set. Returns (rows inserted, seconds spent embedding)."""
    job_id = job_id or ids.new_job_id()
    pending = dirty_set(db, embedder.model_id)
    inserted = 0
    spent = 0.0
    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        # Identical texts share a vector: embed each distinct text once (text_sha256 is the key).
        distinct: dict[str, str] = {}
        for row in batch:
            distinct.setdefault(row["text_sha256"], row["text"])
        shas = list(distinct)
        t0 = time.perf_counter()
        vectors = dict(
            zip(shas, embedder.embed_documents([distinct[s] for s in shas]), strict=True)
        )
        spent += time.perf_counter() - t0
        now = datetime.now(tz=UTC)
        with db.transaction() as tx:
            for row in batch:
                embedding = Embedding(
                    tenant_id=row["tenant_id"],
                    created_at=now,
                    chunk_id=row["chunk_id"],
                    text_sha256=row["text_sha256"],
                    embedding_model_id=embedder.model_id,
                    vector=vectors[row["text_sha256"]],
                    dims=embedder.dims,
                    job_id=job_id,
                    tokens_in=row["token_count"],
                    tokens_out=0,
                    cost_usd=0.0,  # a local model; a hosted embedder prices this row (A11)
                )
                if tx.insert(
                    embedding, on_conflict="ON CONFLICT (chunk_id, embedding_model_id) DO NOTHING"
                ):
                    inserted += 1
        if progress:
            progress(min(start + batch_size, len(pending)), len(pending))
    return inserted, spent


def run(
    db: Db,
    store: ObjectStore,
    embedder: Embedder,
    versions: Sequence[tuple[Document, bytes]],
    *,
    progress: Progress | None = None,
) -> DerivationReport:
    """One ingest run: chunk what changed, backfill what is missing, embed what is dirty."""
    t0 = time.perf_counter()
    report = DerivationReport()
    report.documents, report.chunks = derive_documents(db, embedder, versions)
    report.rederived, backfilled = rederive_missing(db, store, embedder)
    report.chunks += backfilled
    report.embedded, report.embed_seconds = embed_dirty(db, embedder, progress=progress)
    report.elapsed_seconds = time.perf_counter() - t0
    return report
