"""Index lag: the newest change in the source of truth minus the newest change the live index
reflects (A1's first Monday move). Per source, computed from the data on every call, never cached,
so ``cairn lag``, ``/metrics`` and the build all print the same number.
"""

from __future__ import annotations

from datetime import datetime

from cairn_schemas.models import IndexSnapshot

from cairn.lakehouse.db import Db, Tx

# index_lag_seconds per source, for an index built at %(built_at)s.
LAG_SQL = """
SELECT d.source_id,
       GREATEST(0, EXTRACT(EPOCH FROM
         max(COALESCE(d.source_updated_at, d.fetched_at)) - s.built_at)
       )::bigint AS index_lag_seconds
FROM latest_documents d
CROSS JOIN (SELECT %(built_at)s::timestamptz AS built_at) s
GROUP BY d.source_id, s.built_at
ORDER BY d.source_id
"""


def live_snapshot(db: Db | Tx, index_name: str) -> IndexSnapshot | None:
    """The pointer: the one snapshot of this index with status = 'live'."""
    found = db.row(
        "SELECT * FROM index_snapshots WHERE index_name = %s AND status = 'live' "
        "ORDER BY built_at DESC LIMIT 1",
        (index_name,),
    )
    return IndexSnapshot.model_validate(found) if found else None


def lag_for(db: Db | Tx, built_at: datetime) -> dict[str, int]:
    return {
        row["source_id"]: int(row["index_lag_seconds"])
        for row in db.rows(LAG_SQL, {"built_at": built_at})
    }


def index_lag_seconds(db: Db | Tx, index_name: str) -> dict[str, int] | None:
    """Lag per source against the live snapshot of ``index_name``; None without a live index."""
    snapshot = live_snapshot(db, index_name)
    if snapshot is None:
        return None
    return lag_for(db, snapshot.built_at)
