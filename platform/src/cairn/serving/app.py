"""The api: ``/ask``, ``/feedback``, ``/metrics``, ``/healthz``.

A request runs through six steps: embed the question with the corpus model; search the live
snapshot's table (tenant filter inside the query); assemble the context with the prompt
template; generate; write the ``Request`` row with every version the request used; return the
answer. The fifth step happens before the sixth (contract C6): the row is the only record of
what the model saw, and the ``Request`` model will not construct without every lineage field,
so the handler cannot forget one. The contract does the remembering, not the programmer.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException
from fastapi import Request as HttpRequest
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, ValidationError

from cairn_schemas import ids
from cairn_schemas.models import Feedback, Request, RetrievedChunk, StageLatency

from cairn.config import Settings
from cairn.derivation.embedder import Embedder, embedder_from_settings
from cairn.indexes.reconcile import live_snapshot
from cairn.lakehouse.db import Db
from cairn.lineage.request_log import RequestLog
from cairn.observability.metrics import render_metrics
from cairn.serving.generator import (
    Generation,
    Generator,
    OpenAICompatibleGenerator,
    generator_from_env,
)
from cairn.serving.prompts import PromptTemplate, assemble
from cairn.serving.retrieval import Hit, RetrievalConfig, VectorRetrieval

log = logging.getLogger("cairn.serving")

FEEDBACK_EVENT_SCHEMA_VERSION = "feedback-v1"


@dataclass
class Deps:
    settings: Settings
    db: Db
    embedder: Embedder
    retrieval: VectorRetrieval
    generator: Generator
    prompt_template: PromptTemplate
    request_log: RequestLog

    @property
    def index_name(self) -> str:
        return ids.index_name_for(self.settings.index_base_name, self.embedder.model_id)

    @property
    def tenant_id(self) -> str:
        return self.settings.tenant_id


def build_deps(
    settings: Settings, *, db: Db | None = None, embedder: Embedder | None = None
) -> Deps:
    db = db or Db(settings.database_url)
    return Deps(
        settings=settings,
        db=db,
        embedder=embedder or embedder_from_settings(settings),
        retrieval=VectorRetrieval(
            RetrievalConfig(version=settings.retrieval_config_version, k=settings.retrieval_k)
        ),
        generator=generator_from_env(settings),
        prompt_template=PromptTemplate.load(settings.prompt_template_version),
        request_log=RequestLog(db),
    )


# ---- wire types -----------------------------------------------------------------------------


class GeneratorSpec(BaseModel):
    """A per-request generator (Stage 0 only; A17 moves routing server-side)."""

    base_url: str
    model: str
    provider: str = "openai-compatible"
    api_key: str | None = None
    version: str | None = None


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    generator: GeneratorSpec | None = None


class Citation(BaseModel):
    rank: int
    chunk_id: str
    title: str
    heading_path: str
    score: float


class AskReply(BaseModel):
    request_id: str
    answer: str
    citations: list[Citation]
    latency: StageLatency
    llm_id: str
    index_snapshot_id: str


class FeedbackBody(BaseModel):
    request_id: str
    kind: Literal["thumbs", "rating", "correction", "dispute", "click"]
    value: float
    comment: str | None = None
    dedup_key: str | None = Field(
        default=None,
        description="Idempotency key; default: hash of (request_id, kind, value, comment).",
    )
    actor: Literal["user", "reviewer", "judge"] = "user"


class FeedbackReply(BaseModel):
    feedback_id: str
    duplicate: bool


# ---- the app --------------------------------------------------------------------------------


def get_deps(http: HttpRequest) -> Deps:
    return http.app.state.deps


DepsDep = Annotated[Deps, Depends(get_deps)]


def create_app(settings: Settings | None = None, *, deps: Deps | None = None) -> FastAPI:
    settings = settings or (deps.settings if deps else Settings.from_env())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.deps = deps or build_deps(settings)
        d: Deps = app.state.deps
        log.info(
            "api ready",
            extra={
                "embedder": d.embedder.describe(),
                "generator": d.generator.describe(),
                "index_name": d.index_name,
                **settings.lineage_versions(),
            },
        )
        yield
        if deps is None:
            d.db.close()

    app = FastAPI(title="Cairn", version="stage-0", lifespan=lifespan)

    @app.get("/healthz")
    def healthz(d: DepsDep) -> dict[str, Any]:
        database = d.db.ping()
        if not database:
            raise HTTPException(503, "database unreachable")
        snapshot = live_snapshot(d.db, d.index_name)
        return {
            "status": "ok",
            "database": database,
            "embedder": d.embedder.describe(),
            "embedding_model_id": d.embedder.model_id,
            "generator": d.generator.describe(),
            "index_name": d.index_name,
            "live_snapshot": snapshot.index_snapshot_id if snapshot else None,
            "versions": {
                "prompt_template_version": d.prompt_template.version,
                "retrieval_config_version": d.retrieval.config_version,
                "embedding_model_id": d.embedder.model_id,
            },
        }

    @app.post("/ask", response_model=AskReply)
    def ask(body: AskBody, d: DepsDep) -> AskReply:
        received_at = datetime.now(tz=UTC)
        t0 = perf_counter()
        snapshot = live_snapshot(d.db, d.index_name)  # the pointer
        if snapshot is None:
            raise HTTPException(409, f"no live index {d.index_name}; run `cairn build-index`")
        generator = _generator_for(body, d)
        hits = d.retrieval.search(
            d.db, snapshot, d.embedder.embed_query(body.question), tenant_id=d.tenant_id
        )
        t1 = perf_counter()
        context = assemble(hits, d.prompt_template, body.question)  # answer-v1
        error: Exception | None = None
        gen: Generation | None = None
        try:
            gen = generator.generate(body.question, context)  # extractive, or an LLM
        except Exception as err:  # noqa: BLE001 - a failed generation still writes a row
            error = err
        t2 = perf_counter()
        response_text = gen.text if gen is not None else None
        row = Request(
            tenant_id=d.tenant_id,
            created_at=datetime.now(tz=UTC),
            request_id=ids.new_request_id(),
            received_at=received_at,
            query_text=body.question,
            query_sha256=ids.sha256_hex(body.question),
            prompt_template_version=d.prompt_template.version,  # answer-v1
            retrieval_config_version=d.retrieval.config_version,  # vector-k5-v1
            index_snapshot_id=snapshot.index_snapshot_id,
            embedding_model_id=snapshot.embedding_model_id,
            filters={"tenant_id": d.tenant_id},
            retrieved_chunks=[
                RetrievedChunk(
                    chunk_id=h.chunk_id, text_sha256=h.text_sha256, score=h.score, rank=h.rank
                )
                for h in hits
            ],
            reranker_id=None,
            llm_id=gen.model_ref if gen is not None else generator.model_ref,
            llm_params=gen.params if gen is not None else generator.default_params,
            response_text=response_text,
            response_sha256=ids.sha256_hex(response_text or ""),
            latency=StageLatency(
                retrieve_ms=_ms(t1 - t0), generate_ms=_ms(t2 - t1), total_ms=_ms(t2 - t0)
            ),
            tokens_in=gen.tokens_in if gen is not None else 0,
            tokens_out=gen.tokens_out if gen is not None else 0,
            cost_usd=gen.cost_usd if gen is not None else 0.0,
            status="ok" if error is None else "error",
            trace_id=uuid.uuid4().hex,  # a fresh id at Stage 0; a real OTel trace at Stage 7
        )
        d.request_log.write(row)  # C6: the row exists before the client sees the answer
        log.info(
            "ask",
            extra={
                "request_id": row.request_id,
                "status": row.status,
                "index_snapshot_id": row.index_snapshot_id,
                "llm_id": row.llm_id,
                "retrieve_ms": row.latency.retrieve_ms,
                "generate_ms": row.latency.generate_ms,
                "total_ms": row.latency.total_ms,
                "cost_usd": row.cost_usd,
            },
        )
        if gen is None:
            raise HTTPException(
                502, {"request_id": row.request_id, "error": f"{type(error).__name__}: {error}"}
            )
        return AskReply(
            request_id=row.request_id,
            answer=gen.text,
            citations=_cite(hits, context),
            latency=row.latency,
            llm_id=row.llm_id,
            index_snapshot_id=row.index_snapshot_id,
        )

    @app.post("/feedback", response_model=FeedbackReply)
    def feedback(body: FeedbackBody, d: DepsDep) -> FeedbackReply:
        if not d.request_log.exists(body.request_id):
            raise HTTPException(
                404, f"no request {body.request_id}: every signal joins to a request"
            )
        dedup_key = body.dedup_key or ids.sha256_hex(
            "\x1f".join([body.request_id, body.kind, repr(body.value), body.comment or ""])
        )
        try:
            row = Feedback(
                tenant_id=d.tenant_id,
                created_at=datetime.now(tz=UTC),
                feedback_id=ids.new_feedback_id(),
                request_id=body.request_id,
                kind=body.kind,
                value=body.value,
                comment=body.comment,
                actor=body.actor,
                received_at=datetime.now(tz=UTC),
                dedup_key=dedup_key,
                event_schema_version=FEEDBACK_EVENT_SCHEMA_VERSION,
            )
        except ValidationError as err:
            # The contract's rules (thumbs is -1 or 1; corrections need a comment) are the API's.
            raise HTTPException(422, [e["msg"] for e in err.errors()]) from err
        inserted = d.db.insert(row, on_conflict="ON CONFLICT (tenant_id, dedup_key) DO NOTHING")
        if inserted:
            return FeedbackReply(feedback_id=row.feedback_id, duplicate=False)
        existing = d.db.scalar(
            "SELECT feedback_id FROM feedback WHERE tenant_id = %s AND dedup_key = %s",
            (d.tenant_id, dedup_key),
        )
        return FeedbackReply(feedback_id=existing, duplicate=True)

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics(d: DepsDep) -> str:
        return render_metrics(d.db, index_name=d.index_name)

    return app


def _generator_for(body: AskBody, d: Deps) -> Generator:
    if body.generator is None:
        return d.generator
    if not d.settings.allow_generator_override:
        raise HTTPException(
            403, "per-request generators are disabled (CAIRN_ALLOW_GENERATOR_OVERRIDE)"
        )
    spec = body.generator
    return OpenAICompatibleGenerator(
        base_url=spec.base_url,
        model=spec.model,
        provider=spec.provider,
        api_key=spec.api_key,
        settings=d.settings,
        version=spec.version,
    )


def _cite(hits: list[Hit], context: Any) -> list[Citation]:
    by_rank = {c.rank: c for c in context.chunks}
    return [
        Citation(
            rank=h.rank,
            chunk_id=h.chunk_id,
            title=by_rank[h.rank].title,
            heading_path=by_rank[h.rank].heading_path,
            score=round(h.score, 4),
        )
        for h in hits
    ]


def _ms(seconds: float) -> int:
    return max(0, round(seconds * 1000))
