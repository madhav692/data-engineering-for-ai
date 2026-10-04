"""Generators: a function behind a protocol.

``ExtractiveGenerator`` is the Stage 0 default: it returns the top chunk labelled with its
document and heading, and calls no model. Deterministic, free, offline, which is what makes
replay exact. ``OpenAICompatibleGenerator`` talks to any ``/v1/chat/completions`` endpoint
(Ollama, vLLM, a hosted provider), fills in tokens from the response's usage block and cost from
a small price table, and records the model it was answered by as ``{provider}/{model}@{version}``.
Which one runs is decided by one environment variable (``CAIRN_LLM_BASE_URL``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from cairn_schemas.models import LlmParams

from cairn.config import Settings
from cairn.derivation.chunker import MAX_TOKENS
from cairn.serving.prompts import Context

EXTRACTIVE_MODEL_REF = "cairn/extractive@0"

# USD per million tokens (input, output), as of the stage-0 tag. Prices change; override with
# CAIRN_LLM_PRICE_IN_PER_1M / CAIRN_LLM_PRICE_OUT_PER_1M. Unknown models are recorded at 0 and
# logged, because an unpriced row is better than a guessed one (A33 builds the real ledger).
PRICE_TABLE: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-5": (3.00, 15.00),
}

_REF_PART = re.compile(r"[^A-Za-z0-9._-]+")
_PROVIDER_PART = re.compile(r"[^a-z0-9._-]+")


@dataclass(frozen=True)
class Generation:
    text: str
    model_ref: str  # provider/name@version
    params: LlmParams
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0


class Generator(Protocol):
    model_ref: str
    default_params: LlmParams

    def generate(self, question: str, context: Context) -> Generation: ...

    def describe(self) -> str: ...


class ExtractiveGenerator:
    """Stage 0 default: no model call. Deterministic, free, offline.

    Its parameters are configuration, not per-request data (contract C8 compares them across
    requests): temperature 0, and an answer of at most one chunk, so at most ``MAX_TOKENS``.
    """

    model_ref = EXTRACTIVE_MODEL_REF
    default_params = LlmParams(temperature=0.0, max_tokens=MAX_TOKENS)

    def generate(self, question: str, context: Context) -> Generation:
        if not context.chunks:
            text = "No passage in the index matched the question."
            return Generation(text, self.model_ref, self.default_params)
        top = context.chunks[0]
        label = f"{top.title} › {top.heading_path}" if top.heading_path else top.title
        text = f"From {label}:\n\n{top.text}"
        return Generation(text, self.model_ref, self.default_params, 0, 0, 0.0)

    def describe(self) -> str:
        return f"extractive ({self.model_ref}): returns the top chunk, calls no model"


def model_ref_for(provider: str, model: str, version: str) -> str:
    """Spell a model reference so it satisfies ``ModelRef`` (``provider/name@version``)."""
    provider_part = _PROVIDER_PART.sub("-", provider.lower()).strip("-") or "llm"
    name_part = _REF_PART.sub("-", model).strip("-") or "model"
    version_part = _REF_PART.sub("-", version).strip("-") or "latest"
    return f"{provider_part}/{name_part}@{version_part}"


def price_for(model: str, settings: Settings) -> tuple[float, float] | None:
    if settings.llm_price_in_per_1m is not None or settings.llm_price_out_per_1m is not None:
        return (settings.llm_price_in_per_1m or 0.0, settings.llm_price_out_per_1m or 0.0)
    for prefix, prices in sorted(PRICE_TABLE.items(), key=lambda kv: -len(kv[0])):
        if model.startswith(prefix):
            return prices
    return None


class OpenAICompatibleGenerator:
    """Any /v1/chat/completions endpoint: Ollama, vLLM or a hosted provider."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        provider: str,
        api_key: str | None,
        settings: Settings,
        version: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.provider = provider
        self.api_key = api_key
        self.settings = settings
        self.version = version
        self.default_params = LlmParams(temperature=0.0, max_tokens=settings.llm_max_tokens)
        self.model_ref = model_ref_for(provider, model, version or "latest")
        self.prices = price_for(model, settings)

    def generate(self, question: str, context: Context) -> Generation:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": context.prompt}],
            "temperature": self.default_params.temperature,
            "max_tokens": self.default_params.max_tokens,
        }
        response = httpx.post(
            f"{self.base_url}/chat/completions",
            json=body,
            headers=headers,
            timeout=self.settings.llm_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        text = (payload["choices"][0]["message"].get("content") or "").strip()
        usage = payload.get("usage") or {}
        tokens_in = int(usage.get("prompt_tokens") or 0)
        tokens_out = int(usage.get("completion_tokens") or 0)
        reported = str(payload.get("model") or self.model)
        # The version is what the endpoint reports when it adds one (gpt-4o-mini-2024-07-18,
        # llama3.2:3b); otherwise the configured version or "latest".
        version = self.version or (reported if reported != self.model else "latest")
        if self.prices is None:
            cost = 0.0
        else:
            cost = tokens_in * self.prices[0] / 1e6 + tokens_out * self.prices[1] / 1e6
        return Generation(
            text=text,
            model_ref=model_ref_for(self.provider, self.model, version),
            params=self.default_params,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=round(cost, 8),
        )

    def describe(self) -> str:
        priced = "priced" if self.prices else "unpriced (cost recorded as 0)"
        return f"openai-compatible {self.model} at {self.base_url} ({self.model_ref}), {priced}"


def generator_from_env(settings: Settings) -> Generator:
    if settings.llm_base_url:
        return OpenAICompatibleGenerator(
            base_url=settings.llm_base_url,
            model=settings.llm_model or "default",
            provider=settings.llm_provider,
            api_key=settings.llm_api_key,
            settings=settings,
            version=settings.llm_model_version,
        )
    return ExtractiveGenerator()
