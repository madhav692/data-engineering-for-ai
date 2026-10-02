"""Contract tests: the guarantees in docs/architecture/reference.md that are real at stage-0.

C1  Ingestion -> Lakehouse   refetching unchanged content is a no-op; a changed byte is a version
C3  Derivation -> Lakehouse  chunk ids are deterministic; an edit changes only the edited chunk
C6  Serving -> Lakehouse     a request row names every lineage id and round-trips through Avro
"""

from __future__ import annotations

import io

import fastavro

from cairn_schemas.generate import avro
from cairn_schemas.models import Chunk, Document, Request
from tests.conftest import (
    POLICY_V1,
    POLICY_V2,
    chunk_document,
    make_document,
    make_request,
    make_snapshot,
)

# --- C1 ------------------------------------------------------------------------------


def test_c1_refetching_the_same_bytes_is_a_no_op():
    first = make_document(POLICY_V1)
    again = make_document(POLICY_V1, revision=2)
    assert again.doc_id == first.doc_id
    assert again.content_sha256 == first.content_sha256
    assert Document.is_change(first, again) is False


def test_c1_one_changed_byte_is_a_new_version_of_the_same_document():
    v1 = make_document(POLICY_V1)
    v2 = make_document(POLICY_V2, revision=2)
    assert v2.doc_id == v1.doc_id, "identity survives an edit"
    assert v2.content_sha256 != v1.content_sha256, "version does not"
    assert v2.raw_object_key != v1.raw_object_key, "both versions keep their raw bytes"
    assert Document.is_change(v1, v2) is True


def test_c1_a_delete_is_a_revision_not_an_absence():
    v2 = make_document(POLICY_V2, revision=2)
    tombstone = make_document(POLICY_V2, revision=3, status="deleted")
    assert tombstone.doc_id == v2.doc_id
    assert tombstone.content_sha256 == v2.content_sha256
    assert Document.is_change(v2, tombstone) is True
    assert (
        Document.is_change(tombstone, make_document(POLICY_V2, revision=4, status="deleted"))
        is False
    )


# --- C3 ------------------------------------------------------------------------------


def test_c3_chunking_the_same_version_twice_gives_identical_ids():
    doc = make_document(POLICY_V1)
    a = chunk_document(doc, POLICY_V1.decode())
    b = chunk_document(doc, POLICY_V1.decode())
    assert [c.chunk_id for c in a] == [c.chunk_id for c in b]
    assert all(x == y for x, y in zip(a, b, strict=True))


def test_c3_an_edit_changes_only_the_edited_chunk():
    v1 = make_document(POLICY_V1)
    v2 = make_document(POLICY_V2, revision=2)
    before = {c.chunk_id for c in chunk_document(v1, POLICY_V1.decode())}
    after = {c.chunk_id for c in chunk_document(v2, POLICY_V2.decode())}
    assert len(before) == len(after) == 4
    assert len(before & after) == 3, "three paragraphs unchanged keep their ids"
    assert len(after - before) == 1, "exactly one chunk needs a new embedding"


def test_c3_inserting_a_paragraph_does_not_renumber_the_others():
    v1 = make_document(POLICY_V1)
    inserted = POLICY_V1.replace(
        b"# Refund policy\n\n", b"# Refund policy\n\nUpdated 2 October 2026.\n\n"
    )
    v2 = make_document(inserted, revision=2)
    before = {c.chunk_id for c in chunk_document(v1, POLICY_V1.decode())}
    after = chunk_document(v2, inserted.decode())
    assert before <= {c.chunk_id for c in after}, "position shifts do not change identity"
    assert [c.ordinal for c in after] == [0, 1, 2, 3, 4], "ordinals do shift; ids do not"


def test_c3_repeated_text_within_a_document_stays_distinct():
    doubled = POLICY_V1 + b"\nContact support to start a refund.\n"
    doc = make_document(doubled)
    chunks = chunk_document(doc, doubled.decode())
    repeats = [c for c in chunks if c.text == "Contact support to start a refund."]
    assert [c.occurrence for c in repeats] == [0, 1]
    assert len({c.chunk_id for c in repeats}) == 2
    assert len({c.text_sha256 for c in repeats}) == 1, (
        "same text, same embedding (dedup joins here)"
    )


def test_c3_a_chunker_upgrade_is_a_backfill_not_a_mutation():
    doc = make_document(POLICY_V1)
    v1 = chunk_document(doc, POLICY_V1.decode(), chunker_version="para-v1")
    v2 = chunk_document(doc, POLICY_V1.decode(), chunker_version="para-v2")
    assert {c.chunk_id for c in v1}.isdisjoint({c.chunk_id for c in v2})
    assert all(c.lineage()["chunker_version"] == "para-v2" for c in v2)


# --- C6 ------------------------------------------------------------------------------


def test_c6_a_request_names_every_lineage_id_it_read():
    doc = make_document(POLICY_V1)
    chunks = chunk_document(doc, POLICY_V1.decode())
    snapshot = make_snapshot()
    req = make_request(chunks, snapshot)
    lineage = req.lineage()
    assert set(lineage) == set(Request.LINEAGE_FIELDS)
    assert lineage["index_snapshot_id"] == snapshot.index_snapshot_id
    assert lineage["embedding_model_id"] == snapshot.embedding_model_id
    assert [c.chunk_id for c in req.retrieved_chunks] == [c.chunk_id for c in chunks]
    assert all(
        rc.text_sha256 == c.text_sha256 for rc, c in zip(req.retrieved_chunks, chunks, strict=True)
    )


def _avro_round_trip(model, row):
    schema = fastavro.parse_schema(avro.avro_schema(model))
    buf = io.BytesIO()
    fastavro.writer(buf, schema, [row.model_dump(mode="python")])
    buf.seek(0)
    (record,) = list(fastavro.reader(buf))
    return model(**record)


def test_c6_request_rows_survive_the_wire():
    doc = make_document(POLICY_V1)
    req = make_request(chunk_document(doc, POLICY_V1.decode()), make_snapshot())
    assert _avro_round_trip(Request, req) == req


def test_c3_chunk_rows_survive_the_wire():
    doc = make_document(POLICY_V1)
    chunk = chunk_document(doc, POLICY_V1.decode())[2]
    assert _avro_round_trip(Chunk, chunk) == chunk
