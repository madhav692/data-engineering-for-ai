"""Version and identifier value types shared by the models.

Every derived row names the version of the thing that made it. These types make the
spelling of those versions uniform so lineage joins never fail on formatting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated

from pydantic import Field

# provider/name@version:dims  e.g. openai/text-embedding-3-small@2024-01:1536
EMBEDDING_MODEL_ID_PATTERN = (
    r"^[a-z0-9][a-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*@[A-Za-z0-9][A-Za-z0-9._-]*:[1-9][0-9]*$"
)
# provider/name@version  e.g. anthropic/claude-sonnet-4-5@2025-09-29, cohere/rerank-v3.5@2024-12
MODEL_REF_PATTERN = r"^[a-z0-9][a-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*@[A-Za-z0-9][A-Za-z0-9._-]*$"
# A short, filename-safe version tag for parsers, chunkers, prompt templates, retrieval configs.
VERSION_TAG_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"

_EMBEDDING_MODEL_ID_RE = re.compile(EMBEDDING_MODEL_ID_PATTERN)

EmbeddingModelId = Annotated[
    str,
    Field(
        pattern=EMBEDDING_MODEL_ID_PATTERN,
        description=(
            "Embedding model as provider/name@version:dims. The model is the namespace of "
            "every vector it produced; two models never share an index."
        ),
        examples=["openai/text-embedding-3-small@2024-01:1536", "baai/bge-base-en@1.5:768"],
    ),
]

ModelRef = Annotated[
    str,
    Field(
        pattern=MODEL_REF_PATTERN,
        description="A model reference as provider/name@version (LLMs, rerankers, judges).",
        examples=["anthropic/claude-sonnet-4-5@2025-09-29", "cohere/rerank-v3.5@2024-12"],
    ),
]

VersionTag = Annotated[
    str,
    Field(
        pattern=VERSION_TAG_PATTERN,
        description="A version tag: letters, digits, dot, underscore, dash; 1-64 characters.",
        examples=["pymupdf-1.24.9", "struct-v2", "2026-10-02.1"],
    ),
]

Sha256 = Annotated[
    str,
    Field(pattern=SHA256_PATTERN, description="Lower-case hex SHA-256 digest."),
]


@dataclass(frozen=True)
class ModelId:
    provider: str
    name: str
    version: str
    dims: int

    def __str__(self) -> str:
        return f"{self.provider}/{self.name}@{self.version}:{self.dims}"


def parse_embedding_model_id(value: str) -> ModelId:
    """Split ``provider/name@version:dims`` into its parts, or raise ``ValueError``."""
    if not _EMBEDDING_MODEL_ID_RE.match(value):
        raise ValueError(f"not an embedding model id (provider/name@version:dims): {value!r}")
    provider, rest = value.split("/", 1)
    name, rest = rest.split("@", 1)
    version, dims = rest.rsplit(":", 1)
    return ModelId(provider=provider, name=name, version=version, dims=int(dims))
