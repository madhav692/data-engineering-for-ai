"""The Cairn data contracts: seven table rows and their nested records.

The lineage spine (docs/architecture/diagrams/lineage-spine.svg):

    Document -> Chunk -> Embedding -> IndexSnapshot -> Request -> Feedback -> EvalRun

Each model declares its table, primary key, lineage fields and partition spec as class
variables; the generators turn those into Avro, Iceberg DDL, JSON Schema and Postgres DDL, and the
tests prove the invariants named in docs/architecture/reference.md.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, ClassVar, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from cairn_schemas import ids
from cairn_schemas.base import Costed, Row
from cairn_schemas.versions import (
    EmbeddingModelId,
    ModelRef,
    Sha256,
    VersionTag,
    parse_embedding_model_id,
)

DOC_ID_PATTERN = r"^doc_[0-9a-f]{32}$"
CHUNK_ID_PATTERN = r"^chk_[0-9a-f]{32}$"
INDEX_SNAPSHOT_ID_PATTERN = r"^idx_[0-9a-f]{32}$"
REQUEST_ID_PATTERN = r"^req_[0-9a-f]{32}$"
FEEDBACK_ID_PATTERN = r"^fb_[0-9a-f]{32}$"
RUN_ID_PATTERN = r"^run_[0-9a-f]{32}$"
JOB_ID_PATTERN = r"^job_[0-9a-f]{32}$"
SOURCE_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
INDEX_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]*--[a-z0-9][a-z0-9-]*$"

# Vectors are stored as float32 in the lakehouse (4 bytes per dimension); the generators
# read the overrides below because Python has no float32.
Vector = Annotated[
    list[float],
    Field(
        min_length=1,
        description="The embedding, float32. 1,024 dims x 4 bytes = 4 KB per row.",
        json_schema_extra={
            "iceberg_type": "ARRAY<FLOAT>",
            "avro_type": {"type": "array", "items": "float"},
            "postgres_type": "REAL[]",
        },
    ),
]


def _now() -> datetime:
    return datetime.now(tz=UTC)


# --------------------------------------------------------------------------------------
# 1. Ingestion and registry -> Lakehouse (contract C1)
# --------------------------------------------------------------------------------------


class Document(Row):
    """Registry entry: one row per (document, revision).

    Identity (``doc_id``) comes from where the document lives; version (``content_sha256``)
    from what it contains; ``revision`` orders a document's history. A delete is a new
    revision with ``status='deleted'``, never a missing row.
    """

    TABLE: ClassVar[str] = "documents"
    PRIMARY_KEY: ClassVar[tuple[str, ...]] = ("doc_id", "revision")
    LINEAGE_FIELDS: ClassVar[tuple[str, ...]] = ("content_sha256",)
    PARTITION_BY: ClassVar[tuple[str, ...]] = ("source_id", "days(fetched_at)")

    doc_id: str = Field(
        pattern=DOC_ID_PATTERN,
        description="Stable identity: hash of (source_id, canonical uri). Never changes on edit.",
    )
    revision: int = Field(
        ge=1, description="1-based position in this document's history; a delete is a revision too."
    )
    source_id: str = Field(
        pattern=SOURCE_ID_PATTERN,
        description="Connector that produced the row, e.g. 'arxiv', 'docs-iceberg', 'tickets'.",
    )
    uri: str = Field(
        min_length=1, max_length=2048, description="Where the document was fetched from."
    )
    content_sha256: Sha256 = Field(description="Version of the document: SHA-256 of its raw bytes.")
    content_type: str = Field(min_length=1, description="MIME type of the raw bytes.")
    size_bytes: int = Field(ge=0, description="Size of the raw bytes.")
    fetched_at: AwareDatetime = Field(description="When the connector fetched this version.")
    source_updated_at: AwareDatetime | None = Field(
        default=None,
        description="The source's own modification time, when it exposes one. Drives index lag.",
    )
    acl: list[str] = Field(
        default_factory=list,
        description="Principals allowed to read the document; inherited by its chunks.",
    )
    status: Literal["active", "deleted"] = Field(
        default="active", description="Deletes travel as tombstones, never as absence."
    )
    raw_object_key: str = Field(
        min_length=1,
        description="Object-store key of the raw bytes: raw/{source_id}/{sha[:2]}/{sha}.",
    )

    @model_validator(mode="after")
    def _derived_fields_agree(self) -> Document:
        expected_id = ids.doc_id(self.source_id, self.uri)
        if self.doc_id != expected_id:
            raise ValueError(f"doc_id {self.doc_id} != derived {expected_id} for {self.uri!r}")
        expected_key = ids.raw_object_key(self.source_id, self.content_sha256)
        if self.raw_object_key != expected_key:
            raise ValueError(f"raw_object_key {self.raw_object_key} != {expected_key}")
        return self

    @classmethod
    def from_bytes(
        cls,
        *,
        tenant_id: str,
        source_id: str,
        uri: str,
        data: bytes,
        content_type: str,
        fetched_at: datetime | None = None,
        revision: int = 1,
        source_updated_at: datetime | None = None,
        acl: list[str] | tuple[str, ...] = (),
        status: Literal["active", "deleted"] = "active",
        created_at: datetime | None = None,
    ) -> Document:
        """Build a registry row from raw bytes; identity, version and object key are derived."""
        sha = ids.sha256_hex(data)
        return cls(
            tenant_id=tenant_id,
            created_at=created_at or _now(),
            doc_id=ids.doc_id(source_id, uri),
            revision=revision,
            source_id=source_id,
            uri=uri,
            content_sha256=sha,
            content_type=content_type,
            size_bytes=len(data),
            fetched_at=fetched_at or _now(),
            source_updated_at=source_updated_at,
            acl=list(acl),
            status=status,
            raw_object_key=ids.raw_object_key(source_id, sha),
        )

    @staticmethod
    def is_change(latest: Document | None, candidate: Document) -> bool:
        """Contract C1: a refetch that produced the same version and status is a no-op."""
        if latest is None:
            return True
        return (latest.content_sha256, latest.status) != (
            candidate.content_sha256,
            candidate.status,
        )


# --------------------------------------------------------------------------------------
# 3. Derivation -> Lakehouse (contract C3)
# --------------------------------------------------------------------------------------


class Chunk(Row):
    """A piece of a document version, cut by a versioned parser and chunker.

    ``chunk_id`` is content-addressed: it excludes ordinal, byte offsets and the document's
    content hash, so a chunk whose text did not change keeps its id across revisions and
    position shifts. That is what makes "only re-embed what changed" possible.
    """

    TABLE: ClassVar[str] = "chunks"
    PRIMARY_KEY: ClassVar[tuple[str, ...]] = ("chunk_id",)
    LINEAGE_FIELDS: ClassVar[tuple[str, ...]] = (
        "content_sha256",
        "parser_version",
        "chunker_version",
    )
    PARTITION_BY: ClassVar[tuple[str, ...]] = ("chunker_version", "bucket(128, doc_id)")

    chunk_id: str = Field(
        pattern=CHUNK_ID_PATTERN,
        description="hash(doc_id, parser_version, chunker_version, text_sha256, occurrence).",
    )
    doc_id: str = Field(pattern=DOC_ID_PATTERN, description="The document this chunk belongs to.")
    content_sha256: Sha256 = Field(
        description="The document version this row was cut from (latest observed on MERGE)."
    )
    parser_version: VersionTag = Field(
        description="Parser that produced the text, e.g. pymupdf-1.24.9."
    )
    chunker_version: VersionTag = Field(description="Chunker that cut it, e.g. struct-v2.")
    ordinal: int = Field(ge=0, description="Position of the chunk within the document version.")
    occurrence: int = Field(
        default=0,
        ge=0,
        description=(
            "Index among chunks of this document with identical text; keeps repeats distinct."
        ),
    )
    byte_start: int = Field(ge=0, description="Start offset in the parsed text (for citations).")
    byte_end: int = Field(ge=1, description="End offset in the parsed text, exclusive.")
    text: str = Field(min_length=1, description="The chunk text as it was embedded.")
    text_sha256: Sha256 = Field(
        description="SHA-256 of text; the join key for dedup across chunks."
    )
    token_count: int = Field(
        ge=1, description="Tokens in text, counted with the embedding tokenizer."
    )
    metadata: dict[str, str] = Field(
        default_factory=dict,
        description="Flat string metadata: heading path, page, language, section type.",
    )
    acl: list[str] = Field(default_factory=list, description="Inherited from the document.")

    @model_validator(mode="after")
    def _derived_fields_agree(self) -> Chunk:
        if self.byte_end <= self.byte_start:
            raise ValueError("byte_end must be greater than byte_start")
        expected_text_sha = ids.sha256_hex(self.text)
        if self.text_sha256 != expected_text_sha:
            raise ValueError("text_sha256 does not match text")
        expected_id = ids.chunk_id(
            self.doc_id,
            self.parser_version,
            self.chunker_version,
            self.text_sha256,
            self.occurrence,
        )
        if self.chunk_id != expected_id:
            raise ValueError(f"chunk_id {self.chunk_id} != derived {expected_id}")
        return self

    @classmethod
    def build(
        cls,
        *,
        document: Document,
        parser_version: str,
        chunker_version: str,
        ordinal: int,
        byte_start: int,
        byte_end: int,
        text: str,
        token_count: int,
        occurrence: int = 0,
        metadata: dict[str, str] | None = None,
        created_at: datetime | None = None,
    ) -> Chunk:
        """Build a chunk of a document; id and text hash are derived."""
        text_sha = ids.sha256_hex(text)
        return cls(
            tenant_id=document.tenant_id,
            created_at=created_at or _now(),
            chunk_id=ids.chunk_id(
                document.doc_id, parser_version, chunker_version, text_sha, occurrence
            ),
            doc_id=document.doc_id,
            content_sha256=document.content_sha256,
            parser_version=parser_version,
            chunker_version=chunker_version,
            ordinal=ordinal,
            occurrence=occurrence,
            byte_start=byte_start,
            byte_end=byte_end,
            text=text,
            text_sha256=text_sha,
            token_count=token_count,
            metadata=dict(metadata or {}),
            acl=list(document.acl),
        )


class Embedding(Costed, Row):
    """A vector for one chunk under one embedding model.

    The model id is the namespace: an index never mixes models, and a model change is a
    new partition, not an update in place (ADR-0001, A16).
    """

    TABLE: ClassVar[str] = "embeddings"
    PRIMARY_KEY: ClassVar[tuple[str, ...]] = ("chunk_id", "embedding_model_id")
    LINEAGE_FIELDS: ClassVar[tuple[str, ...]] = ("embedding_model_id",)
    PARTITION_BY: ClassVar[tuple[str, ...]] = ("embedding_model_id", "bucket(128, chunk_id)")

    chunk_id: str = Field(pattern=CHUNK_ID_PATTERN, description="The chunk that was embedded.")
    text_sha256: Sha256 = Field(
        description="What was actually embedded; lets dedup reuse a vector across identical texts."
    )
    embedding_model_id: EmbeddingModelId
    vector: Vector
    dims: int = Field(ge=1, description="Length of vector; must equal the model id's dims.")
    job_id: str = Field(pattern=JOB_ID_PATTERN, description="The embedding job that wrote the row.")

    @model_validator(mode="after")
    def _dims_agree(self) -> Embedding:
        declared = parse_embedding_model_id(self.embedding_model_id).dims
        if self.dims != declared:
            raise ValueError(
                f"dims {self.dims} != {declared} declared by {self.embedding_model_id}"
            )
        if len(self.vector) != self.dims:
            raise ValueError(f"vector has {len(self.vector)} values, dims says {self.dims}")
        return self


# --------------------------------------------------------------------------------------
# 4. Lakehouse -> Indexes (contract C4)
# --------------------------------------------------------------------------------------


class IndexSnapshot(Row):
    """One build of one index from one lakehouse snapshot under one model.

    This is the record Day 23 lacked: it says what the index was built from, so index lag
    is a computed number and a response can name the exact index it read.
    """

    TABLE: ClassVar[str] = "index_snapshots"
    PRIMARY_KEY: ClassVar[tuple[str, ...]] = ("index_snapshot_id",)
    LINEAGE_FIELDS: ClassVar[tuple[str, ...]] = ("embedding_model_id", "lakehouse_snapshot_id")
    PARTITION_BY: ClassVar[tuple[str, ...]] = ()

    index_snapshot_id: str = Field(
        pattern=INDEX_SNAPSHOT_ID_PATTERN,
        description="hash(index_name, embedding_model_id, lakehouse_snapshot_id).",
    )
    index_name: str = Field(
        pattern=INDEX_NAME_PATTERN,
        description=(
            "'{base}--{model slug}': the model is part of the name (versions are namespaces)."
        ),
    )
    embedding_model_id: EmbeddingModelId
    lakehouse_snapshot_id: str = Field(
        min_length=1, description="Iceberg snapshot id of the embeddings table the build read."
    )
    built_at: AwareDatetime = Field(description="When the build finished.")
    row_count: int = Field(ge=0, description="Vectors in the index at build time.")
    lag_seconds_at_build: int = Field(
        ge=0,
        description="Newest source_updated_at in the lakehouse minus the snapshot time, at build.",
    )
    status: Literal["building", "live", "retired"] = Field(
        description="Only one snapshot per index_name is live; cutover moves a pointer."
    )

    @model_validator(mode="after")
    def _derived_fields_agree(self) -> IndexSnapshot:
        suffix = "--" + ids.model_slug(self.embedding_model_id)
        if not self.index_name.endswith(suffix):
            raise ValueError(f"index_name {self.index_name!r} must end with {suffix!r}")
        expected = ids.index_snapshot_id(
            self.index_name, self.embedding_model_id, self.lakehouse_snapshot_id
        )
        if self.index_snapshot_id != expected:
            raise ValueError(f"index_snapshot_id {self.index_snapshot_id} != derived {expected}")
        return self


# --------------------------------------------------------------------------------------
# 5. Serving and lineage -> Lakehouse (contract C6)
# --------------------------------------------------------------------------------------


class RetrievedChunk(BaseModel):
    """One retrieved chunk as the model saw it: identity, text identity, score, rank."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: str = Field(pattern=CHUNK_ID_PATTERN)
    text_sha256: Sha256 = Field(description="Pins the exact text; chunk ids are content-addressed.")
    score: float = Field(description="Retrieval score after fusion, if any.")
    rank: int = Field(ge=1, description="1-based rank in the context, after reranking.")


