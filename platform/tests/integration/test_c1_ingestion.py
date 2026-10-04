"""Contract C1: Ingestion and registry -> Lakehouse. Every input has a stable identity and a
content hash at the door; a refetch of the same bytes writes nothing; a delete is a row."""

from __future__ import annotations

from cairn_schemas import ids

from cairn.ingestion.filesystem import document_uri
from support import POLICY_EDIT, SOURCE_ID, Platform


def test_ingest_twice_is_a_no_op(platform: Platform):
    """The second `cairn ingest` writes no rows and no objects."""
    first, derivation = platform.ingest()
    assert first.registered == 3 and first.unchanged == 0
    assert derivation.chunks > 0 and derivation.embedded == derivation.chunks
    before = {
        "documents": platform.count("documents"),
        "chunks": platform.count("chunks"),
        "embeddings": platform.count("embeddings"),
        "objects": platform.store.count(),
    }
    assert before["objects"] == 3

    second, derivation = platform.ingest()
    assert second.registered == 0 and second.unchanged == 3 and second.tombstoned == 0
    assert derivation.chunks == 0 and derivation.embedded == 0
    after = {
        "documents": platform.count("documents"),
        "chunks": platform.count("chunks"),
        "embeddings": platform.count("embeddings"),
        "objects": platform.store.count(),
    }
    assert after == before


def test_edit_is_a_new_revision_of_the_same_document(platform: Platform):
    """Same doc_id, new content_sha256, revision 2."""
    platform.ingest()
    doc_id = ids.doc_id(SOURCE_ID, document_uri(SOURCE_ID, "policy.md"))
    v1 = platform.db.latest_document(doc_id)
    assert v1 is not None and v1.revision == 1

    platform.edit("policy.md", *POLICY_EDIT)
    report, _ = platform.ingest()
    assert report.registered == 1 and report.unchanged == 2

    v2 = platform.db.latest_document(doc_id)
    assert v2 is not None
    assert v2.doc_id == v1.doc_id
    assert v2.revision == 2
    assert v2.content_sha256 != v1.content_sha256
    assert v2.status == "active"
    assert platform.count("documents", "doc_id = %s", (doc_id,)) == 2
    # both versions' raw bytes are in the object store, under their content hashes
    assert platform.store.exists(v1.raw_object_key) and platform.store.exists(v2.raw_object_key)


def test_removed_file_becomes_a_tombstone(platform: Platform):
    """--prune writes a deleted revision; the next build drops its chunks."""
    platform.ingest()
    first_build = platform.build()
    doc_id = ids.doc_id(SOURCE_ID, document_uri(SOURCE_ID, "snapshots.md"))
    its_chunks = platform.count("chunks", "doc_id = %s", (doc_id,))
    assert its_chunks > 0

    (platform.corpus / "snapshots.md").unlink()
    report, _ = platform.ingest(prune=True)
    assert report.tombstoned == 1 and report.registered == 0

    latest = platform.db.latest_document(doc_id)
    assert latest is not None and latest.status == "deleted" and latest.revision == 2
    # a delete is a row, not an absence: the history is intact
    assert platform.count("documents", "doc_id = %s", (doc_id,)) == 2

    second_build = platform.build()
    assert second_build.snapshot.row_count == first_build.snapshot.row_count - its_chunks
    hits = platform.ask("What is a snapshot and how does time travel work?")
    cited_docs = {
        platform.db.scalar("SELECT doc_id FROM chunks WHERE chunk_id = %s", (c["chunk_id"],))
        for c in hits["citations"]
    }
    assert doc_id not in cited_docs
