"""Embedders: text to vectors, behind one small protocol.

``FastembedEmbedder`` runs BAAI/bge-small-en-v1.5 as ONNX on the CPU through fastembed: 384
dimensions, about 65 MB (quantised ONNX) downloaded once into the model cache. Its identity is
the string ``baai/bge-small-en@v1.5:384``, the namespace of every vector it produces.

``HashingEmbedder`` is a deterministic, offline test double (feature hashing of word unigrams
and bigrams into 384 dimensions). It proves the seams between subsystems without a model
download; it does not retrieve well, and the tests do not pretend it does. Its identity is
``cairn/hashing-v1@1:384``, so its vectors never share an index with a real model's.

Token counting lives here too, because a chunk's token budget is counted with the embedding
model's tokenizer: a different model may cut a document differently under the same
chunker_version. Stage 0 has one model; A9 revisits this when there are several.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol

from cairn_schemas.versions import parse_embedding_model_id

from cairn.config import Settings

# Embedding model id -> fastembed model name. Add a row to support another model.
FASTEMBED_MODELS: dict[str, str] = {
    "baai/bge-small-en@v1.5:384": "BAAI/bge-small-en-v1.5",
    "baai/bge-base-en@v1.5:768": "BAAI/bge-base-en-v1.5",
    "snowflake/arctic-embed-xs@1:384": "snowflake/snowflake-arctic-embed-xs",
}

HASHING_MODEL_ID = "cairn/hashing-v1@1:384"


class Embedder(Protocol):
    model_id: str
    dims: int

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...

    def count_tokens(self, text: str) -> int: ...

    def describe(self) -> str: ...


class FastembedEmbedder:
    def __init__(self, model_id: str, cache_dir: str, *, batch_size: int = 32):
        if model_id not in FASTEMBED_MODELS:
            known = ", ".join(sorted(FASTEMBED_MODELS))
            raise ValueError(f"no fastembed model for {model_id!r}; known: {known}")
        from fastembed import TextEmbedding
        from tokenizers import Tokenizer

        self.model_id = model_id
        self.dims = parse_embedding_model_id(model_id).dims
        self.batch_size = batch_size
        self._model = TextEmbedding(model_name=FASTEMBED_MODELS[model_id], cache_dir=cache_dir)
        # An untruncated copy of the model's tokenizer, so counting a long block gives its real
        # length instead of the model's 512-token ceiling.
        self._counter = Tokenizer.from_str(self._model.model.tokenizer.to_str())
        self._counter.no_truncation()
        self._counter.no_padding()

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [v.tolist() for v in self._model.embed(list(texts), batch_size=self.batch_size)]

    def embed_query(self, text: str) -> list[float]:
        return next(iter(self._model.query_embed(text))).tolist()

    def count_tokens(self, text: str) -> int:
        return len(self._counter.encode(text, add_special_tokens=False).ids)

    def describe(self) -> str:
        return f"fastembed {FASTEMBED_MODELS[self.model_id]} ({self.model_id}), ONNX on CPU"


class HashingEmbedder:
    """Feature hashing: deterministic, offline, free. A seam-tester, not a retriever."""

    model_id = HASHING_MODEL_ID
    dims = 384
    _WORD = re.compile(r"\w+")
    _TOKEN = re.compile(r"\w+|[^\w\s]")

    def _vector(self, text: str) -> list[float]:
        words = self._WORD.findall(text.lower())
        features = words + [f"{a} {b}" for a, b in zip(words, words[1:], strict=False)]
        vector = [0.0] * self.dims
        for feature in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dims
            vector[index] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:
            vector[0] = 1.0
            return vector
        return [v / norm for v in vector]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    def count_tokens(self, text: str) -> int:
        return len(self._TOKEN.findall(text))

    def describe(self) -> str:
        return f"hashing test double ({self.model_id}); proves seams, not retrieval quality"


def embedder_from_settings(settings: Settings) -> Embedder:
    if settings.embedder == "hashing":
        return HashingEmbedder()
    if settings.embedder == "fastembed":
        return FastembedEmbedder(
            settings.embedding_model_id,
            settings.model_cache_dir,
            batch_size=settings.embed_batch_size,
        )
    raise ValueError(f"CAIRN_EMBEDDER must be 'fastembed' or 'hashing', not {settings.embedder!r}")


def effective_model_id(settings: Settings) -> str:
    """The model id the configured embedder will stamp on its vectors."""
    return HASHING_MODEL_ID if settings.embedder == "hashing" else settings.embedding_model_id
