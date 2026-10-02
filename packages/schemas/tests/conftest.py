"""Shared fixtures: a document, a tiny reference chunker, and factories for the other rows.

The chunker here is a test fixture, not the platform's chunker (that arrives in A10). It is
deliberately simple (paragraphs split on blank lines) so the identity tests stay readable.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cairn_schemas import ids
from cairn_schemas.models import (
    Chunk,
    Document,
    Embedding,
    EvalConfig,
    EvalRun,
    Feedback,
    IndexSnapshot,
    LlmParams,
    Request,
    RetrievedChunk,
    StageLatency,
)

NOW = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
TENANT = "acme"
EMBED_MODEL = "openai/text-embedding-3-small@2024-01:1536"
LLM = "anthropic/claude-sonnet-4-5@2025-09-29"

POLICY_V1 = b"""# Refund policy

Annual plans can be refunded within 30 days of purchase.

Monthly plans can be cancelled at any time.

Contact support to start a refund.
"""

POLICY_V2 = POLICY_V1.replace(b"30 days", b"14 days")


def make_document(data: bytes = POLICY_V1, revision: int = 1, **overrides) -> Document:
    kwargs = dict(
        tenant_id=TENANT,
        source_id="docs-site",
        uri="https://docs.example.com/policies/refunds",
        data=data,
        content_type="text/markdown",
        fetched_at=NOW,
        revision=revision,
        created_at=NOW,
        acl=["group:everyone"],
    )
    kwargs.update(overrides)
    return Document.from_bytes(**kwargs)


def chunk_document(
    document: Document,
    text: str,
    parser_version: str = "md-1.0",
    chunker_version: str = "para-v1",
) -> list[Chunk]:
    """Split on blank lines; occurrence counts repeats of identical text within the document."""
    chunks: list[Chunk] = []
    seen: dict[str, int] = {}
    cursor = 0
    for ordinal, raw in enumerate(p for p in text.split("\n\n") if p.strip()):
        para = raw.strip()
        start = text.index(para, cursor)
        end = start + len(para)
        cursor = end
        sha = ids.sha256_hex(para)
        occurrence = seen.get(sha, 0)
        seen[sha] = occurrence + 1
        chunks.append(
            Chunk.build(
                document=document,
                parser_version=parser_version,
                chunker_version=chunker_version,
                ordinal=ordinal,
                occurrence=occurrence,
                byte_start=start,
                byte_end=end,
                text=para,
                token_count=max(1, len(para.split())),
                metadata={"section": "refunds"},
                created_at=NOW,
            )
        )
    return chunks


def make_embedding(chunk: Chunk, model_id: str = EMBED_MODEL) -> Embedding:
    dims = int(model_id.rsplit(":", 1)[1])
    return Embedding(
        tenant_id=TENANT,
        created_at=NOW,
        chunk_id=chunk.chunk_id,
        text_sha256=chunk.text_sha256,
        embedding_model_id=model_id,
        vector=[0.001 * i for i in range(dims)],
        dims=dims,
        job_id="job_" + "0" * 32,
        tokens_in=chunk.token_count,
        cost_usd=chunk.token_count * 0.02 / 1_000_000,
    )


def make_snapshot(
    model_id: str = EMBED_MODEL, lakehouse_snapshot_id: str = "8512345678901234"
) -> IndexSnapshot:
    index_name = ids.index_name_for("chunks", model_id)
    return IndexSnapshot(
        tenant_id=TENANT,
        created_at=NOW,
        index_snapshot_id=ids.index_snapshot_id(index_name, model_id, lakehouse_snapshot_id),
        index_name=index_name,
        embedding_model_id=model_id,
        lakehouse_snapshot_id=lakehouse_snapshot_id,
        built_at=NOW,
        row_count=60_000_000,
        lag_seconds_at_build=240,
        status="live",
    )


def make_request(chunks: list[Chunk], snapshot: IndexSnapshot, **overrides) -> Request:
    query = "What is the refund window for annual plans?"
    answer = "Annual plans can be refunded within 30 days of purchase."
    kwargs = dict(
        tenant_id=TENANT,
        created_at=NOW,
        request_id=ids.new_request_id(),
        user_id_hash="u_" + "a" * 16,
        received_at=NOW,
        query_text=query,
        query_sha256=ids.sha256_hex(query),
        prompt_template_version="support-v3",
        retrieval_config_version="hybrid-rrf-v1",
        index_snapshot_id=snapshot.index_snapshot_id,
        embedding_model_id=snapshot.embedding_model_id,
        filters={"tenant_id": TENANT, "acl": "group:everyone"},
        retrieved_chunks=[
            RetrievedChunk(
                chunk_id=c.chunk_id, text_sha256=c.text_sha256, score=1.0 / (i + 1), rank=i + 1
            )
            for i, c in enumerate(chunks)
        ],
        reranker_id="cohere/rerank-v3.5@2024-12",
        llm_id=LLM,
        llm_params=LlmParams(temperature=0.0, max_tokens=512, seed=7),
        response_text=answer,
        response_sha256=ids.sha256_hex(answer),
        latency=StageLatency(retrieve_ms=38, rerank_ms=61, generate_ms=840, total_ms=951),
        status="ok",
        trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
        tokens_in=1_950,
        tokens_out=42,
        cost_usd=0.0067,
    )
    kwargs.update(overrides)
    return Request(**kwargs)


def make_feedback(request: Request, **overrides) -> Feedback:
    kwargs = dict(
        tenant_id=TENANT,
        created_at=NOW,
        feedback_id=ids.new_feedback_id(),
        request_id=request.request_id,
        kind="thumbs",
        value=-1.0,
        comment=None,
        actor="user",
        received_at=NOW,
        dedup_key=f"{request.request_id}:thumbs:user",
        event_schema_version="events-v1",
    )
    kwargs.update(overrides)
    return Feedback(**kwargs)


def make_eval_run(request: Request, **overrides) -> EvalRun:
    kwargs = dict(
        tenant_id=TENANT,
        created_at=NOW,
        run_id=ids.new_run_id(),
        eval_set_id="cairn-core",
        eval_set_version="2026-10-02.1",
        config=EvalConfig(
            prompt_template_version=request.prompt_template_version,
            retrieval_config_version=request.retrieval_config_version,
            index_snapshot_id=request.index_snapshot_id,
            embedding_model_id=request.embedding_model_id,
            reranker_id=request.reranker_id,
            llm_id=request.llm_id,
            llm_params=request.llm_params,
        ),
        metrics={"recall_at_5": 0.81, "mrr": 0.62, "faithfulness": 0.93, "p95_ms": 1120.0},
        baseline_run_id=None,
        git_sha="a1b2c3d4e5f",
        started_at=NOW,
        finished_at=NOW,
        status="succeeded",
        tokens_in=410_000,
        tokens_out=22_000,
        cost_usd=4.10,
    )
    kwargs.update(overrides)
    return EvalRun(**kwargs)


@pytest.fixture
def document() -> Document:
    return make_document()


@pytest.fixture
def chunks(document: Document) -> list[Chunk]:
    return chunk_document(document, POLICY_V1.decode())


@pytest.fixture
def snapshot() -> IndexSnapshot:
    return make_snapshot()


@pytest.fixture
def request_row(chunks: list[Chunk], snapshot: IndexSnapshot) -> Request:
    return make_request(chunks, snapshot)
