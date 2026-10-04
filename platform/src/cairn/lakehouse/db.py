"""Database access: a connection pool, transactions, and inserts straight from contract rows.

``Db.insert(row)`` writes any ``cairn_schemas`` model into its table. Column types follow the
Postgres generator (``cairn_schemas.generate.postgres``): nested records and maps travel as
JSONB, string lists as TEXT[], vectors as REAL[]. Reading back is the reverse:
``Model.model_validate(dict_row)`` re-runs every validator, so a corrupted row refuses to
load rather than flowing on silently.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from cairn_schemas.base import Row
from cairn_schemas.generate._fields import fields_of
from cairn_schemas.generate.postgres import jsonb_columns
from cairn_schemas.models import Document

Params = Sequence[Any] | dict[str, Any] | None


def row_values(row: Row) -> tuple[list[str], list[Any]]:
    """Column names and adapted values for an INSERT of this row."""
    as_json = row.model_dump(mode="json")
    as_python = row.model_dump()
    jsonb = jsonb_columns(type(row))
    columns: list[str] = []
    values: list[Any] = []
    for spec in fields_of(type(row)):
        value = as_python[spec.name]
        if spec.name in jsonb and value is not None:
            value = Jsonb(as_json[spec.name])
        columns.append(spec.name)
        values.append(value)
    return columns, values


class Tx:
    """One transaction. Every method runs on the same connection."""

    def __init__(self, conn: psycopg.Connection[dict[str, Any]]):
        self.conn = conn

    def execute(self, query: str | sql.Composed, params: Params = None) -> int:
        return self.conn.execute(query, params).rowcount

    def rows(self, query: str | sql.Composed, params: Params = None) -> list[dict[str, Any]]:
        return self.conn.execute(query, params).fetchall()

    def row(self, query: str | sql.Composed, params: Params = None) -> dict[str, Any] | None:
        return self.conn.execute(query, params).fetchone()

    def scalar(self, query: str | sql.Composed, params: Params = None) -> Any:
        found = self.conn.execute(query, params).fetchone()
        if found is None:
            return None
        return next(iter(found.values()))

    def insert(self, row: Row, *, on_conflict: str = "") -> bool:
        """INSERT one contract row. Returns False when ``on_conflict`` swallowed it."""
        columns, values = row_values(row)
        query = sql.SQL("INSERT INTO {table} ({columns}) VALUES ({values}) {conflict}").format(
            table=sql.Identifier(row.TABLE),
            columns=sql.SQL(", ").join(sql.Identifier(c) for c in columns),
            values=sql.SQL(", ").join(sql.Placeholder() for _ in columns),
            conflict=sql.SQL(on_conflict),
        )
        return self.conn.execute(query, values).rowcount == 1

    def latest_document(self, doc_id: str) -> Document | None:
        found = self.row("SELECT * FROM latest_documents WHERE doc_id = %s", (doc_id,))
        return Document.model_validate(found) if found else None


class Db:
    """A pool of connections to the one database. Use ``with db.transaction() as tx:``."""

    def __init__(self, url: str, *, min_size: int = 1, max_size: int = 8):
        self.url = url
        self._pool: ConnectionPool[psycopg.Connection[dict[str, Any]]] = ConnectionPool(
            url,
            min_size=min_size,
            max_size=max_size,
            open=True,
            kwargs={"row_factory": dict_row},
        )

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def transaction(self) -> Iterator[Tx]:
        with self._pool.connection() as conn, conn.transaction():
            yield Tx(conn)

    # One-shot conveniences: each runs in its own transaction.

    def execute(self, query: str | sql.Composed, params: Params = None) -> int:
        with self.transaction() as tx:
            return tx.execute(query, params)

    def rows(self, query: str | sql.Composed, params: Params = None) -> list[dict[str, Any]]:
        with self.transaction() as tx:
            return tx.rows(query, params)

    def row(self, query: str | sql.Composed, params: Params = None) -> dict[str, Any] | None:
        with self.transaction() as tx:
            return tx.row(query, params)

    def scalar(self, query: str | sql.Composed, params: Params = None) -> Any:
        with self.transaction() as tx:
            return tx.scalar(query, params)

    def insert(self, row: Row, *, on_conflict: str = "") -> bool:
        with self.transaction() as tx:
            return tx.insert(row, on_conflict=on_conflict)

    def latest_document(self, doc_id: str) -> Document | None:
        with self.transaction() as tx:
            return tx.latest_document(doc_id)

    def ping(self) -> bool:
        try:
            return self.scalar("SELECT 1") == 1
        except psycopg.Error:
            return False
