"""The chunker is a deterministic pure function with a heading path and a token budget."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cairn_schemas import ids
from cairn_schemas.models import Document

from cairn.derivation import chunker
from cairn.derivation.chunker import MAX_TOKENS, chunk_document, parse, sections
from cairn.derivation.embedder import HashingEmbedder

count = HashingEmbedder().count_tokens

DOC = """---
title: Front matter
tags: [a, b]
---
<!-- a license header -->
# Title

Intro paragraph under the title.

## Section A

First paragraph of A. It has two sentences.

Second paragraph of A.

### Sub A.1

```python
# not a heading: inside a fence
print("hello")
```

## Section B

Only paragraph of B.

Only paragraph of B.
"""


def make_doc(text: str) -> tuple[Document, str]:
    data = text.encode("utf-8")
    doc = Document.from_bytes(
        tenant_id="t",
        source_id="docs",
        uri="fs://docs/x.md",
        data=data,
        content_type="text/markdown",
        fetched_at=datetime.now(tz=UTC),
    )
    return doc, parse(data)


def test_parse_strips_front_matter_and_html_comments():
    text = parse(DOC.encode())
    assert text.startswith("\n# Title")
    assert "license header" not in text
    assert "title: Front matter" not in text


def test_heading_paths_follow_the_structure():
    doc, text = make_doc(DOC)
    chunks = chunk_document(doc, text, count)
    paths = [c.metadata["heading_path"] for c in chunks]
    assert paths[0] == "Title"
    assert "Title > Section A" in paths
    assert "Title > Section A > Sub A.1" in paths
    assert "Title > Section B" in paths
    # a fenced "# comment" is not a heading
    assert not any("not a heading" in p for p in paths)


def test_chunks_are_deterministic_and_content_addressed():
    doc, text = make_doc(DOC)
    a = chunk_document(doc, text, count)
    b = chunk_document(doc, text, count)
    assert [c.chunk_id for c in a] == [c.chunk_id for c in b]
    assert [c.text for c in a] == [c.text for c in b]
    for c in a:
        assert c.chunk_id == ids.chunk_id(
            doc.doc_id, chunker.PARSER_VERSION, chunker.CHUNKER_VERSION, c.text_sha256, c.occurrence
        )
        assert text.encode("utf-8")[c.byte_start : c.byte_end].decode("utf-8") == c.text


def test_repeated_text_in_one_document_gets_distinct_ids():
    """A block repeated three times: the first chunk also holds the heading line, the other two
    are byte-identical, and the occurrence counter keeps their ids distinct."""
    block = " ".join(f"word{i}" for i in range(250))  # 250 tokens: one block per chunk
    text = f"# T\n\n{block}\n\n{block}\n\n{block}\n"
    doc, parsed = make_doc(text)
    chunks = chunk_document(doc, parsed, count)
    assert len(chunks) == 3
    repeated = [c for c in chunks if c.text == block]
    assert len(repeated) == 2
    assert {c.occurrence for c in repeated} == {0, 1}
    assert len({c.chunk_id for c in repeated}) == 2


def test_no_chunk_exceeds_the_budget_and_offsets_are_exact():
    paragraphs = [f"Paragraph {i}: " + " ".join(f"word{j}" for j in range(60)) for i in range(40)]
    text = "# Big\n\n" + "\n\n".join(paragraphs) + "\n"
    doc, parsed = make_doc(text)
    chunks = chunk_document(doc, parsed, count)
    assert len(chunks) > 1
    for c in chunks:
        assert c.token_count <= MAX_TOKENS
        assert parsed[c.byte_start : c.byte_end] == c.text  # ascii: bytes == chars
    ordinals = [c.ordinal for c in chunks]
    assert ordinals == list(range(len(chunks)))


def test_oversize_block_is_split_on_sentences_then_words():
    one_line = " ".join(f"Sentence number {i} is here." for i in range(300))
    text = f"# H\n\n{one_line}\n"
    pieces = list(sections(text, count, 50))
    assert len(pieces) > 1
    assert all(count(p.body) <= 50 for p in pieces)
    joined = " ".join(p.body for p in pieces)
    assert joined.replace(" ", "").removeprefix("#H\n\n") == one_line.replace(" ", "")


def test_edit_changes_only_the_edited_chunk():
    doc_a, text_a = make_doc(DOC)
    doc_b, text_b = make_doc(
        DOC.replace("Second paragraph of A.", "Second paragraph of A, edited.")
    )
    ids_a = {c.text: c.chunk_id for c in chunk_document(doc_a, text_a, count)}
    ids_b = {c.text: c.chunk_id for c in chunk_document(doc_b, text_b, count)}
    # the document's bytes changed, so doc_b has a new content_sha256, but doc_id is the same...
    assert doc_a.doc_id == doc_b.doc_id
    assert doc_a.content_sha256 != doc_b.content_sha256
    # ...and every chunk whose text did not change keeps its id
    unchanged = set(ids_a) & set(ids_b)
    assert unchanged
    assert all(ids_a[t] == ids_b[t] for t in unchanged)
    assert len(set(ids_b.values()) - set(ids_a.values())) == 1


@pytest.mark.parametrize("version_name", ["PARSER_VERSION", "CHUNKER_VERSION"])
def test_a_version_change_changes_every_chunk_id(monkeypatch, version_name):
    doc, text = make_doc(DOC)
    before = {c.chunk_id for c in chunk_document(doc, text, count)}
    monkeypatch.setattr(chunker, version_name, "test-v2")
    after = {c.chunk_id for c in chunk_document(doc, text, count)}
    assert before.isdisjoint(after)
    assert len(before) == len(after)
