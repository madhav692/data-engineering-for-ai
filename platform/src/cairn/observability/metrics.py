"""``/metrics`` in the Prometheus text format, computed from the tables on every scrape.

Nothing is cached and nothing lives in process memory, so a restart loses nothing and the
number printed by ``cairn lag`` is the number a scraper sees. A collector, ClickHouse and
Grafana arrive at Stage 7; the only thing that changes for this endpoint is who reads it.
"""

from __future__ import annotations

from cairn.derivation.chunker import live_versions
from cairn.indexes.reconcile import index_lag_seconds, live_snapshot
from cairn.lakehouse.db import Db

LATENCY_WINDOW = 1000  # the most recent requests the quantiles are computed over

_PCT = "percentile_cont({q}) WITHIN GROUP (ORDER BY (latency->>'{stage}_ms')::numeric) AS {name}"
_QUANTILES_SQL = (
    "SELECT "
    + ", ".join(
        _PCT.format(q=q, stage=stage, name=f"{stage[0]}{int(q * 100)}")
        for stage in ("retrieve", "generate", "total")
        for q in (0.5, 0.95)
    )
    + " FROM (SELECT latency FROM requests ORDER BY received_at DESC LIMIT %s) recent"
)


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _num(value: float | int | None) -> str:
    if value is None:
        return "0"
    if isinstance(value, int):
        return str(value)
    return f"{value:.6g}"


def collect(db: Db, *, index_name: str) -> list[tuple[str, dict[str, str], float | int]]:
    """Every metric as (name, labels, value)."""
    out: list[tuple[str, dict[str, str], float | int]] = []
    with db.transaction() as tx:
        for row in tx.rows(
            "SELECT status, count(*) AS n FROM requests GROUP BY status ORDER BY status"
        ):
            out.append(("cairn_requests_total", {"status": row["status"]}, int(row["n"])))
        quantiles = tx.row(_QUANTILES_SQL, (LATENCY_WINDOW,))
        if quantiles and quantiles["r50"] is not None:
            for stage, keys in (
                ("retrieve", ("r50", "r95")),
                ("generate", ("g50", "g95")),
                ("total", ("t50", "t95")),
            ):
                for quantile, key in zip(("0.5", "0.95"), keys, strict=True):
                    out.append(
                        (
                            "cairn_request_latency_ms",
                            {"stage": stage, "quantile": quantile},
                            float(quantiles[key]),
                        )
                    )
        lag = index_lag_seconds(tx, index_name)
        for source, seconds in (lag or {}).items():
            out.append(("cairn_index_lag_seconds", {"source": source}, seconds))
        out.append(
            (
                "cairn_documents_total",
                {},
                int(tx.scalar("SELECT count(*) FROM latest_documents WHERE status = 'active'")),
            )
        )
        out.append(
            (
                "cairn_chunks_total",
                {},
                int(
                    tx.scalar(
                        "SELECT count(*) FROM chunks c JOIN latest_documents d "
                        "ON d.doc_id = c.doc_id AND d.content_sha256 = c.content_sha256 "
                        "WHERE d.status = 'active' AND c.parser_version = %(parser_version)s "
                        "AND c.chunker_version = %(chunker_version)s",
                        live_versions(),
                    )
                ),
            )
        )
        for row in tx.rows(
            "SELECT embedding_model_id, count(*) AS n FROM embeddings "
            "GROUP BY embedding_model_id ORDER BY 1"
        ):
            out.append(
                ("cairn_embeddings_total", {"model": row["embedding_model_id"]}, int(row["n"]))
            )
        snapshot = live_snapshot(tx, index_name)
        if snapshot:
            out.append(("cairn_index_rows", {"index": snapshot.index_name}, snapshot.row_count))
        for row in tx.rows(
            "SELECT 'requests' AS t, coalesce(sum(cost_usd), 0) AS c FROM requests "
            "UNION ALL SELECT 'embeddings', coalesce(sum(cost_usd), 0) FROM embeddings"
        ):
            out.append(("cairn_cost_usd", {"table": row["t"]}, float(row["c"])))
        out.append(
            (
                "cairn_cost_usd_total",
                {},
                float(
                    tx.scalar(
                        "SELECT (SELECT coalesce(sum(cost_usd), 0) FROM requests) "
                        "+ (SELECT coalesce(sum(cost_usd), 0) FROM embeddings)"
                    )
                ),
            )
        )
    return out


def render_metrics(db: Db, *, index_name: str) -> str:
    lines = []
    for name, labels, value in collect(db, index_name=index_name):
        if labels:
            rendered = ",".join(f'{k}="{_label(str(v))}"' for k, v in labels.items())
            lines.append(f"{name}{{{rendered}}} {_num(value)}")
        else:
            lines.append(f"{name} {_num(value)}")
    return "\n".join(lines) + "\n"
