"""The ``cairn`` command: everything that is not an HTTP request.

cairn migrate                      create the schema (idempotent)
cairn ingest [--prune] SPEC...     SPEC is NAME=PATH, or PATH (name = folder name)
cairn build-index                  build the index for the live model; flip the pointer
cairn ask "QUESTION"               POST /ask and print the answer, citations and request_id
cairn replay REQUEST_ID [--diff B] print a request's lineage and re-run its retrieval
cairn feedback REQUEST_ID ...      POST /feedback
cairn requests [--limit N]         recent request ids (for replay and feedback)
cairn eval [--set small]           run a golden set; write an EvalRun
cairn lag                          index_lag_seconds per source
cairn stats                        corpus and database numbers for CHANGES-stage-0.md
cairn serve                        migrate, then run the api (what the container runs)
"""

from __future__ import annotations

import logging
import re
import sys
import time
from pathlib import Path
from typing import Annotated

import httpx
import typer

from cairn_schemas import ids

from cairn.config import Settings
from cairn.derivation import pipeline
from cairn.derivation.embedder import effective_model_id, embedder_from_settings
from cairn.indexes import build as index_build
from cairn.indexes.reconcile import index_lag_seconds, live_snapshot
from cairn.ingestion.filesystem import ingest_source
from cairn.lakehouse.db import Db
from cairn.lakehouse.migrate import migrate as run_migrate
from cairn.lakehouse.objects import object_store_from_settings
from cairn.observability.logging import configure as configure_logging

app = typer.Typer(
    add_completion=False,
    help="Cairn, Stage 0: the walking skeleton.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
log = logging.getLogger("cairn.cli")

_SLUG = re.compile(r"[^a-z0-9_-]+")


def _settings() -> Settings:
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    return settings


def _db(settings: Settings) -> Db:
    db = Db(settings.database_url)
    if not db.ping():
        typer.echo(f"cannot reach the database at {settings.redacted()['database_url']}", err=True)
        raise typer.Exit(2)
    return db


def _parse_spec(spec: str) -> tuple[str, Path]:
    if "=" in spec:
        name, _, path = spec.partition("=")
    else:
        path = spec
        name = _SLUG.sub("-", Path(path).resolve().name.lower()).strip("-")
    if not re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", name):
        raise typer.BadParameter(f"source name {name!r} must match [a-z0-9][a-z0-9_-]*")
    return name, Path(path)


@app.command()
def migrate(reset: Annotated[bool, typer.Option(help="Drop everything first.")] = False) -> None:
    """Create the schema: the seven contract tables, then the platform's own objects."""
    settings = _settings()
    db = _db(settings)
    applied = run_migrate(db, reset=reset)
    object_store_from_settings(settings)  # creates the bucket
    typer.echo("migrated: " + ", ".join(applied))


@app.command()
def ingest(
    specs: Annotated[list[str], typer.Argument(help="NAME=PATH or PATH, one per source.")],
    prune: Annotated[bool, typer.Option(help="Tombstone documents whose file is gone.")] = False,
) -> None:
    """Register every Markdown file under each source; chunk and embed what changed."""
    settings = _settings()
    db = _db(settings)
    store = object_store_from_settings(settings)
    embedder = embedder_from_settings(settings)
    t0 = time.perf_counter()
    changed = []
    for spec in specs:
        source_id, root = _parse_spec(spec)
        report = ingest_source(
            db, store, root=root, source_id=source_id, tenant_id=settings.tenant_id, prune=prune
        )
        typer.echo(report.line())
        changed.extend(report.changed)

    def progress(done: int, total: int) -> None:
        typer.echo(f"  embedding {done}/{total}", err=True)

    derivation = pipeline.run(db, store, embedder, changed, progress=progress)
    derivation.elapsed_seconds = time.perf_counter() - t0
    typer.echo(derivation.line())


@app.command("build-index")
def build_index() -> None:
    """Build the index for the live embedding model from the lakehouse; flip the live pointer."""
    settings = _settings()
    db = _db(settings)
    model_id = effective_model_id(settings)
    try:
        result = index_build.build_index(
            db,
            model_id,
            tenant_id=settings.tenant_id,
            base_name=settings.index_base_name,
            retain=settings.index_retain,
        )
    except RuntimeError as err:
        typer.echo(str(err), err=True)
        raise typer.Exit(1) from err
    snap = result.snapshot
    typer.echo(f"index {snap.index_name}   rows {snap.row_count}   built in {result.seconds:.1f}s")
    typer.echo(
        f"snapshot {snap.index_snapshot_id}   status {snap.status}   "
        f"index_lag_seconds {snap.lag_seconds_at_build}"
    )
    if result.dropped_tables:
        typer.echo(f"dropped {len(result.dropped_tables)} table(s) beyond retention", err=True)


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="The question.")],
    api: Annotated[str | None, typer.Option(help="The api base URL.")] = None,
) -> None:
    """POST /ask; print the answer, the citations and the request_id."""
    settings = _settings()
    body: dict = {"question": question}
    if settings.llm_base_url:
        # The CLI's own CAIRN_LLM_* names a generator for this one request (the demo swap).
        body["generator"] = {
            "base_url": settings.llm_base_url,
            "model": settings.llm_model or "default",
            "provider": settings.llm_provider,
            "api_key": settings.llm_api_key,
            "version": settings.llm_model_version,
        }
    reply = _post(api or settings.api_url, "/ask", body, timeout=settings.llm_timeout_seconds + 30)
    typer.echo(reply["answer"])
    typer.echo("")
    typer.echo("citations")
    for c in reply["citations"]:
        where = f"{c['title']} › {c['heading_path']}" if c["heading_path"] else c["title"]
        typer.echo(f"  {c['rank']}  {where:<60} score {c['score']:.3f}")
    lat = reply["latency"]
    typer.echo(
        f"request_id  {reply['request_id']}        retrieve {lat['retrieve_ms']} ms · "
        f"generate {lat['generate_ms']} ms · total {lat['total_ms']} ms"
        f"        llm {reply['llm_id']}"
    )


