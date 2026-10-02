"""cairn-schemas: the data contracts of the Cairn platform.

Seven Pydantic models are the single source of truth; ``cairn_schemas.generate`` emits
Avro (for Kafka), Iceberg DDL (for the lakehouse) and JSON Schema (for the API) from them.
"""

from cairn_schemas.base import Costed, Row
from cairn_schemas.models import (
    ALL_MODELS,
    Chunk,
    Document,
    Embedding,
    EvalConfig,
    EvalRun,
    Feedback,
    IndexSnapshot,
    LlmParams,
    Request,
    RetrievedChunk,
    StageLatency,
)
from cairn_schemas.versions import (
    EmbeddingModelId,
    ModelId,
    ModelRef,
    Sha256,
    VersionTag,
    parse_embedding_model_id,
)

__all__ = [
    "ALL_MODELS",
    "Chunk",
    "Costed",
    "Document",
    "Embedding",
    "EmbeddingModelId",
    "EvalConfig",
    "EvalRun",
    "Feedback",
    "IndexSnapshot",
    "LlmParams",
    "ModelId",
    "ModelRef",
    "Request",
    "RetrievedChunk",
    "Row",
    "Sha256",
    "StageLatency",
    "VersionTag",
    "parse_embedding_model_id",
]

__version__ = "0.1.0"
