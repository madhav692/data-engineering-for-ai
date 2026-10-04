"""Contract C3: Derivation -> Lakehouse. Every derived row names the version that made it, and
only what changed is re-embedded."""

from __future__ import annotations

from cairn_schemas import ids

from cairn.derivation import chunker
from cairn.ingestion.filesystem import document_uri
from support import POLICY_EDIT, SOURCE_ID, Platform


def test_edit_reembeds_exactly_one_chunk(platform: Platform):
    """One new embeddings row after a one-paragraph edit; every other chunk keeps its id."""
    platform.ingest()
    doc_id = ids.doc_id(SOURCE_ID, document_uri(SOURCE_ID, "policy.md"))
    embeddings_before = platform.count("embeddings")
    chunk_ids_before = {
        r["chunk_id"]
        for r in platform.db.rows("SELECT chunk_id FROM chunks WHERE doc_id = %s", (doc_id,))
    }

    platform.edit("policy.md", *POLICY_EDIT)
    _, derivation = platform.ingest()

    assert derivation.embedded == 1
    assert platform.count("embeddings") == embeddings_before + 1
    live = platform.db.latest_document(doc_id)
    assert live is not None
    chunk_ids_live = {
        r["chunk_id"]
        for r in platform.db.rows(
            "SELECT chunk_id FROM chunks WHERE doc_id = %s AND content_sha256 = %s",
            (doc_id, live.content_sha256),
        )
    }
    # the live version has the same number of chunks; exactly one id is new
    assert len(chunk_ids_live) == len(chunk_ids_before)
    assert len(chunk_ids_live - chunk_ids_before) == 1
    # the stale chunk (old text) is still there under the old content hash: a row, not an absence
    assert platform.count("chunks", "doc_id = %s", (doc_id,)) == len(chunk_ids_before) + 1


def test_chunker_version_change_is_a_backfill(platform: Platform, monkeypatch):
    """New ids for every chunk; the old rows remain under the old version."""
    platform.ingest()
    old_version = chunker.CHUNKER_VERSION
    old_ids = {r["chunk_id"] for r in platform.db.rows("SELECT chunk_id FROM chunks")}
    old_embeddings = platform.count("embeddings")

    monkeypatch.setattr(chunker, "CHUNKER_VERSION", "md-struct-v2")
    report, derivation = platform.ingest()  # no file changed...
    assert report.registered == 0
    # ...yet every active document was re-derived from its raw bytes under the new version
    assert derivation.rederived == 3
    assert derivation.chunks == len(old_ids)
    assert derivation.embedded == len(old_ids)

    new_ids = {
        r["chunk_id"]
        for r in platform.db.rows(
            "SELECT chunk_id FROM chunks WHERE chunker_version = %s", ("md-struct-v2",)
        )
    }
    assert new_ids.isdisjoint(old_ids)
    assert len(new_ids) == len(old_ids)
    assert platform.count("chunks", "chunker_version = %s", (old_version,)) == len(old_ids)
    assert platform.count("embeddings") == old_embeddings * 2

    # and the index built now holds only the live version's chunks
    result = platform.build()
    assert result.snapshot.row_count == len(new_ids)