@app.command()
def replay(
    request_id: Annotated[str, typer.Argument(help="A request_id (req_...).")],
    diff: Annotated[str | None, typer.Option(help="Diff against this second request_id.")] = None,
    no_embed: Annotated[
        bool, typer.Option("--no-embed", help="Skip re-running retrieval (no model load).")
    ] = False,
) -> None:
    """Print a request's lineage and context; re-run its retrieval against the logged snapshot."""
    from cairn.lineage import replay as replay_mod

    settings = _settings()
    db = _db(settings)
    try:
        if diff:
            text, changed = replay_mod.diff(db, request_id, diff)
            typer.echo(text)
            typer.echo("")
            typer.echo(
                f"{len(changed)} field(s) differ: {', '.join(changed) if changed else 'none'}"
            )
            return
        embedder = None if no_embed else embedder_from_settings(settings)
        report = replay_mod.replay(db, embedder, request_id)
    except LookupError as err:
        typer.echo(str(err), err=True)
        raise typer.Exit(1) from err
    typer.echo(replay_mod.format_report(report))
    # Exit 1 when the row cannot be trusted or the re-run disagrees, so scripts can tell.
    if (
        report.inconsistent_chunks
        or report.chunk_ids_match is False
        or report.response_matches is False
    ):
        raise typer.Exit(1)


@app.command()
def feedback(
    request_id: Annotated[str, typer.Argument()],
    kind: Annotated[
        str, typer.Option(help="thumbs | rating | correction | dispute | click")
    ] = "thumbs",
    value: Annotated[float, typer.Option(help="thumbs: -1 or 1; rating: 1-5; click: 1")] = 1.0,
    comment: Annotated[str | None, typer.Option()] = None,
    dedup_key: Annotated[
        str | None, typer.Option(help="Idempotency key; defaults to a hash.")
    ] = None,
    api: Annotated[str | None, typer.Option()] = None,
) -> None:
    """POST /feedback for a request."""
    settings = _settings()
    body = {"request_id": request_id, "kind": kind, "value": value, "comment": comment}
    if dedup_key:
        body["dedup_key"] = dedup_key
    reply = _post(api or settings.api_url, "/feedback", body)
    note = "duplicate: no new row" if reply["duplicate"] else "recorded"
    typer.echo(f"feedback {reply['feedback_id']}   {note}")


