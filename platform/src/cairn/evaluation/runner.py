"""``cairn eval``: run a golden set through /ask, score it, write an ``EvalRun``.

recall@k is document-level: a question counts as a hit when any of its expected documents owns
one of the k retrieved chunks. The run's ``config`` is read back from the Request rows the run
itself made, so an EvalRun and a production request are comparable by construction (contract
C8); a run whose requests disagree on any version is a failed run, not an averaged one.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cairn_schemas import ids
from cairn_schemas.models import EvalConfig, EvalRun, Request

from cairn.config import Settings
from cairn.evaluation.golden import GoldenSet
from cairn.lakehouse.db import Db

Ask = Callable[[str], dict[str, Any]]  # question -> the /ask reply as a dict


@dataclass
class QuestionResult:
    question: str
    request_id: str
    hit: bool
    first_hit_rank: int | None


@dataclass
class EvalReport:
    run: EvalRun
    results: list[QuestionResult]

    def line(self) -> str:
        m = self.run.metrics
        return (
            f"eval {self.run.eval_set_id}@{self.run.eval_set_version} "
            f"({len(self.results)} questions)   "
            f"recall@{int(m['k'])} {m['recall_at_k']:.2f}   mrr {m['mrr']:.2f}   "
            f"run {self.run.run_id}   status {self.run.status}"
        )


def git_sha(settings: Settings, repo_root: Path | None = None) -> str:
    if settings.git_sha:
        return settings.git_sha
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "0000000"  # unknown; the pattern needs 7-40 hex characters


def run_eval(
    db: Db, ask: Ask, golden: GoldenSet, *, settings: Settings, k: int | None = None
) -> EvalReport:
    started = datetime.now(tz=UTC)
    k = k or settings.retrieval_k
    results: list[QuestionResult] = []
    requests: list[Request] = []
    for q in golden.questions:
        reply = ask(q.question)
        row = db.row("SELECT * FROM requests WHERE request_id = %s", (reply["request_id"],))
        request = Request.model_validate(row)
        requests.append(request)
        chunk_ids = [c.chunk_id for c in request.retrieved_chunks[:k]]
        owners = {
            r["chunk_id"]: r["doc_id"]
            for r in db.rows(
                "SELECT chunk_id, doc_id FROM chunks WHERE chunk_id = ANY(%s)", (chunk_ids,)
            )
        }
        expected = q.expected_doc_ids()
        first_hit = next(
            (i + 1 for i, cid in enumerate(chunk_ids) if owners.get(cid) in expected), None
        )
        results.append(
            QuestionResult(q.question, request.request_id, first_hit is not None, first_hit)
        )

    configs = {_config_of(r) for r in requests}
    status = "succeeded" if len(configs) == 1 else "failed"
    config = next(iter(configs)) if configs else None
    if config is None:
        raise RuntimeError("the golden set produced no requests")
    n = len(results)
    metrics = {
        "k": float(k),
        "questions": float(n),
        "recall_at_k": sum(1 for r in results if r.hit) / n,
        "mrr": sum(1.0 / r.first_hit_rank for r in results if r.first_hit_rank) / n,
        "p50_total_ms": float(sorted(r.latency.total_ms for r in requests)[n // 2]),
    }
    run = EvalRun(
        tenant_id=settings.tenant_id,
        created_at=datetime.now(tz=UTC),
        run_id=ids.new_run_id(),
        eval_set_id=golden.set_id,
        eval_set_version=golden.version,
        config=config,
        metrics=metrics,
        baseline_run_id=None,
        git_sha=git_sha(settings),
        started_at=started,
        finished_at=datetime.now(tz=UTC),
        status=status,
        tokens_in=sum(r.tokens_in for r in requests),
        tokens_out=sum(r.tokens_out for r in requests),
        cost_usd=sum(r.cost_usd for r in requests),
    )
    db.insert(run)
    return EvalReport(run=run, results=results)


def _config_of(r: Request) -> EvalConfig:
    """The same keys a Request carries; the contract test asserts the two types agree."""
    return EvalConfig(
        prompt_template_version=r.prompt_template_version,
        retrieval_config_version=r.retrieval_config_version,
        index_snapshot_id=r.index_snapshot_id,
        embedding_model_id=r.embedding_model_id,
        reranker_id=r.reranker_id,
        llm_id=r.llm_id,
        llm_params=r.llm_params,
    )
