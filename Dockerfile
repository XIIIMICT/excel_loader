ARG PYTHON_VERSION=3.15
FROM ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-alpine AS base
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy

FROM base AS deps
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project \
    --allow-insecure-host pypi.org \
    --allow-insecure-host files.pythonhosted.org

FROM python:${PYTHON_VERSION}-alpine AS prod
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# The container runs as root on purpose (a deliberate deviation from the
# non-root default): the loader renames processed files in the bind-mounted
# ./data, and a fixed non-root uid turned those renames into permission errors
# on Linux. See docs/ARCHITECTURE.md.
RUN mkdir -p /app/logs /app/unloaded_data /app/mock_data/pd

COPY --from=deps /app/.venv /app/.venv
COPY src ./src
COPY sql ./sql

ENTRYPOINT ["python", "src/main.py"]
