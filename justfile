set dotenv-load := true

[windows]
set shell := ["powershell.exe", "-Command"]

export UV_SYSTEM_CERTS := "1"

# Запуск загрузчика с указанием модуля
loader *args:
    docker compose run --rm loader {{args}}

# То же самое, но с пересборкой
loader-build *args:
    docker compose run --rm --build loader {{args}}

# Запуск тестовой БД в этом же compose
db-stand:
    docker compose up -d postgres

# Запуск Alloy (требует запущенный мониторинговый стек)
monitoring:
    docker compose up -d alloy

# Запуск полного мониторингового стека в этом же compose
monitoring-stand: monitoring
    docker compose up -d loki grafana prometheus pushgateway alertmanager

# Остановка среды
down:
    docker compose down

# Полная очистка: контейнеры и тома
clean:
    docker compose down --volumes --remove-orphans

# Сидирование ТЕСТОВОЙ БД (требует just db-stand, на проде НЕ применять)
seed:
    uv run src/scripts/seed.py

# Установка / обновление зависимостей (uv)
deps:
    uv sync

# Линтинг проекта (ruff)
lint:
    uv run ruff check --fix

# Форматирование кода (ruff)
format:
    uv run ruff format

# Проверка типов (ty)
typecheck:
    uv run ty check --fix

# Тестирование с определением покрытия (pytest)
test:
    uv run pytest -q --cov=src

# Запуск всех проверок и тестов сразу
check: format
    uv run ruff check
    uv run ty check
    just test
