"""Contracts C8 and C9. An EvalRun is comparable to production requests by construction; the
metrics endpoint exposes lag and cost computed from the data."""

from __future__ import annotations

import re

from cairn_schemas.models import EvalConfig, EvalRun, Request

from cairn.evaluation.golden import load_golden
from cairn.evaluation.runner import run_eval
from support import Platform


def test_eval_run_config_matches_request_keys(indexed: Platform):
    """EvalRun.config equals the lineage of the requests it made."""
    golden = load_golden(
        indexed.golden_file(
            [
                ("How does hidden partitioning work?", ["docs/partitioning.md"]),
                ("What is a snapshot?", ["docs/snapshots.md"]),
                ("How long is the refund window?", ["docs/policy.md"]),
            ]
        ),
        set_id="test",
    )
    requests_before = indexed.count("requests")
    report = run_eval(
        indexed.db,
        lambda q: indexed.client.post("/ask", json={"question": q}).json(),
        golden,
        settings=indexed.settings,
    )
    run = report.run
    assert run.status == "succeeded"
    assert indexed.count("requests") == requests_before + 3
    assert indexed.count("eval_runs") == 1
    stored = EvalRun.model_validate(
        indexed.db.row("SELECT * FROM eval_runs WHERE run_id = %s", (run.run_id,))
    )
    assert stored == run

    # the same keys, the same values, for every request the run made
    for result in report.results:
        request = indexed.request(result.request_id)
        assert run.config == EvalConfig(**{k: getattr(request, k) for k in EvalConfig.model_fields})
    assert set(EvalConfig.model_fields) <= set(Request.model_fields)

    assert run.eval_set_id == "test" and len(run.eval_set_version) == 12
    assert run.git_sha == "deadbeef"
    assert 0.0 <= run.metrics["recall_at_k"] <= 1.0
    assert run.metrics["questions"] == 3.0 and run.metrics["k"] == 5.0
    # three documents, five retrieved chunks: the expected document is almost always among them
    assert run.metrics["recall_at_k"] >= 2 / 3


def test_metrics_expose_lag_and_cost(indexed: Platform):
    """index_lag_seconds and cost_usd_total are present and numeric; counts match the tables."""
    indexed.ask("How does hidden partitioning work?")
    body = indexed.client.get("/metrics").text
    metrics = dict(re.findall(r"^(\S+) (\S+)$", body, flags=re.MULTILINE))

    assert metrics['cairn_index_lag_seconds{source="docs"}'] == "0"
    assert float(metrics["cairn_cost_usd_total"]) == 0.0
    assert metrics['cairn_requests_total{status="ok"}'] == "1"
    assert int(metrics["cairn_documents_total"]) == 3
    assert int(metrics["cairn_chunks_total"]) == indexed.count("chunks")
    assert int(
        metrics[f'cairn_embeddings_total{{model="{indexed.embedder.model_id}"}}']
    ) == indexed.count("embeddings")
    snapshot_rows = indexed.db.scalar("SELECT row_count FROM index_snapshots WHERE status = 'live'")
    index_name = next(k for k in metrics if k.startswith("cairn_index_rows{"))
    assert int(metrics[index_name]) == snapshot_rows
    assert float(metrics['cairn_request_latency_ms{stage="retrieve",quantile="0.5"}']) >= 0
    assert float(metrics['cairn_cost_usd{table="embeddings"}']) == 0.0