class LlmParams(BaseModel):
    """Generation parameters; part of the lineage of every answer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    temperature: float = Field(ge=0, le=2)
    max_tokens: int = Field(ge=1)
    top_p: float = Field(default=1.0, ge=0, le=1)
    seed: int | None = Field(default=None, description="Set when the provider honours seeds.")


class StageLatency(BaseModel):
    """Milliseconds per serving stage; the latency budget is checked against these (A19)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    retrieve_ms: int = Field(ge=0)
    rerank_ms: int = Field(default=0, ge=0)
    generate_ms: int = Field(ge=0)
    total_ms: int = Field(ge=0)


class Request(Costed, Row):
    """The request log: everything needed to replay a response from its request_id.

    Written with every lineage id before the response is returned (or enqueued durably).
    Hot copy in Postgres for 30 days, history in Iceberg.
    """

    TABLE: ClassVar[str] = "requests"
    PRIMARY_KEY: ClassVar[tuple[str, ...]] = ("request_id",)
    LINEAGE_FIELDS: ClassVar[tuple[str, ...]] = (
        "prompt_template_version",
        "retrieval_config_version",
        "index_snapshot_id",
        "embedding_model_id",
        "reranker_id",
        "llm_id",
    )
    PARTITION_BY: ClassVar[tuple[str, ...]] = ("days(received_at)",)

    request_id: str = Field(pattern=REQUEST_ID_PATTERN, description="Minted at the edge; UUIDv7.")
    user_id_hash: str | None = Field(
        default=None, description="Keyed hash of the user id; never the raw id."
    )
    received_at: AwareDatetime = Field(description="When the request arrived.")
    query_text: str | None = Field(
        default=None,
        description="The question, unless the tenant's PII policy forbids retention.",
    )
    query_sha256: Sha256 = Field(description="Always kept, even when query_text is not.")
    prompt_template_version: VersionTag
    retrieval_config_version: VersionTag
    index_snapshot_id: str = Field(
        pattern=INDEX_SNAPSHOT_ID_PATTERN, description="The exact index this request read."
    )
    embedding_model_id: EmbeddingModelId
    filters: dict[str, str] = Field(
        default_factory=dict,
        description="Filters applied inside the index query (tenant, ACL, time); never after it.",
    )
    retrieved_chunks: list[RetrievedChunk] = Field(
        default_factory=list, description="What the model saw, in context order."
    )
    reranker_id: ModelRef | None = Field(default=None)
    llm_id: ModelRef
    llm_params: LlmParams
    response_text: str | None = Field(
        default=None, description="The answer, unless the tenant's PII policy forbids retention."
    )
    response_sha256: Sha256 = Field(description="Always kept, even when response_text is not.")
    latency: StageLatency
    status: Literal["ok", "error", "timeout"]
    trace_id: str = Field(
        min_length=1, description="OpenTelemetry trace id; joins the request to its spans."
    )

    @model_validator(mode="after")
    def _context_is_well_formed(self) -> Request:
        ranks = [c.rank for c in self.retrieved_chunks]
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("retrieved_chunks must be in rank order 1..n with no gaps")
        chunk_ids = [c.chunk_id for c in self.retrieved_chunks]
        if len(set(chunk_ids)) != len(chunk_ids):
            raise ValueError("retrieved_chunks must not repeat a chunk_id")
        return self


