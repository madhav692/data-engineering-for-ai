# The api image: Python 3.12, uv, the workspace (cairn-schemas + cairn) and the test group, so
# `make test` runs the integration suite inside the container. The embedding model is not baked
# in; it is downloaded on first start into the model-cache volume (/models).
#
# Build context is the repository root (see infra/compose/base.yml). The uv cache is a BuildKit
# cache mount, so it is not baked into the image. The container runs as root; that is fine for a
# local skeleton and is revisited when the image is deployed anywhere (Stage 7).

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CAIRN_MODEL_CACHE=/models \
    HF_HUB_DISABLE_TELEMETRY=1 \
    TOKENIZERS_PARALLELISM=false

WORKDIR /app

# 1. Third-party dependencies only, from the lockfile: this layer is cached until the lock changes.
COPY pyproject.toml uv.lock .python-version ./
COPY packages/schemas/pyproject.toml packages/schemas/README.md packages/schemas/
COPY platform/pyproject.toml platform/README.md platform/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --all-packages --no-dev --group test --no-install-workspace

# 2. The workspace packages themselves (editable, so the bind mount of ./platform is live).
COPY packages packages
COPY platform platform
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --all-packages --no-dev --group test

RUN mkdir -p /models /data

EXPOSE 8000

# `cairn serve` migrates the schema (idempotent) and runs uvicorn on :8000.
CMD ["cairn", "serve"]
