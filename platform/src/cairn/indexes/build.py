"""``cairn build-index``: one pgvector table per index snapshot, one ``IndexSnapshot`` row.

The index name carries the embedding model (``chunks--baai-bge-small-en-v1-5-384``): versions
are namespaces, two models never share an index. Each build writes a fresh table named after
the snapshot, fills it from embeddings ⨝ chunks ⨝ latest_documents for the live versions,
builds an HNSW index, and writes an ``IndexSnapshot`` row that says what it read: which model,
and which state of the lakehouse (``lakehouse_snapshot_id``; at Stage 0 the time of the newest
embedding it read, at Stage 1 an Iceberg snapshot id and nothing else changes).

The row with ``status='live'`` is the pointer. Retrieval asks for the live snapshot and reads
its table, so a build flips the pointer and a rollback would flip it back. The tables of the
last ``retain`` retired snapshots are kept so that replay can search exactly what a request
searched; older tables are dropped.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from psycopg import sql

from cairn_schemas import ids
from cairn_schemas.models import IndexSnapshot
from cairn_schemas.versions import parse_embedding_model_id

from cairn.derivation.chunker import live_versions
from cairn.indexes.reconcile import lag_for
from cairn.lakehouse.db import Db, Tx

_UNSAFE = re.compile(r"[^a-z0-9_]+")


def table_name_for(snapshot_id: str, index_name: str) -> str:
    """``idx_<index name>_<12 hex of the snapshot id>``, a valid Postgres identifier."""
    slug = _UNSAFE.sub("_", index_name.lower()).strip("_")
    suffix = snapshot_id.split("_", 1)[1][:12]
    name = f"idx_{slug}_{suffix}"
    if len(name) > 63:  # Postgres identifier limit
        name = f"idx_{slug[:40].rstrip('_')}_{suffix}"
    return name


def table_exists(tx: Tx, table: str) -> bool:
    return tx.scalar("SELECT to_regclass(%s) IS NOT NULL", (table,)) is True


@dataclass(frozen=True)
class BuildResult:
    snapshot: IndexSnapshot
    table: str
    seconds: float
    dropped_tables: tuple[str, ...]


def build_index(
    db: Db,
    model_id: str,
    *,
    tenant_id: str,
    base_name: str = "chunks",
    retain: int = 2,
) -> BuildResult:
    """Build the index for ``model_id`` from the lakehouse and flip the live pointer.

    One transaction: the table, its rows, the HNSW index, the snapshot row, the pointer. A
    rebuild of an unchanged lakehouse state has the same snapshot id and replaces its own table,
    so concurrent queries on that table wait for the transaction; a build after new embeddings
    writes a new table and never touches the live one.
    """
    t0 = time.perf_counter()
    name = ids.index_name_for(base_name, model_id)  # chunks--baai-bge-small-en-v1-5-384
    dims = parse_embedding_model_id(model_id).dims
    with db.transaction() as tx:
        snapshot_at: datetime | None = tx.scalar(
            "SELECT max(created_at) FROM embeddings WHERE embedding_model_id = %s", (model_id,)
        )
        if snapshot_at is None:
            raise RuntimeError(f"nothing to index for {model_id}: run `cairn ingest` first")
        lakehouse_snapshot_id = snapshot_at.astimezone(UTC).isoformat()
        snapshot_id = ids.index_snapshot_id(name, model_id, lakehouse_snapshot_id)
        table = table_name_for(snapshot_id, name)
        ident = sql.Identifier(table)
        hnsw = sql.Identifier(f"{table}_hnsw")

        tx.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(ident))
        tx.execute(
            sql.SQL(
                "CREATE TABLE {} (chunk_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, "
                "tenant_id TEXT NOT NULL, uri TEXT NOT NULL, text TEXT NOT NULL, "
                "text_sha256 TEXT NOT NULL, token_count BIGINT NOT NULL, metadata JSONB NOT NULL, "
                "acl TEXT[] NOT NULL, embedding vector({}) NOT NULL)"
            ).format(ident, sql.Literal(dims))
        )
        tx.execute(
            sql.SQL(
                "INSERT INTO {} SELECT c.chunk_id, c.doc_id, c.tenant_id, d.uri, c.text, "
                "c.text_sha256, c.token_count, c.metadata, c.acl, e.vector::vector "
                "FROM embeddings e JOIN chunks c USING (chunk_id) "
                "JOIN latest_documents d ON d.doc_id = c.doc_id "
                "AND d.content_sha256 = c.content_sha256 "
                "WHERE e.embedding_model_id = %(model_id)s AND d.status = 'active' "
                "AND c.parser_version = %(parser_version)s "
                "AND c.chunker_version = %(chunker_version)s"
            ).format(ident),
            {"model_id": model_id, **live_versions()},
        )
        tx.execute(
            sql.SQL("CREATE INDEX {} ON {} USING hnsw (embedding vector_cosine_ops)").format(
                hnsw, ident
            )
        )
        row_count = tx.scalar(sql.SQL("SELECT count(*) FROM {}").format(ident))
        built_at = datetime.now(tz=UTC)
        lag_at_build = max([0, *lag_for(tx, built_at).values()])
        snapshot = IndexSnapshot(
            tenant_id=tenant_id,
            created_at=built_at,
            index_snapshot_id=snapshot_id,
            index_name=name,
            embedding_model_id=model_id,
            lakehouse_snapshot_id=lakehouse_snapshot_id,
            built_at=built_at,
            row_count=row_count,
            lag_seconds_at_build=int(lag_at_build),
            status="live",
        )
        # A rebuild of the same lakehouse state has the same id: replace the row, not duplicate it.
        tx.execute("DELETE FROM index_snapshots WHERE index_snapshot_id = %s", (snapshot_id,))
        tx.execute(
            "UPDATE index_snapshots SET status = 'retired' "
            "WHERE index_name = %s AND status = 'live'",
            (name,),
        )
        tx.insert(snapshot)
        dropped = _drop_beyond_retention(tx, name, retain)
    return BuildResult(snapshot, table, time.perf_counter() - t0, dropped)


def _drop_beyond_retention(tx: Tx, index_name: str, retain: int) -> tuple[str, ...]:
    """Keep the live table and the ``retain`` newest retired ones; drop the rest."""
    retired = tx.rows(
        "SELECT index_snapshot_id FROM index_snapshots "
        "WHERE index_name = %s AND status = 'retired' ORDER BY built_at DESC",
        (index_name,),
    )
    dropped = []
    for row in retired[retain:]:
        table = table_name_for(row["index_snapshot_id"], index_name)
        if table_exists(tx, table):
            tx.execute(sql.SQL("DROP TABLE {}").format(sql.Identifier(table)))
            dropped.append(table)
    return tuple(dropped)


def rollback(db: Db, index_name: str) -> IndexSnapshot:
    """Flip the live pointer back to the newest retired snapshot whose table still exists."""
    with db.transaction() as tx:
        live = tx.row(
            "SELECT * FROM index_snapshots WHERE index_name = %s AND status = 'live'", (index_name,)
        )
        candidates = tx.rows(
            "SELECT * FROM index_snapshots WHERE index_name = %s AND status = 'retired' "
            "ORDER BY built_at DESC",
            (index_name,),
        )
        for candidate in candidates:
            if table_exists(tx, table_name_for(candidate["index_snapshot_id"], index_name)):
                if live:
                    tx.execute(
                        "UPDATE index_snapshots SET status = 'retired' "
                        "WHERE index_snapshot_id = %s",
                        (live["index_snapshot_id"],),
                    )
                tx.execute(
                    "UPDATE index_snapshots SET status = 'live' WHERE index_snapshot_id = %s",
                    (candidate["index_snapshot_id"],),
                )
                return IndexSnapshot.model_validate({**candidate, "status": "live"})
    raise RuntimeError(f"no retained retired snapshot to roll back to for {index_name}")
