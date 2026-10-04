"""Test fixtures: a real Postgres, a fresh schema per test, a small corpus, the whole platform.

Where the database comes from, in order:

1. ``CAIRN_TEST_DATABASE_URL`` if set (any reachable Postgres with pgvector).
2. ``CAIRN_DATABASE_URL`` if reachable (inside the api container, this is the compose Postgres).
3. An embedded Postgres from ``pgserver`` (the ``dev`` dependency group; no Docker needed).

In cases 1 and 2 the tests never touch the configured database: they create and use a database
named ``<dbname>_test`` beside it. Every test starts from an empty schema.

The embedder is the hashing test double unless ``CAIRN_TEST_EMBEDDER=fastembed``. The object
store is a temporary directory unless ``CAIRN_TEST_S3_ENDPOINT`` names an S3 endpoint (inside the
api container, ``make test`` points it at the compose SeaweedFS): then one bucket, ``cairn-test``
by default (``CAIRN_TEST_S3_BUCKET``), is emptied before every test. One bucket rather than one
per test because SeaweedFS backs each bucket with its own set of volume files. Both are the thin
end of a seam the tests exercise either way.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from cairn.config import Settings
from cairn.derivation.embedder import Embedder, embedder_from_settings
from cairn.lakehouse.db import Db
from cairn.lakehouse.migrate import migrate
from cairn.lakehouse.objects import FsObjectStore, ObjectStore, S3ObjectStore
from support import CORPUS, Platform, _reachable, _test_url_for


@pytest.fixture(scope="session")
def database_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    explicit = os.environ.get("CAIRN_TEST_DATABASE_URL")
    if explicit:
        yield _test_url_for(explicit)
        return
    configured = os.environ.get("CAIRN_DATABASE_URL")
    if configured and _reachable(configured):
        yield _test_url_for(configured)
        return
    pgserver = pytest.importorskip(
        "pgserver",
        reason="no Postgres: set CAIRN_TEST_DATABASE_URL or install the dev group (pgserver)",
    )
    server = pgserver.get_server(tmp_path_factory.mktemp("pg"))
    try:
        yield server.get_uri()
    finally:
        server.cleanup()


@pytest.fixture(scope="session")
def embedder() -> Embedder:
    kind = os.environ.get("CAIRN_TEST_EMBEDDER", "hashing")
    cache = os.environ.get("CAIRN_MODEL_CACHE") or str(Path.home() / ".cache" / "cairn-models")
    return embedder_from_settings(Settings().with_(embedder=kind, model_cache_dir=cache))


@pytest.fixture(scope="session")
def shared_db(database_url: str) -> Iterator[Db]:
    db = Db(database_url, max_size=4)
    yield db
    db.close()


@pytest.fixture
def object_store(tmp_path: Path) -> Iterator[tuple[ObjectStore, Settings]]:
    """An empty store per test: a temporary directory, or an emptied bucket on a real S3."""
    endpoint = os.environ.get("CAIRN_TEST_S3_ENDPOINT")
    if not endpoint:
        settings = Settings(object_store="fs", fs_store_dir=str(tmp_path / "objects"))
        yield FsObjectStore(settings.fs_store_dir), settings
        return
    bucket = os.environ.get("CAIRN_TEST_S3_BUCKET", "cairn-test")
    if bucket == os.environ.get("CAIRN_S3_BUCKET", "cairn"):
        raise RuntimeError("CAIRN_TEST_S3_BUCKET must not be the bucket the platform ingests into")
    settings = Settings(
        object_store="s3",
        s3_endpoint=endpoint,
        s3_bucket=bucket,
        s3_access_key=os.environ.get("CAIRN_S3_ACCESS_KEY", "cairn"),
        s3_secret_key=os.environ.get("CAIRN_S3_SECRET_KEY", "cairn-local"),
    )
    store = S3ObjectStore(endpoint, bucket, settings.s3_access_key, settings.s3_secret_key)
    store.ensure_bucket()
    store.clear()
    yield store, settings


@pytest.fixture
def platform(
    shared_db: Db,
    embedder: Embedder,
    object_store: tuple[ObjectStore, Settings],
    tmp_path: Path,
) -> Iterator[Platform]:
    migrate(shared_db, reset=True)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for name, text in CORPUS.items():
        (corpus / name).write_text(text, encoding="utf-8")
    store, store_settings = object_store
    settings = store_settings.with_(
        database_url=shared_db.url,
        embedder=os.environ.get("CAIRN_TEST_EMBEDDER", "hashing"),
        golden_dir=str(tmp_path),
        git_sha="deadbeef",
    )
    p = Platform(settings=settings, db=shared_db, store=store, embedder=embedder, corpus=corpus)
    try:
        yield p
    finally:
        p.close()


@pytest.fixture
def indexed(platform: Platform) -> Platform:
    """The platform after ``ingest`` and ``build-index``: ready to answer."""
    platform.ingest()
    platform.build()
    return platform
