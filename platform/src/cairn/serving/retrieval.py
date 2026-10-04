"""Vector retrieval against the live snapshot's table.

``vector-k5-v1``: cosine distance, five nearest, the tenant filter applied inside the query
rather than on the results. No hybrid, no reranker, no cache: each is a component behind C5
that arrives at a named stage without changing this interface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from psycopg import sql

from cairn_schemas.models import IndexSnapshot

from cairn.indexes.build import table_exists, table_name_for
from cairn.lakehouse.db import Db, Tx

_CONFIG = re.compile(r"^vector-k(?P<k>\d+)-v(?P<v>\d+)$")


@dataclass(frozen=True)
class Hit:
    rank: int
    chunk_id: str
    doc_id: str
    uri: str
    text: str
    text_sha256: str
    token_count: int
    metadata: dict[str, str]
    score: float  # cosine similarity after retrieval; higher is closer


@dataclass(frozen=True)
class RetrievalConfig:
    version: str
    k: int

    @classmethod
    def parse(cls, version: str) -> RetrievalConfig:
        """The configuration a version string names; replay rebuilds it from a Request row."""
        match = _CONFIG.match(version)
        if not match:
            raise ValueError(f"unknown retrieval_config_version {version!r}")
        return cls(version=version, k=int(match.group("k")))


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{x:.8g}" for x in vector) + "]"


class VectorRetrieval:
    def __init__(self, config: RetrievalConfig):
        self.config = config

    @property
    def config_version(self) -> str:
        return self.config.version

    def search(
        self,
        db: Db | Tx,
        snapshot: IndexSnapshot,
        query_vector: list[float],
        *,
        tenant_id: str,
        k: int | None = None,
    ) -> list[Hit]:
        table = table_name_for(snapshot.index_snapshot_id, snapshot.index_name)
        query = sql.SQL(
            "SELECT chunk_id, doc_id, uri, text, text_sha256, token_count, metadata, "
            "1 - (embedding <=> %(q)s::vector) AS score FROM {} "
            "WHERE tenant_id = %(tenant)s "
            "ORDER BY embedding <=> %(q)s::vector LIMIT %(k)s"
        ).format(sql.Identifier(table))
        params: dict[str, Any] = {
            "q": vector_literal(query_vector),
            "tenant": tenant_id,
            "k": k or self.config.k,
        }
        return [
            Hit(
                rank=i + 1,
                chunk_id=r["chunk_id"],
                doc_id=r["doc_id"],
                uri=r["uri"],
                text=r["text"],
                text_sha256=r["text_sha256"],
                token_count=int(r["token_count"]),
                metadata=dict(r["metadata"] or {}),
                score=float(r["score"]),
            )
            for i, r in enumerate(db.rows(query, params))
        ]

    def table_available(self, db: Db | Tx, snapshot: IndexSnapshot) -> bool:
        table = table_name_for(snapshot.index_snapshot_id, snapshot.index_name)
        if isinstance(db, Tx):
            return table_exists(db, table)
        with db.transaction() as tx:
            return table_exists(tx, table)
