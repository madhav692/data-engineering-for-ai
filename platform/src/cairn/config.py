"""Settings: every knob is a ``CAIRN_*`` environment variable with a Compose-friendly default.

The defaults are the values inside the ``api`` container of ``infra/compose/base.yml``. The
versions here (prompt template, retrieval configuration, embedding model) are lineage: every
one of them is written into every ``Request`` row.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields, replace
from typing import Any, Self

DEFAULT_EMBEDDING_MODEL_ID = "baai/bge-small-en@v1.5:384"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    return int(value) if value is not None else default


def _env_float(name: str, default: float) -> float:
    value = _env(name)
    return float(value) if value is not None else default


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Lakehouse stand-in (ADR-0003)
    database_url: str = "postgresql://cairn:cairn@postgres:5432/cairn"
    # Raw bytes: "s3" (any S3 API: SeaweedFS in compose, AWS, ...) or "fs" (a directory)
    object_store: str = "s3"
    s3_endpoint: str = "http://seaweedfs:8333"
    s3_bucket: str = "cairn"
    s3_access_key: str = "cairn"
    s3_secret_key: str = "cairn-local"
    fs_store_dir: str = "/data/objects"
    # Single tenant until A22
    tenant_id: str = "local"
    # Derivation: "fastembed" (BAAI/bge-small-en-v1.5 on the CPU) or "hashing" (a deterministic,
    # offline test double; it proves the seams, not retrieval quality)
    embedder: str = "fastembed"
    embedding_model_id: str = DEFAULT_EMBEDDING_MODEL_ID
    model_cache_dir: str = "/models"
    embed_batch_size: int = 32
    # Indexes
    index_base_name: str = "chunks"
    index_retain: int = 2  # retired snapshots whose tables are kept, for replay and rollback
    # Serving: versions that are lineage
    prompt_template_version: str = "answer-v1"
    retrieval_config_version: str = "vector-k5-v1"
    retrieval_k: int = 5
    # Generation: unset means the extractive generator (no model call)
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None
    llm_provider: str = "openai-compatible"
    llm_model_version: str | None = None  # defaults to the model name the endpoint reports
    llm_max_tokens: int = 512
    llm_price_in_per_1m: float | None = None  # USD per million input tokens; None = price table
    llm_price_out_per_1m: float | None = None
    llm_timeout_seconds: float = 120.0
    allow_generator_override: bool = True  # /ask may name a generator per request (A17 removes)
    # Where the CLI finds the api, and where it finds the golden sets
    api_url: str = "http://localhost:8000"
    golden_dir: str = "/data/datasets/golden"
    git_sha: str | None = None
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Self:
        defaults = cls()
        return cls(
            database_url=_env("CAIRN_DATABASE_URL", defaults.database_url),
            object_store=_env("CAIRN_OBJECT_STORE", defaults.object_store),
            s3_endpoint=_env("CAIRN_S3_ENDPOINT", defaults.s3_endpoint),
            s3_bucket=_env("CAIRN_S3_BUCKET", defaults.s3_bucket),
            s3_access_key=_env("CAIRN_S3_ACCESS_KEY", defaults.s3_access_key),
            s3_secret_key=_env("CAIRN_S3_SECRET_KEY", defaults.s3_secret_key),
            fs_store_dir=_env("CAIRN_FS_STORE_DIR", defaults.fs_store_dir),
            tenant_id=_env("CAIRN_TENANT_ID", defaults.tenant_id),
            embedder=_env("CAIRN_EMBEDDER", defaults.embedder),
            embedding_model_id=_env("CAIRN_EMBEDDING_MODEL_ID", defaults.embedding_model_id),
            model_cache_dir=_env("CAIRN_MODEL_CACHE", defaults.model_cache_dir),
            embed_batch_size=_env_int("CAIRN_EMBED_BATCH_SIZE", defaults.embed_batch_size),
            index_base_name=_env("CAIRN_INDEX_BASE_NAME", defaults.index_base_name),
            index_retain=_env_int("CAIRN_INDEX_RETAIN", defaults.index_retain),
            prompt_template_version=_env(
                "CAIRN_PROMPT_TEMPLATE_VERSION", defaults.prompt_template_version
            ),
            retrieval_config_version=_env(
                "CAIRN_RETRIEVAL_CONFIG_VERSION", defaults.retrieval_config_version
            ),
            retrieval_k=_env_int("CAIRN_RETRIEVAL_K", defaults.retrieval_k),
            llm_base_url=_env("CAIRN_LLM_BASE_URL"),
            llm_model=_env("CAIRN_LLM_MODEL"),
            llm_api_key=_env("CAIRN_LLM_API_KEY"),
            llm_provider=_env("CAIRN_LLM_PROVIDER", defaults.llm_provider),
            llm_model_version=_env("CAIRN_LLM_MODEL_VERSION"),
            llm_max_tokens=_env_int("CAIRN_LLM_MAX_TOKENS", defaults.llm_max_tokens),
            llm_price_in_per_1m=(
                _env_float("CAIRN_LLM_PRICE_IN_PER_1M", 0.0)
                if _env("CAIRN_LLM_PRICE_IN_PER_1M")
                else None
            ),
            llm_price_out_per_1m=(
                _env_float("CAIRN_LLM_PRICE_OUT_PER_1M", 0.0)
                if _env("CAIRN_LLM_PRICE_OUT_PER_1M")
                else None
            ),
            llm_timeout_seconds=_env_float(
                "CAIRN_LLM_TIMEOUT_SECONDS", defaults.llm_timeout_seconds
            ),
            allow_generator_override=_env_bool(
                "CAIRN_ALLOW_GENERATOR_OVERRIDE", defaults.allow_generator_override
            ),
            api_url=_env("CAIRN_API_URL", defaults.api_url),
            golden_dir=_env("CAIRN_GOLDEN_DIR", defaults.golden_dir),
            git_sha=_env("CAIRN_GIT_SHA"),
            log_level=_env("CAIRN_LOG_LEVEL", defaults.log_level),
        )

    def with_(self, **changes: Any) -> Self:
        """A copy with some fields replaced (tests use this)."""
        return replace(self, **changes)

    def lineage_versions(self) -> dict[str, str]:
        """The versions every Stage 0 request carries, for logs and the /healthz reply."""
        return {
            "embedding_model_id": self.embedding_model_id,
            "prompt_template_version": self.prompt_template_version,
            "retrieval_config_version": self.retrieval_config_version,
        }

    def redacted(self) -> dict[str, Any]:
        out = {f.name: getattr(self, f.name) for f in fields(self)}
        for secret in ("s3_secret_key", "llm_api_key"):
            if out.get(secret):
                out[secret] = "***"
        if "@" in out["database_url"]:
            creds, rest = out["database_url"].split("@", 1)
            scheme = creds.split("//", 1)[0]
            out["database_url"] = f"{scheme}//***@{rest}"
        return out
