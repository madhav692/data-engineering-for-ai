"""Identifier derivations.

Identity is derived from content and versions wherever it can be, so that re-running a
step is a no-op and retries are safe. Only identifiers minted at the edge of the system
(requests, feedback, jobs, runs) are random, and those are time-ordered so the hot
request log stays append-friendly.

ID conventions (see docs/architecture/reference.md):

- ``doc_id``            identity of a document: hash of (source_id, canonical uri)
- ``content_sha256``    version of a document: hash of its raw bytes
- ``chunk_id``          hash of (doc_id, parser_version, chunker_version, text_sha256, occurrence);
                        an unchanged chunk keeps its id across document revisions and
                        across position shifts, so only changed chunks are re-embedded
- ``index_snapshot_id`` hash of (index_name, embedding_model_id, lakehouse_snapshot_id)
- ``request_id`` etc.   opaque, time-ordered (UUIDv7), minted when the request arrives
"""

from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

# ASCII unit separator: cannot appear in any identifier component, so joins are unambiguous.
_SEP = "\x1f"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def sha256_hex(data: bytes | str) -> str:
    """Hex SHA-256 of bytes (or of a string's UTF-8 encoding)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _digest(prefix: str, *parts: object) -> str:
    return f"{prefix}_{sha256_hex(_SEP.join(str(p) for p in parts))[:32]}"


def canonical_uri(uri: str) -> str:
    """A stable spelling of a URI for identity purposes.

    Lower-cases the scheme and host, drops the fragment and a default port, and strips
    one trailing slash from a non-root path. Query strings are kept: they can select
    different documents.
    """
    parts = urlsplit(uri.strip())
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        netloc = netloc.rsplit(":", 1)[0]
    path = parts.path or ("/" if netloc else "")
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def doc_id(source_id: str, uri: str) -> str:
    """Stable identity of a document: the same source and location always give the same id."""
    return _digest("doc", source_id, canonical_uri(uri))


def raw_object_key(source_id: str, content_sha256: str) -> str:
    """Where the raw bytes of a document version live in the lakehouse object store."""
    return f"raw/{source_id}/{content_sha256[:2]}/{content_sha256}"


def chunk_id(
    doc_id: str,
    parser_version: str,
    chunker_version: str,
    text_sha256: str,
    occurrence: int = 0,
) -> str:
    """Content-addressed chunk identity.

    Deliberately excludes the document's ``content_sha256``, ordinal and byte offsets:
    a chunk whose text did not change keeps its id when the document around it changes,
    which is what makes "only re-embed what changed" possible. ``occurrence`` keeps
    repeated identical texts within one document distinct.
    """
    return _digest("chk", doc_id, parser_version, chunker_version, text_sha256, occurrence)


def index_snapshot_id(index_name: str, embedding_model_id: str, lakehouse_snapshot_id: str) -> str:
    """An index build is a function of one lakehouse snapshot and one model, and says which."""
    return _digest("idx", index_name, embedding_model_id, lakehouse_snapshot_id)


def model_slug(model_id: str) -> str:
    """A filename-safe spelling of a model id.

    ``openai/text-embedding-3-small@2024-01:1536`` -> ``openai-text-embedding-3-small-2024-01-1536``
    """
    return _SLUG_RE.sub("-", model_id.lower()).strip("-")


def index_name_for(base: str, embedding_model_id: str) -> str:
    """Versions are namespaces: the embedding model is part of the index name (ADR-0001)."""
    return f"{base}--{model_slug(embedding_model_id)}"


def uuid7() -> uuid.UUID:
    """A time-ordered UUID (RFC 9562 version 7): 48-bit Unix milliseconds, then randomness."""
    unix_ms = time.time_ns() // 1_000_000
    rand_a = int.from_bytes(os.urandom(2)) & 0x0FFF
    rand_b = int.from_bytes(os.urandom(8)) & 0x3FFF_FFFF_FFFF_FFFF
    value = (unix_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return uuid.UUID(int=value)


def new_id(prefix: str) -> str:
    """A minted, time-ordered identifier with a type prefix, e.g. ``req_018f...``."""
    return f"{prefix}_{uuid7().hex}"


def new_request_id() -> str:
    return new_id("req")


def new_feedback_id() -> str:
    return new_id("fb")


def new_run_id() -> str:
    return new_id("run")


def new_job_id() -> str:
    return new_id("job")
