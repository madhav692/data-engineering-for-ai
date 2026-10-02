"""Derived ids are deterministic and sensitive to every input; minted ids are time-ordered."""

from __future__ import annotations

import re

from cairn_schemas import ids


def test_doc_id_is_stable_under_uri_spelling():
    base = ids.doc_id("docs-site", "https://docs.example.com/policies/refunds")
    assert base == ids.doc_id("docs-site", "HTTPS://DOCS.EXAMPLE.COM/policies/refunds")
    assert base == ids.doc_id("docs-site", "https://docs.example.com:443/policies/refunds/")
    assert base == ids.doc_id("docs-site", "https://docs.example.com/policies/refunds#section-2")
    assert base != ids.doc_id("docs-site", "https://docs.example.com/policies/refunds?v=2")
    assert base != ids.doc_id("other-source", "https://docs.example.com/policies/refunds")
    assert re.fullmatch(r"doc_[0-9a-f]{32}", base)


def test_canonical_uri_handles_non_http_schemes_and_paths():
    assert ids.canonical_uri("s3://bucket/key/") == "s3://bucket/key"
    assert ids.canonical_uri("file:///data/a.pdf") == "file:///data/a.pdf"
    assert ids.canonical_uri("docs/a.md") == "docs/a.md"
    assert ids.canonical_uri("https://example.com") == "https://example.com/"


def test_chunk_id_depends_on_every_identity_input_and_nothing_else():
    args = dict(
        doc_id="doc_" + "0" * 32,
        parser_version="md-1.0",
        chunker_version="para-v1",
        text_sha256="a" * 64,
        occurrence=0,
    )
    base = ids.chunk_id(**args)
    assert base == ids.chunk_id(**args)
    for key, value in [
        ("doc_id", "doc_" + "1" * 32),
        ("parser_version", "md-1.1"),
        ("chunker_version", "para-v2"),
        ("text_sha256", "b" * 64),
        ("occurrence", 1),
    ]:
        assert ids.chunk_id(**{**args, key: value}) != base, key


def test_index_snapshot_id_and_name_convention():
    model = "openai/text-embedding-3-small@2024-01:1536"
    name = ids.index_name_for("chunks", model)
    assert name == "chunks--openai-text-embedding-3-small-2024-01-1536"
    a = ids.index_snapshot_id(name, model, "100")
    assert a == ids.index_snapshot_id(name, model, "100")
    assert a != ids.index_snapshot_id(name, model, "101")


def test_raw_object_key_layout():
    sha = "ab" + "0" * 62
    assert ids.raw_object_key("arxiv", sha) == f"raw/arxiv/ab/{sha}"


def test_minted_ids_are_unique_and_time_ordered():
    first = [ids.new_request_id() for _ in range(200)]
    assert len(set(first)) == 200
    assert all(re.fullmatch(r"req_[0-9a-f]{32}", r) for r in first)
    # UUIDv7 puts the Unix millisecond in the leading 48 bits (12 hex chars after the prefix),
    # so ids sort by creation time at millisecond granularity; within a millisecond the
    # remaining bits are random.
    stamps = [r[4:16] for r in first]
    assert stamps == sorted(stamps)
    assert ids.uuid7().version == 7