# --------------------------------------------------------------------------------------
# 6. Feedback and evaluation (contracts C7, C8)
# --------------------------------------------------------------------------------------


class Feedback(Row):
    """A signal about one request. Evidence, never an approval to train.

    Delivered at least once; ``dedup_key`` makes the second delivery a no-op.
    """

    TABLE: ClassVar[str] = "feedback"
    PRIMARY_KEY: ClassVar[tuple[str, ...]] = ("feedback_id",)
    LINEAGE_FIELDS: ClassVar[tuple[str, ...]] = ("event_schema_version",)
    PARTITION_BY: ClassVar[tuple[str, ...]] = ("days(received_at)",)

    feedback_id: str = Field(pattern=FEEDBACK_ID_PATTERN)
    request_id: str = Field(
        pattern=REQUEST_ID_PATTERN, description="Every signal joins to a request."
    )
    kind: Literal["thumbs", "rating", "correction", "dispute", "click"]
    value: float = Field(description="thumbs: -1 or 1; rating: 1-5; click: 1; others: 1.")
    comment: str | None = Field(default=None, description="Required for corrections and disputes.")
    actor: Literal["user", "reviewer", "judge"]
    received_at: AwareDatetime
    dedup_key: str = Field(
        min_length=1, max_length=256, description="Producer idempotency key; unique per tenant."
    )
    event_schema_version: VersionTag = Field(
        description="Version of the application's event schema that produced this row."
    )

    @model_validator(mode="after")
    def _value_matches_kind(self) -> Feedback:
        if self.kind == "thumbs" and self.value not in (-1.0, 1.0):
            raise ValueError("thumbs value must be -1 or 1")
        if self.kind == "rating" and not (1.0 <= self.value <= 5.0):
            raise ValueError("rating value must be between 1 and 5")
        if self.kind == "click" and self.value != 1.0:
            raise ValueError("click value must be 1")
        if self.kind in ("correction", "dispute") and not (self.comment and self.comment.strip()):
            raise ValueError(f"{self.kind} requires a comment")
        return self


