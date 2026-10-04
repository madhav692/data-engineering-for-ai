"""The raw bytes of every document version, keyed by content hash.

Keys are ``raw/{source_id}/{sha[:2]}/{sha}`` (``cairn_schemas.ids.raw_object_key``), so a
version is written once no matter how many times it is fetched, and every rebuild starts
from here. ``S3ObjectStore`` talks to any S3 API (SeaweedFS in compose, MinIO, AWS) through the
``minio`` client library, which is what Stage 1's Iceberg tables will use too; ``FsObjectStore``
is a directory, for tests and Docker-free runs.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from cairn.config import Settings


class ObjectStore(Protocol):
    def put(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> None: ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def count(self, prefix: str = "raw/") -> int: ...

    def describe(self) -> str: ...


class FsObjectStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError(f"object key escapes the store root: {key!r}")
        return path

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)  # atomic: a reader never sees a half-written object

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def count(self, prefix: str = "raw/") -> int:
        base = self.root / prefix
        if not base.exists():
            return 0
        return sum(1 for p in base.rglob("*") if p.is_file() and not p.name.endswith(".tmp"))

    def describe(self) -> str:
        return f"fs://{self.root}"


class S3ObjectStore:
    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str):
        from minio import Minio  # imported here so the fs store never needs it

        parts = urlsplit(endpoint)
        self._client = Minio(
            parts.netloc,
            access_key=access_key,
            secret_key=secret_key,
            secure=parts.scheme == "https",
        )
        self.endpoint = endpoint
        self.bucket = bucket

    def ensure_bucket(self) -> None:
        if not self._client.bucket_exists(self.bucket):
            self._client.make_bucket(self.bucket)

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self._client.put_object(
            self.bucket, key, io.BytesIO(data), length=len(data), content_type=content_type
        )

    def get(self, key: str) -> bytes:
        response = self._client.get_object(self.bucket, key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def exists(self, key: str) -> bool:
        from minio.error import S3Error

        try:
            self._client.stat_object(self.bucket, key)
            return True
        except S3Error as err:
            if err.code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
                return False
            raise

    def count(self, prefix: str = "raw/") -> int:
        return sum(1 for _ in self._client.list_objects(self.bucket, prefix=prefix, recursive=True))

    def clear(self) -> int:
        """Delete every object in the bucket (the tests use this; nothing else should)."""
        from minio.deleteobjects import DeleteObject

        objects = [
            DeleteObject(o.object_name)
            for o in self._client.list_objects(self.bucket, recursive=True)
        ]
        if objects:
            for error in self._client.remove_objects(self.bucket, objects):
                raise RuntimeError(f"could not delete {error.object_name}: {error.message}")
        return len(objects)

    def remove_bucket(self) -> None:
        """Delete every object, then the bucket."""
        self.clear()
        self._client.remove_bucket(self.bucket)

    def describe(self) -> str:
        return f"{self.endpoint}/{self.bucket}"


def object_store_from_settings(settings: Settings) -> ObjectStore:
    if settings.object_store == "fs":
        return FsObjectStore(settings.fs_store_dir)
    if settings.object_store == "s3":
        store = S3ObjectStore(
            settings.s3_endpoint,
            settings.s3_bucket,
            settings.s3_access_key,
            settings.s3_secret_key,
        )
        store.ensure_bucket()
        return store
    raise ValueError(f"CAIRN_OBJECT_STORE must be 's3' or 'fs', not {settings.object_store!r}")