@app.command("eval")
def eval_(
    set_name: Annotated[
        str, typer.Option("--set", help="Golden set name under the golden dir.")
    ] = "small",
    golden_path: Annotated[
        Path | None, typer.Option("--golden", help="Explicit JSONL path.")
    ] = None,
    api: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Run a golden set through /ask; print recall@k and write an EvalRun."""
    from cairn.evaluation.golden import load_golden
    from cairn.evaluation.runner import run_eval

    settings = _settings()
    db = _db(settings)
    path = golden_path or Path(settings.golden_dir) / f"{set_name}.jsonl"
    golden = load_golden(path, set_id=set_name)
    base = api or settings.api_url
    report = run_eval(db, lambda q: _post(base, "/ask", {"question": q}), golden, settings=settings)
    for r in report.results:
        mark = "hit " if r.hit else "miss"
        rank = f"@{r.first_hit_rank}" if r.first_hit_rank else "   "
        typer.echo(f"  {mark} {rank}  {r.question}")
    typer.echo(report.line())


@app.command()
def requests(
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many, newest first.")] = 10,
) -> None:
    """Recent requests: id, time, status, generator, question (the ids replay and feedback take)."""
    settings = _settings()
    db = _db(settings)
    rows = db.rows(
        "SELECT request_id, received_at, status, llm_id, query_text FROM requests "
        "ORDER BY received_at DESC LIMIT %s",
        (limit,),
    )
    if not rows:
        typer.echo('no requests yet: cairn ask "..."')
        return
    for r in rows:
        question = (r["query_text"] or "").replace("\n", " ")
        if len(question) > 60:
            question = question[:57] + "..."
        typer.echo(
            f"{r['request_id']}  {r['received_at']:%Y-%m-%d %H:%M:%S}  {r['status']:<6} "
            f"{r['llm_id']:<24} {question}"
        )


@app.command()
def lag() -> None:
    """index_lag_seconds per source, from the reconciliation query."""
    settings = _settings()
    db = _db(settings)
    index_name = ids.index_name_for(settings.index_base_name, effective_model_id(settings))
    lags = index_lag_seconds(db, index_name)
    if lags is None:
        typer.echo(f"no live index {index_name}; run `cairn build-index`")
        raise typer.Exit(1)
    for source, seconds in lags.items():
        typer.echo(f"source {source:<14} index_lag_seconds {seconds}")


@app.command()
def stats() -> None:
    """Corpus and database numbers (for CHANGES-stage-0.md)."""
    from cairn.derivation.chunker import live_versions

    settings = _settings()
    db = _db(settings)
    index_name = ids.index_name_for(settings.index_base_name, effective_model_id(settings))
    with db.transaction() as tx:
        docs = tx.row(
            "SELECT count(*) FILTER (WHERE status = 'active') AS active, count(*) AS latest, "
            "coalesce(sum(size_bytes) FILTER (WHERE status = 'active'), 0) AS raw_bytes "
            "FROM latest_documents"
        )
        revisions = tx.scalar("SELECT count(*) FROM documents")
        chunks = tx.row(
            "SELECT count(*) AS n, coalesce(sum(c.token_count), 0) AS tokens FROM chunks c "
            "JOIN latest_documents d ON d.doc_id = c.doc_id "
            "AND d.content_sha256 = c.content_sha256 "
            "WHERE d.status = 'active' AND c.parser_version = %(parser_version)s "
            "AND c.chunker_version = %(chunker_version)s",
            live_versions(),
        )
        all_chunks = tx.scalar("SELECT count(*) FROM chunks")
        embeddings = tx.rows(
            "SELECT embedding_model_id, count(*) AS n FROM embeddings GROUP BY 1 ORDER BY 1"
        )
        requests = tx.scalar("SELECT count(*) FROM requests")
        size = tx.scalar("SELECT pg_size_pretty(pg_database_size(current_database()))")
        snapshot = live_snapshot(tx, index_name)
    typer.echo(
        f"documents   active {docs['active']}   latest rows {docs['latest']}   "
        f"revisions {revisions}   raw bytes {int(docs['raw_bytes']):,}"
    )
    typer.echo(
        f"chunks      live {chunks['n']}   tokens {int(chunks['tokens']):,}   "
        f"all versions {all_chunks}"
    )
    for row in embeddings:
        typer.echo(f"embeddings  {row['embedding_model_id']}   {row['n']}")
    if snapshot:
        typer.echo(
            f"index       {snapshot.index_name}   rows {snapshot.row_count}   "
            f"built {snapshot.built_at.isoformat(timespec='seconds')}   "
            f"{snapshot.index_snapshot_id}"
        )
    else:
        typer.echo(f"index       {index_name}   (not built)")
    typer.echo(f"requests    {requests}")
    typer.echo(f"database    {size}")


@app.command()
def serve(
    host: Annotated[str, typer.Option()] = "0.0.0.0",  # noqa: S104 - a container port
    port: Annotated[int, typer.Option()] = 8000,
) -> None:
    """Migrate, then run the api. The api container's command."""
    import uvicorn

    from cairn.serving.app import create_app

    settings = _settings()
    db = _db(settings)
    run_migrate(db)
    object_store_from_settings(settings)
    db.close()
    log.info("schema migrated; starting api", extra={"port": port})
    uvicorn.run(create_app(settings), host=host, port=port, log_config=None, access_log=False)


@app.command()
def health(api: Annotated[str | None, typer.Option()] = None) -> None:
    """GET /healthz and print one line (what `make start` prints when the api is up)."""
    settings = _settings()
    base = (api or settings.api_url).rstrip("/")
    try:
        reply = httpx.get(base + "/healthz", timeout=10).json()
    except (httpx.HTTPError, ValueError) as err:
        typer.echo(f"api not reachable at {base}: {err}", err=True)
        raise typer.Exit(2) from err
    live = reply.get("live_snapshot") or "none (run `make ingest` then `make build-index`)"
    typer.echo(
        f"api listening on {base}  (embedding model {reply['embedding_model_id']}; "
        f"generator {reply['generator'].split(' (')[0]}; live snapshot {live})"
    )


@app.command()
def config() -> None:
    """Print the effective settings (secrets redacted)."""
    for key, value in _settings().redacted().items():
        typer.echo(f"{key:<28} {value}")


def _post(base_url: str, path: str, body: dict, *, timeout: float = 60.0) -> dict:
    try:
        response = httpx.post(base_url.rstrip("/") + path, json=body, timeout=timeout)
    except httpx.HTTPError as err:
        typer.echo(f"cannot reach the api at {base_url}: {err}", err=True)
        raise typer.Exit(2) from err
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        typer.echo(f"api returned {response.status_code}: {detail}", err=True)
        raise typer.Exit(1)
    return response.json()


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
