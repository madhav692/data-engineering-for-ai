"""``cairn replay``: reconstruct what the model saw from a request_id, then check it.

Reads the Request row, prints its lineage and context, loads each retrieved chunk back through
the ``Chunk`` model (so a corrupted row refuses to load and is reported, not used), re-runs
retrieval with the logged configuration against the logged snapshot, and says whether the same
chunk ids came back in the same order. With the extractive generator the answer is a pure
function of the context, so the response hash is checked too.

``diff`` prints two request rows side by side, lineage field by lineage field: the thesis in one
diff when the only lines that differ belong to the generator.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import ValidationError

from cairn_schemas import ids
from cairn_schemas.models import Chunk, IndexSnapshot, Request

from cairn.derivation.embedder import Embedder
from cairn.ingestion.filesystem import display_path
from cairn.lakehouse.db import Db
from cairn.serving.generator import EXTRACTIVE_MODEL_REF, ExtractiveGenerator
from cairn.serving.prompts import PromptTemplate, assemble
from cairn.serving.retrieval import Hit, RetrievalConfig, VectorRetrieval


@dataclass
class ReplayReport:
    request: Request
    snapshot: IndexSnapshot | None
    chunks: dict[str, Chunk | str] = field(default_factory=dict)  # chunk_id -> row or problem
    rerun: list[Hit] | None = None
    rerun_note: str = ""
    chunk_ids_match: bool | None = None
    same_order: bool | None = None
    response_matches: bool | None = None
    response_note: str = ""

    @property
    def inconsistent_chunks(self) -> list[str]:
        return [cid for cid, c in self.chunks.items() if isinstance(c, str)]


def replay(db: Db, embedder: Embedder | None, request_id: str) -> ReplayReport:
    found = db.row("SELECT * FROM requests WHERE request_id = %s", (request_id,))
    if found is None:
        raise LookupError(f"no request {request_id}")
    request = Request.model_validate(found)
    snapshot_row = db.row(
        "SELECT * FROM index_snapshots WHERE index_snapshot_id = %s", (request.index_snapshot_id,)
    )
    snapshot = IndexSnapshot.model_validate(snapshot_row) if snapshot_row else None
    report = ReplayReport(request=request, snapshot=snapshot)

    # 1. The logged chunks, validated on the way in.
    logged_ids = [c.chunk_id for c in request.retrieved_chunks]
    rows = {
        r["chunk_id"]: r
        for r in db.rows("SELECT * FROM chunks WHERE chunk_id = ANY(%s)", (logged_ids,))
    }
    for retrieved in request.retrieved_chunks:
        row = rows.get(retrieved.chunk_id)
        if row is None:
            report.chunks[retrieved.chunk_id] = "missing from chunks"
            continue
        try:
            chunk = Chunk.model_validate(row)
        except ValidationError as err:
            first = err.errors()[0]["msg"]
            report.chunks[retrieved.chunk_id] = f"inconsistent: {first}"
            continue
        if chunk.text_sha256 != retrieved.text_sha256:
            report.chunks[retrieved.chunk_id] = "text changed since the request"
            continue
        report.chunks[retrieved.chunk_id] = chunk

    # 2. Retrieval, re-run with the logged configuration against the logged snapshot.
    if snapshot is None:
        report.rerun_note = "index snapshot row is gone; retrieval cannot be re-run"
    elif embedder is None or embedder.model_id != request.embedding_model_id:
        loaded = embedder.model_id if embedder else "none"
        report.rerun_note = (
            f"request used {request.embedding_model_id} but the loaded embedder is {loaded}; "
            "retrieval not re-run"
        )
    else:
        retrieval = VectorRetrieval(RetrievalConfig.parse(request.retrieval_config_version))
        if not retrieval.table_available(db, snapshot):
            report.rerun_note = (
                f"table of snapshot {snapshot.index_snapshot_id} was dropped "
                "(beyond CAIRN_INDEX_RETAIN); retrieval not re-run"
            )
        elif request.query_text is None:
            report.rerun_note = "query_text was not retained; retrieval not re-run"
        else:
            hits = retrieval.search(
                db,
                snapshot,
                embedder.embed_query(request.query_text),
                tenant_id=request.filters.get("tenant_id", request.tenant_id),
                k=len(logged_ids) or retrieval.config.k,
            )
            report.rerun = hits
            rerun_ids = [h.chunk_id for h in hits]
            report.chunk_ids_match = set(rerun_ids) == set(logged_ids)
            report.same_order = rerun_ids == logged_ids
            # 3. The answer, when the generator is a pure function of the context.
            if request.llm_id == EXTRACTIVE_MODEL_REF:
                template = PromptTemplate.load(request.prompt_template_version)
                regenerated = ExtractiveGenerator().generate(
                    request.query_text, assemble(hits, template, request.query_text)
                )
                report.response_matches = (
                    ids.sha256_hex(regenerated.text) == request.response_sha256
                )
            else:
                report.response_note = (
                    f"generator {request.llm_id} is a model call; inputs are reproduced, "
                    "the wording may differ"
                )
    return report


def format_report(report: ReplayReport) -> str:
    r = report.request
    out: list[str] = []
    out.append(
        f"request    {r.request_id}     received {r.received_at.isoformat(timespec='seconds')}"
        f"     tenant {r.tenant_id}     status {r.status}"
    )
    out.append(f"question   {r.query_text or '(not retained)'}     sha256 {r.query_sha256[:16]}…")
    snap = report.snapshot
    snap_note = (
        f"   {snap.index_name}, built {snap.built_at.isoformat(timespec='seconds')}, "
        f"{snap.row_count} rows"
        if snap
        else "   (snapshot row missing)"
    )
    out.append(f"lineage    {'prompt_template_version':<26} {r.prompt_template_version}")
    out.append(f"           {'retrieval_config_version':<26} {r.retrieval_config_version}")
    out.append(f"           {'index_snapshot_id':<26} {r.index_snapshot_id}{snap_note}")
    out.append(f"           {'embedding_model_id':<26} {r.embedding_model_id}")
    out.append(f"           {'reranker_id':<26} {r.reranker_id or 'none'}")
    out.append(
        f"           {'llm_id':<26} {r.llm_id}     temperature {r.llm_params.temperature}"
        f"   max_tokens {r.llm_params.max_tokens}"
    )
    out.append(
        "context    rank  score    chunk_id                                  document › heading"
    )
    for c in r.retrieved_chunks:
        loaded = report.chunks.get(c.chunk_id)
        if isinstance(loaded, Chunk):
            doc = _document_label(report, loaded)
            where = f"{doc} › {loaded.metadata.get('heading_path', '')}".rstrip(" ›")
        else:
            where = f"!! {loaded}"
        out.append(f"           {c.rank:<5} {c.score:<8.4f} {c.chunk_id}  {where}")
    out.append(
        f"response   sha256 {r.response_sha256[:16]}…     "
        f"tokens {r.tokens_in} in / {r.tokens_out} out     cost ${r.cost_usd:.4f}"
    )
    lat = r.latency
    out.append(
        f"latency    retrieve {lat.retrieve_ms} ms · generate {lat.generate_ms} ms · "
        f"total {lat.total_ms} ms"
    )
    if report.rerun is None:
        out.append(f"replay     {report.rerun_note}")
    else:
        matched = sum(
            1 for h in report.rerun if h.chunk_id in {c.chunk_id for c in r.retrieved_chunks}
        )
        order = "same order" if report.same_order else "different order"
        out.append(
            f"replay     retrieval re-run against {r.index_snapshot_id} with "
            f"{r.retrieval_config_version}: {matched} of {len(r.retrieved_chunks)} "
            f"chunk ids match, {order}"
        )
        if report.response_matches is not None:
            verdict = "matches" if report.response_matches else "DIFFERS"
            out.append(
                f"           generator {r.llm_id} is deterministic: response sha256 {verdict}"
            )
        elif report.response_note:
            out.append(f"           {report.response_note}")
    if report.inconsistent_chunks:
        out.append(
            f"warning    {len(report.inconsistent_chunks)} logged chunk(s) could not be loaded; "
            "see above"
        )
    return "\n".join(out)


def _document_label(report: ReplayReport, chunk: Chunk) -> str:
    for hit in report.rerun or []:
        if hit.chunk_id == chunk.chunk_id:
            return display_path(hit.uri)
    return chunk.doc_id


DIFF_FIELDS: tuple[str, ...] = (
    "prompt_template_version",
    "retrieval_config_version",
    "index_snapshot_id",
    "embedding_model_id",
    "retrieved_chunks",
    "reranker_id",
    "llm_id",
    "llm_params",
    "tokens",
    "cost_usd",
    "response_sha256",
    "latency.generate_ms",
)


def _field_value(r: Request, name: str) -> str:
    match name:
        case "retrieved_chunks":
            return "[" + ", ".join(c.chunk_id for c in r.retrieved_chunks) + "]"
        case "llm_params":
            return f"temperature {r.llm_params.temperature}  max_tokens {r.llm_params.max_tokens}"
        case "tokens":
            return f"{r.tokens_in} / {r.tokens_out}"
        case "cost_usd":
            return f"{r.cost_usd:.4f}"
        case "latency.generate_ms":
            return str(r.latency.generate_ms)
        case "reranker_id":
            return r.reranker_id or "none"
    return str(getattr(r, name))


def diff(db: Db, request_id_a: str, request_id_b: str) -> tuple[str, list[str]]:
    """A unified-style diff of two request rows. Returns (text, names of fields that differ)."""
    rows = {}
    for rid in (request_id_a, request_id_b):
        found = db.row("SELECT * FROM requests WHERE request_id = %s", (rid,))
        if found is None:
            raise LookupError(f"no request {rid}")
        rows[rid] = Request.model_validate(found)
    a, b = rows[request_id_a], rows[request_id_b]
    lines: list[str] = []
    changed: list[str] = []
    for name in DIFF_FIELDS:
        label = "tokens_in / tokens_out" if name == "tokens" else name
        va, vb = _field_value(a, name), _field_value(b, name)
        if va == vb:
            lines.append(f"  {label:<26}{va}")
        else:
            changed.append(name)
            lines.append(f"- {label:<26}{va}")
            lines.append(f"+ {label:<26}{vb}")
    return "\n".join(lines), changed
