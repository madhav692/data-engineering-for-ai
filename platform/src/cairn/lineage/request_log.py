"""The request log (contract C6): every request, with the version of every input it used,
written before the response is returned. Hot copy in Postgres; history moves to the lakehouse at
Stage 1 (A4 is about this table)."""

from __future__ import annotations

from cairn_schemas.models import Request

from cairn.lakehouse.db import Db


class RequestLog:
    def __init__(self, db: Db):
        self.db = db

    def write(self, row: Request) -> None:
        self.db.insert(row)

    def read(self, request_id: str) -> Request | None:
        found = self.db.row("SELECT * FROM requests WHERE request_id = %s", (request_id,))
        return Request.model_validate(found) if found else None

    def exists(self, request_id: str) -> bool:
        return self.db.scalar("SELECT 1 FROM requests WHERE request_id = %s", (request_id,)) == 1

    def recent(self, limit: int = 20) -> list[Request]:
        rows = self.db.rows("SELECT * FROM requests ORDER BY received_at DESC LIMIT %s", (limit,))
        return [Request.model_validate(r) for r in rows]
