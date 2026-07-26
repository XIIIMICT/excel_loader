set windows-shell := ["powershell.exe", "-Command"]

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
    uv --system-certs sync
