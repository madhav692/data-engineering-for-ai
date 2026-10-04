"""Shared test material: the corpus, the edit, and the Platform object the fixtures build."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg
from fastapi.testclient import TestClient

from cairn_schemas.models import Request

from cairn.config import Settings
from cairn.derivation import pipeline
from cairn.derivation.embedder import Embedder
from cairn.derivation.pipeline import DerivationReport
from cairn.indexes.build import BuildResult, build_index
from cairn.ingestion.filesystem import SourceReport, ingest_source
from cairn.lakehouse.db import Db
from cairn.lakehouse.objects import ObjectStore
from cairn.serving.app import build_deps, create_app

SOURCE_ID = "docs"

CORPUS: dict[str, str] = {
    "partitioning.md": """---
title: Partitioning
---
<!-- a license header, stripped by md-raw-v1 -->
# Partitioning

## What is partitioning?

Partitioning is a way to make queries faster by grouping similar rows together when writing.
Queries for log entries usually include a time range, and a table partitioned by the date of
the event time groups log events into files with the same event date.

## Hidden partitioning

Iceberg handles the tedious and error-prone task of producing partition values for rows in a
table. Iceberg avoids reading unnecessary partitions automatically; consumers do not need to
know how the table is partitioned and do not add extra filters to their queries.

Because the partition values are hidden, partition layouts can evolve as needed without
breaking queries that were written against the old layout.
""",
    "snapshots.md": """# Snapshots

## What a snapshot is

A snapshot is the state of a table at some time. Each snapshot lists all of the data files
that make up the table's contents at the time of the snapshot.

## Time travel

Readers can use the snapshot id or a timestamp to read an older state of the table. Writers
produce a new snapshot on every commit and never modify an existing one.
""",
    "policy.md": """# Refund policy

## Window

Refunds are accepted within 30 days of purchase.

## Exceptions

Digital goods are refundable only when they were never downloaded.
""",
}

POLICY_EDIT = ("30 days", "14 days")


def _test_url_for(url: str) -> str:
    parts = urlsplit(url)
    dbname = parts.path.lstrip("/") or "postgres"
    if dbname.endswith("_test"):
        return url
    test_db = f"{dbname}_test"
    with psycopg.connect(url, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (test_db,)).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{test_db}"')
    return urlunsplit((parts.scheme, parts.netloc, f"/{test_db}", parts.query, parts.fragment))


def _reachable(url: str) -> bool:
    try:
        with psycopg.connect(url, connect_timeout=3):
            return True
    except psycopg.Error:
        return False


@dataclass
class Platform:
    """Everything a test needs, wired to one fresh schema."""

    settings: Settings
    db: Db
    store: ObjectStore
    embedder: Embedder
    corpus: Path
    _client: TestClient | None = None

    # -- the three commands --------------------------------------------------------------------

    def ingest(self, *, prune: bool = False) -> tuple[SourceReport, DerivationReport]:
        report = ingest_source(
            self.db,
            self.store,
            root=self.corpus,
            source_id=SOURCE_ID,
            tenant_id=self.settings.tenant_id,
            prune=prune,
        )
        derivation = pipeline.run(self.db, self.store, self.embedder, report.changed)
        return report, derivation

    def build(self) -> BuildResult:
        return build_index(
            self.db,
            self.embedder.model_id,
            tenant_id=self.settings.tenant_id,
            retain=self.settings.index_retain,
        )

    # -- the api --------------------------------------------------------------------------------

    @property
    def client(self) -> TestClient:
        if self._client is None:
            deps = build_deps(self.settings, db=self.db, embedder=self.embedder)
            self._client = TestClient(create_app(deps=deps))
            self._client.__enter__()
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.__exit__(None, None, None)

    def ask(self, question: str, generator: dict[str, Any] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"question": question}
        if generator:
            body["generator"] = generator
        response = self.client.post("/ask", json=body)
        assert response.status_code == 200, response.text
        return response.json()

    def request(self, request_id: str) -> Request:
        row = self.db.row("SELECT * FROM requests WHERE request_id = %s", (request_id,))
        assert row is not None, f"no request row for {request_id}"
        return Request.model_validate(row)

    # -- helpers ---------------------------------------------------------------------------------

    def count(self, table: str, where: str = "", params: tuple = ()) -> int:
        clause = f" WHERE {where}" if where else ""
        return int(self.db.scalar(f"SELECT count(*) FROM {table}{clause}", params))  # noqa: S608

    def edit(self, name: str, old: str, new: str, *, mtime: float | None = None) -> Path:
        path = self.corpus / name
        text = path.read_text(encoding="utf-8")
        assert old in text, f"{old!r} not in {name}"
        path.write_text(text.replace(old, new), encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def golden_file(self, questions: list[tuple[str, list[str]]]) -> Path:
        path = self.corpus.parent / "golden.jsonl"
        path.write_text(
            "\n".join(json.dumps({"question": q, "expected": e}) for q, e in questions) + "\n",
            encoding="utf-8",
        )
        return path
