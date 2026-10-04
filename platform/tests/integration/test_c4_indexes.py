"""Contract C4: Lakehouse -> Indexes. An index says what it was built from, and its lag is a
number computed from the data."""

from __future__ import annotations

import time

from cairn_schemas import ids

from cairn.indexes.build import rollback, table_exists, table_name_for
from cairn.indexes.reconcile import index_lag_seconds, live_snapshot
from support import POLICY_EDIT, SOURCE_ID, Platform


def test_build_writes_snapshot_and_lag_is_zero(platform: Platform):
    """An IndexSnapshot row with status = live; `cairn lag` reads 0."""
    platform.ingest()
    result = platform.build()
    snap = result.snapshot
    index_name = ids.index_name_for("chunks", platform.embedder.model_id)

    assert snap.status == "live"
    assert snap.index_name == index_name
    assert snap.embedding_model_id == platform.embedder.model_id
    assert snap.row_count == platform.count("chunks")
    assert snap.lag_seconds_at_build == 0
    assert snap.index_snapshot_id == ids.index_snapshot_id(
        index_name, platform.embedder.model_id, snap.lakehouse_snapshot_id
    )
    assert live_snapshot(platform.db, index_name) == snap
    assert platform.count("index_snapshots", "status = 'live'") == 1
    assert index_lag_seconds(platform.db, index_name) == {SOURCE_ID: 0}
    with platform.db.transaction() as tx:
        assert table_exists(tx, table_name_for(snap.index_snapshot_id, index_name))


def test_ingest_after_build_raises_lag(platform: Platform):
    """Lag is positive until the next build, then zero; the pointer moves and can move back."""
    platform.ingest()
    first = platform.build()
    index_name = first.snapshot.index_name
    assert index_lag_seconds(platform.db, index_name) == {SOURCE_ID: 0}

    # Pretend the build happened two minutes ago (instead of sleeping), then edit a file now:
    # the connector reads the file's mtime as source_updated_at.
    platform.db.execute(
        "UPDATE index_snapshots SET built_at = built_at - interval '120 seconds' "
        "WHERE index_snapshot_id = %s",
        (first.snapshot.index_snapshot_id,),
    )
    platform.edit("policy.md", *POLICY_EDIT, mtime=time.time())
    platform.ingest()
    lag = index_lag_seconds(platform.db, index_name)
    assert lag is not None and 100 <= lag[SOURCE_ID] <= 140

    second = platform.build()
    assert second.snapshot.index_snapshot_id != first.snapshot.index_snapshot_id
    assert index_lag_seconds(platform.db, index_name) == {SOURCE_ID: 0}
    assert second.snapshot.lag_seconds_at_build == 0
    statuses = {
        r["index_snapshot_id"]: r["status"]
        for r in platform.db.rows("SELECT index_snapshot_id, status FROM index_snapshots")
    }
    assert statuses == {
        first.snapshot.index_snapshot_id: "retired",
        second.snapshot.index_snapshot_id: "live",
    }
    # the retired table is retained (CAIRN_INDEX_RETAIN), so the pointer can flip back
    restored = rollback(platform.db, index_name)
    assert restored.index_snapshot_id == first.snapshot.index_snapshot_id
    assert (
        live_snapshot(platform.db, index_name).index_snapshot_id == first.snapshot.index_snapshot_id
    )
