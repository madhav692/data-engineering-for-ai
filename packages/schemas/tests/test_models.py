"""Every model carries the cross-cutting fields, declares its contract metadata, and
rejects rows whose derived fields disagree with their inputs."""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from cairn_schemas.base import Costed, Row
from cairn_schemas.models import (
    ALL_MODELS,
    Chunk,
    Document,
    Embedding,
    EvalConfig,
    IndexSnapshot,
    Request,
)

from tests.conftest import (
    EMBED_MODEL,
    NOW,
    POLICY_V1,
    chunk_document,
    make_document,
    make_embedding,
    make_eval_run,
    make_feedback,
    make_request,
    make_snapshot,
)

_PARTITION_TRANSFORM = re.compile(r"^(?:\w+\((?:\d+,\s*)?(\w+)\)|(\w+))$")


@pytest.mark.parametrize("model", ALL_MODELS, ids=lambda m: m.__name__)
def test_every_model_carries_the_cross_cutting_fields(model: type[Row]):
    for name in ("tenant_id", "created_at", "schema_version"):
        assert name in model.model_fields, f"{model.__name__} lacks {name}"
    assert model.LINEAGE_FIELDS, f"{model.__name__} must name the version that made it"


@pytest.mark.parametrize("model", ALL_MODELS, ids=lambda m: m.__name__)
def test_contract_metadata_refers_to_real_fields(model: type[Row]):
    fields = set(model.model_fields)
    assert model.TABLE and re.fullmatch(r"[a-z_]+", model.TABLE)
    assert model.PRIMARY_KEY and set(model.PRIMARY_KEY) <= fields
    assert set(model.LINEAGE_FIELDS) <= fields
    for spec in model.PARTITION_BY:
        m = _PARTITION_TRANSFORM.match(spec)
        assert m, f"unparseable partition spec {spec!r}"
        assert (m.group(1) or m.group(2)) in fields, spec


def test_rows_that_touch_a_model_are_costed():
    assert issubclass(Embedding, Costed)
    assert issubclass(Request, Costed)
    assert not issubclass(Document, Costed)
    assert not issubclass(Chunk, Costed)


def test_eval_config_uses_the_same_keys_as_request():
    """C8 depends on this: an eval run and a production request must be comparable."""
    for name, info in EvalConfig.model_fields.items():
        assert name in Request.model_fields, f"EvalConfig.{name} is not a Request field"
        assert info.annotation == Request.model_fields[name].annotation, name


def test_document_rejects_a_doc_id_that_does_not_match_its_location():
    doc = make_document()
    with pytest.raises(ValidationError, match="doc_id"):
        Document(**{**doc.model_dump(), "uri": "https://docs.example.com/other"})
    with pytest.raises(ValidationError, match="raw_object_key"):
        Document(**{**doc.model_dump(), "raw_object_key": "raw/docs-site/zz/" + "0" * 64})


def test_chunk_rejects_text_that_does_not_match_its_hash():
    doc = make_document()
    chunk = chunk_document(doc, POLICY_V1.decode())[1]
    with pytest.raises(ValidationError, match="text_sha256"):
        Chunk(**{**chunk.model_dump(), "text": chunk.text + " edited"})
    with pytest.raises(ValidationError, match="byte_end"):
        Chunk(**{**chunk.model_dump(), "byte_end": chunk.byte_start})


def test_embedding_rejects_a_vector_whose_length_disagrees_with_the_model():
    doc = make_document()
    chunk = chunk_document(doc, POLICY_V1.decode())[0]
    emb = make_embedding(chunk)
    with pytest.raises(ValidationError, match="dims"):
        Embedding(**{**emb.model_dump(), "vector": emb.vector[:-1]})
    with pytest.raises(ValidationError, match="dims"):
        Embedding(**{**emb.model_dump(), "dims": 1024})


def test_index_snapshot_name_must_carry_the_model_slug():
    snap = make_snapshot()
    bad_name = "chunks--some-other-model"
    with pytest.raises(ValidationError, match="index_name"):
        IndexSnapshot(**{**snap.model_dump(), "index_name": bad_name})


def test_request_requires_every_lineage_field():
    doc = make_document()
    chunks = chunk_document(doc, POLICY_V1.decode())
    req = make_request(chunks, make_snapshot())
    for name in Request.LINEAGE_FIELDS:
        payload = req.model_dump()
        payload.pop(name)
        if Request.model_fields[name].is_required():
            with pytest.raises(ValidationError):
                Request(**payload)
    with pytest.raises(ValidationError, match="rank order"):
        make_request(
            list(reversed(chunks)),
            make_snapshot(),
            retrieved_chunks=[c.model_copy(update={"rank": 5}) for c in req.retrieved_chunks],
        )


def test_feedback_value_rules():
    doc = make_document()
    req = make_request(chunk_document(doc, POLICY_V1.decode()), make_snapshot())
    assert make_feedback(req, kind="rating", value=4.0).value == 4.0
    with pytest.raises(ValidationError, match="thumbs"):
        make_feedback(req, kind="thumbs", value=0.5)
    with pytest.raises(ValidationError, match="requires a comment"):
        make_feedback(req, kind="dispute", value=1.0, comment=None)


def test_eval_run_times_are_ordered():
    doc = make_document()
    req = make_request(chunk_document(doc, POLICY_V1.decode()), make_snapshot())
    run = make_eval_run(req)
    assert run.config.index_snapshot_id == req.index_snapshot_id
    with pytest.raises(ValidationError, match="finished_at"):
        make_eval_run(req, finished_at=NOW.replace(year=2025))


def test_rows_are_immutable_and_reject_unknown_fields():
    doc = make_document()
    with pytest.raises(ValidationError):
        doc.tenant_id = "other"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        Document(**{**doc.model_dump(), "surprise": 1})


def test_lineage_and_key_helpers():
    doc = make_document()
    chunk = chunk_document(doc, POLICY_V1.decode())[0]
    assert doc.key() == (doc.doc_id, 1)
    assert chunk.lineage() == {
        "content_sha256": doc.content_sha256,
        "parser_version": "md-1.0",
        "chunker_version": "para-v1",
    }
    assert make_embedding(chunk).lineage() == {"embedding_model_id": EMBED_MODEL}
