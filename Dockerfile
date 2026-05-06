FROM ghcr.io/astral-sh/uv:python3.14-alpine AS base
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=0 \
    UV_INSECURE_HOST="pypi.org,files.pythonhosted.org" \
    UV_LINK_MODE=copy 

FROM base AS deps
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

FROM python:3.14-alpine AS prod
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

RUN addgroup -S appgroup && adduser -S -G appgroup -D appuser && \
    mkdir -p /app/logs /app/unloaded_data /app/mock_data/pd && \
    chown -R appuser:appgroup /app

COPY --chown=appuser:appgroup --from=deps /app/.venv /app/.venv
COPY --chown=appuser:appgroup src ./src

USER appuser
ENTRYPOINT ["python", "src/main.py"]
