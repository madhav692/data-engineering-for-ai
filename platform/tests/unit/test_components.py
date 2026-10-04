"""Small pure pieces: the hashing embedder, model refs, prices, names, the extractive generator."""

from __future__ import annotations

import math

import pytest

from cairn_schemas import ids
from cairn_schemas.models import LlmParams
from cairn_schemas.versions import parse_embedding_model_id

from cairn.config import Settings
from cairn.derivation.embedder import HASHING_MODEL_ID, HashingEmbedder
from cairn.indexes.build import table_name_for
from cairn.serving.generator import ExtractiveGenerator, model_ref_for, price_for
from cairn.serving.prompts import Context, ContextChunk, PromptTemplate
from cairn.serving.retrieval import RetrievalConfig


def test_hashing_embedder_is_deterministic_and_unit_length():
    e = HashingEmbedder()
    a = e.embed_query("hidden partitioning in iceberg")
    b = e.embed_documents(["hidden partitioning in iceberg"])[0]
    assert a == b
    assert len(a) == 384 == parse_embedding_model_id(HASHING_MODEL_ID).dims
    assert math.isclose(sum(x * x for x in a), 1.0, rel_tol=1e-9)
    assert e.embed_query("")[0] == 1.0  # the empty text has a vector too


def test_hashing_embedder_prefers_overlapping_text():
    e = HashingEmbedder()
    q = e.embed_query("how does hidden partitioning work")
    near = e.embed_query("hidden partitioning avoids extra filters")
    far = e.embed_query("refunds are accepted within thirty days")
    dot = lambda u, v: sum(x * y for x, y in zip(u, v, strict=True))  # noqa: E731
    assert dot(q, near) > dot(q, far)


def test_model_ref_spelling_satisfies_the_contract():
    assert model_ref_for("ollama", "llama3.2:3b", "latest") == "ollama/llama3.2-3b@latest"
    assert model_ref_for("OpenAI", "gpt-4o-mini", "gpt-4o-mini-2024-07-18") == (
        "openai/gpt-4o-mini@gpt-4o-mini-2024-07-18"
    )
    assert model_ref_for("x", "", "") == "x/model@latest"


def test_price_table_and_override():
    assert price_for("gpt-4o-mini-2024-07-18", Settings()) == (0.15, 0.60)
    assert price_for("gpt-4o", Settings()) == (2.50, 10.00)
    assert price_for("llama3.2", Settings()) is None
    custom = Settings().with_(llm_price_in_per_1m=1.0, llm_price_out_per_1m=2.0)
    assert price_for("llama3.2", custom) == (1.0, 2.0)


def test_index_table_name_is_a_valid_identifier():
    name = ids.index_name_for("chunks", "baai/bge-small-en@v1.5:384")
    assert name == "chunks--baai-bge-small-en-v1-5-384"
    snapshot_id = ids.index_snapshot_id(
        name, "baai/bge-small-en@v1.5:384", "2026-01-01T00:00:00+00:00"
    )
    table = table_name_for(snapshot_id, name)
    assert table.startswith("idx_chunks_baai_bge_small_en_v1_5_384_")
    assert len(table) <= 63
    assert table.replace("_", "").isalnum()


def test_retrieval_config_parses_its_version():
    assert RetrievalConfig.parse("vector-k5-v1").k == 5
    assert RetrievalConfig.parse("vector-k20-v3").k == 20
    with pytest.raises(ValueError):
        RetrievalConfig.parse("hybrid-v1")


def test_extractive_generator_is_a_pure_function_of_the_context():
    template = PromptTemplate.load("answer-v1")
    chunk = ContextChunk(1, "chk_" + "0" * 32, "docs/a.md", "A > B", "The text.", 3)
    context = Context(
        chunks=(chunk,), prompt=template.render("[1] ...", "q"), template_version="answer-v1"
    )
    g1 = ExtractiveGenerator().generate("q", context)
    g2 = ExtractiveGenerator().generate("q", context)
    assert g1 == g2
    assert g1.text == "From docs/a.md › A > B:\n\nThe text."
    assert g1.model_ref == "cairn/extractive@0"
    assert g1.params == LlmParams(temperature=0.0, max_tokens=400)  # configuration, not data
    assert (g1.tokens_in, g1.tokens_out, g1.cost_usd) == (0, 0, 0.0)
