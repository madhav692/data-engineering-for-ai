"""The filesystem connector: a folder of Markdown files becomes rows in the registry.

A document's identity is ``doc_id = hash(source_id, uri)`` where the URI names the file's place
*within the source* (``fs://{source_id}/{relative path}``), not where the folder happens to be
mounted, so the same corpus has the same ids on a laptop, in the api container and in CI. Its
version is ``content_sha256``, the hash of the bytes. The registry keeps one row per
(document, revision); a file that disappeared gets a revision with ``status='deleted'`` when
the connector runs with ``prune=True``. A delete is a row, never an absence (contract C1).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from cairn_schemas import ids
from cairn_schemas.models import Document

from cairn.lakehouse.db import Db
from cairn.lakehouse.objects import ObjectStore

URI_SCHEME = "fs"
CONTENT_TYPE = "text/markdown"


def document_uri(source_id: str, relpath: str) -> str:
    return f"{URI_SCHEME}://{source_id}/{Path(relpath).as_posix()}"


def display_path(uri: str) -> str:
    """``fs://iceberg-docs/partitioning.md`` -> ``iceberg-docs/partitioning.md`` (for citations)."""
    return uri.split("://", 1)[1] if "://" in uri else uri


def walk(root: Path) -> Iterator[Path]:
    """Every ``*.md`` under ``root``, sorted, skipping hidden files and directories."""
    for path in sorted(root.rglob("*.md")):
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        if path.is_file():
            yield path


def mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)


@dataclass(frozen=True)
class Ingested:
    status: str  # "registered" | "unchanged"
    document: Document
    data: bytes


@dataclass
class SourceReport:
    source_id: str
    root: Path
    files: int = 0
    registered: int = 0
    unchanged: int = 0
    tombstoned: int = 0
    # Changed, active versions and their bytes, for derivation.
    changed: list[tuple[Document, bytes]] = field(default_factory=list)

    def line(self) -> str:
        return (
            f"source {self.source_id:<14} {self.files:>5} files   registered {self.registered:<5} "
            f"unchanged {self.unchanged:<5} tombstoned {self.tombstoned}"
        )


def ingest_file(
    db: Db,
    store: ObjectStore,
    path: Path,
    *,
    root: Path,
    source_id: str,
    tenant_id: str,
) -> Ingested:
    """Register one file: ``registered`` (a new revision was written) or ``unchanged``."""
    data = path.read_bytes()
    uri = document_uri(source_id, path.relative_to(root).as_posix())
    latest = db.latest_document(ids.doc_id(source_id, uri))
    candidate = Document.from_bytes(
        tenant_id=tenant_id,
        source_id=source_id,
        uri=uri,
        data=data,
        content_type=CONTENT_TYPE,
        source_updated_at=mtime(path),
        revision=(latest.revision + 1) if latest else 1,
    )
    if not Document.is_change(latest, candidate):
        assert latest is not None
        return Ingested("unchanged", latest, data)  # C1: same bytes, same status, nothing written
    store.put(candidate.raw_object_key, data, CONTENT_TYPE)  # raw/{source}/{sha[:2]}/{sha}
    db.insert(candidate)
    return Ingested("registered", candidate, data)


def tombstone(db: Db, latest: Document) -> Document:
    """A delete is a new revision of the same document with ``status='deleted'``."""
    now = datetime.now(tz=UTC)
    row = Document(
        tenant_id=latest.tenant_id,
        created_at=now,
        doc_id=latest.doc_id,
        revision=latest.revision + 1,
        source_id=latest.source_id,
        uri=latest.uri,
        content_sha256=latest.content_sha256,
        content_type=latest.content_type,
        size_bytes=latest.size_bytes,
        fetched_at=now,
        source_updated_at=None,  # the source has no timestamp for a file that is gone
        acl=latest.acl,
        status="deleted",
        raw_object_key=latest.raw_object_key,
    )
    db.insert(row)
    return row


def ingest_source(
    db: Db,
    store: ObjectStore,
    *,
    root: Path,
    source_id: str,
    tenant_id: str,
    prune: bool = False,
) -> SourceReport:
    """Walk one source folder; register what changed; optionally tombstone what disappeared."""
    root = root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"source {source_id}: {root} is not a directory")
    report = SourceReport(source_id=source_id, root=root)
    seen: set[str] = set()
    for path in walk(root):
        report.files += 1
        result = ingest_file(db, store, path, root=root, source_id=source_id, tenant_id=tenant_id)
        seen.add(result.document.uri)
        if result.status == "registered":
            report.registered += 1
            report.changed.append((result.document, result.data))
        else:
            report.unchanged += 1
    if prune:
        active = db.rows(
            "SELECT * FROM latest_documents WHERE source_id = %s AND tenant_id = %s "
            "AND status = 'active' ORDER BY uri",
            (source_id, tenant_id),
        )
        for row in active:
            latest = Document.model_validate(row)
            if latest.uri not in seen:
                tombstone(db, latest)
                report.tombstoned += 1
    return report