class EvalConfig(BaseModel):
    """The versions an evaluation ran against: the same keys a Request carries.

    The contract test asserts that every field here exists on Request with the same type,
    so an eval run and a production request are always comparable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt_template_version: VersionTag
    retrieval_config_version: VersionTag
    index_snapshot_id: str = Field(pattern=INDEX_SNAPSHOT_ID_PATTERN)
    embedding_model_id: EmbeddingModelId
    reranker_id: ModelRef | None = Field(default=None)
    llm_id: ModelRef
    llm_params: LlmParams


class EvalRun(Costed, Row):
    """One evaluation of one configuration against one version of one eval set.

    Two runs with equal ``config`` and equal (eval_set_id, eval_set_version) are comparable;
    CI compares a candidate run to ``baseline_run_id`` before a data change ships (C8).
    """

    TABLE: ClassVar[str] = "eval_runs"
    PRIMARY_KEY: ClassVar[tuple[str, ...]] = ("run_id",)
    LINEAGE_FIELDS: ClassVar[tuple[str, ...]] = ("eval_set_id", "eval_set_version")
    PARTITION_BY: ClassVar[tuple[str, ...]] = ()

    run_id: str = Field(pattern=RUN_ID_PATTERN)
    eval_set_id: str = Field(min_length=1, description="The golden set, e.g. 'cairn-core'.")
    eval_set_version: VersionTag = Field(
        description="Version of the golden set; sets are immutable."
    )
    config: EvalConfig
    metrics: dict[str, float] = Field(
        default_factory=dict, description="e.g. recall_at_5, mrr, faithfulness, p95_ms."
    )
    baseline_run_id: str | None = Field(
        default=None, pattern=RUN_ID_PATTERN, description="The run this one is compared against."
    )
    git_sha: str = Field(pattern=r"^[0-9a-f]{7,40}$", description="Code version of the runner.")
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = Field(default=None)
    status: Literal["running", "succeeded", "failed"]

    @model_validator(mode="after")
    def _times_are_ordered(self) -> EvalRun:
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        return self


# The seven tables, in lineage-spine order. The generators and the tests iterate this.
ALL_MODELS: tuple[type[Row], ...] = (
    Document,
    Chunk,
    Embedding,
    IndexSnapshot,
    Request,
    Feedback,
    EvalRun,
)
