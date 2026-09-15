set windows-shell := ["powershell.exe", "-Command"]
set dotenv-load := true

export UV_SYSTEM_CERTS := "1"

loader *args:
    docker compose run --rm loader {{args}}

loader-build *args:
    docker compose run --rm --build loader {{args}}

logging:
    docker compose up -d alloy

logging-standalone:
    docker compose --profile standalone up -d loki grafana alloy

db-standalone:
    docker compose --profile standalone up -d postgres

down-standalone:
    docker compose --profile standalone down

clean:
    docker compose down --volumes --remove-orphans

seed:
    uv run src/scripts/seed.py

deps:
    uv sync

lint:
    uv run ruff check

format:
    uv run ruff format

typecheck:
    uv run ty check

test:
    uv run pytest -q

check: format
    uv run ruff check
    uv run ty check
    uv run pytest -q
