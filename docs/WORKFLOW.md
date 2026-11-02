# Workflow (environment, quality, process)

## Environment

Python 3.15 (`requires-python = "==3.15.*"` — a deliberate pin: the production image runs `PYTHON_VERSION=3.15` and the dev venv is 3.15.0, so the pin guarantees `uv sync --locked` resolves identically everywhere; widening it is a decision to make together with a version bump, not a linter suggestion), package manager and runner — **uv**. The app runs in Docker Compose (`el-loader`); PostgreSQL and Loki are external services.

1. Config: copy `config.example.toml` to `config.toml` and fill in PostgreSQL/IMAP. `.env` is needed by Compose for container variables (Loki, monitoring stack, optionally standalone Postgres/Grafana) and does **not** replace `config.toml`.
2. Dependencies: `just deps` (`uv sync`).
3. Run the CLI in Compose: `just loader --pd --source local` (image build — `just loader-build …`).
4. External services for standalone: `just db-standalone` (Postgres), `just logging-standalone` (Loki + Grafana + Alloy), `just logging` (Alloy only), `just monitoring` (full observability: also Pushgateway + Prometheus + Alertmanager; alert e-mail is rendered from `.env` at container start).
5. Seed PD reference data: `just seed`.

`POSTGRES_HOST` (when set) overrides `common.postgres.host`; `compose.yaml` mounts `./config.toml` to `/app/config.toml`. Stop the environment with `just down-standalone`, full cleanup (with volumes) with `just clean`.

## Quality (what we check before delivery)

| What | Command |
|---|---|
| Lint | `uv run ruff check` |
| Format | `uv run ruff format` |
| Types | `uv run ty check` |
| Tests | `just test` (`uv run pytest -q --cov=src`) |
| Everything at once | `just check` (format + check + ty, then tests) |

- `ruff` with the `ALL` set and explicit exclusions (`pyproject.toml`): Cyrillic is allowed (`RUF001-003`), docstrings are optional (`D1xx`), TODO markers need no author (`TD002/003`, `FIX002`). `line-length = 120`, `target-version = py314`, formatting — LF, double quotes.
- `ty check` — a static type checker (not mypy!). Type errors are a red flag.
- `pytest`: `testpaths = ["tests"]`, `pythonpath = ["src"]`, `filterwarnings = ["error", ...]` — any unexpected warning is a failure (one openpyxl `Unknown extension…` warning is ignored for the `.xlsb` mock workbooks). Tests in `tests/`: parsing/mapping per module (`test_common.py`, `test_quotes_nsi.py`, `test_daily_excel.py`, `test_baiinfo.py`, `test_argus_pdf.py`, `test_argus_fc_exception.py`, `test_sci.py`, `test_sales_op.py`), workbook metadata (`test_workbook_metadata.py`), metrics (`test_metrics.py`), and contract tests (`test_audit_followup.py`, `test_config.py`, `test_db.py`, `test_app_logging.py`, `test_main_contracts.py`). Shared helpers/fixtures live in `tests/conftest.py`: `workbook_metadata` (fixed timestamps), `MOCK_DATA_DIR`, and the `settings` fixture — a `Settings` equal to a minimal `config.toml` (schema `el`, sources `mock_data`, UTC) so the loader tests do not read `config.toml` (CI has none; `test_config.py` is the only place that tests the real `load_settings()`). Parsing/mapping tests use mock data from `mock_data/` and need no database.
- Coverage config lives in `pyproject.toml` (`[tool.coverage.run]`: source `src`, branch mode, `src/scripts/*` excluded; `[tool.coverage.report]`: `fail_under = 40`). `just test` runs pytest with `--cov=src`, so the 40% floor is enforced locally, in CI and in `just check`. The floor is a regression threshold, not a target: the owner set it below the once-proposed 50 so a future module does not fail the suite on arrival. Add tests where behavior warrants them — parsing/config/contracts naturally sit above it, DB-layer code is exercised against real PostgreSQL instead.
- `sales_op` tests read the specification and workbooks from `mock_data/sales_op`.
- Tests must not touch production services. If a test needs a database, use a dedicated database/schema and clear it explicitly.

## Pre-commit (prek)

The root `prek.toml` — ruff format (staged files only, `require_serial`), ruff check `--fix`, uv lock (all via `uv run`) plus builtin hooks: large files, merge-conflict, yaml, eof, trailing whitespace, `mixed-line-ending --fix=lf`, `fix-byte-order-marker`. **`ty check` is `stages = ["manual"]`**: `ty` resolves imports across the whole package, cannot narrow to staged files, and prek stashes the working tree to run such project-wide checks — that breaks parallel agents. It still runs on every change in CI and in `just check`. Install/run on `git commit` (`prek install`). If pre-commit fixed something itself, recheck the diff and commit again.

### Line endings and encoding (LF, UTF-8, no BOM)

Write files as **UTF-8 without BOM**, line endings LF. prek (`mixed-line-ending --fix=lf`, `fix-byte-order-marker`) normalizes this on commit; `.gitattributes` (`* text=auto eol=lf`) does the same at git level, overriding `core.autocrlf`. `ruff.format.line-ending = "lf"`.

## Tests and CI

`.github/workflows/`:

- `ci.yaml` — the single CI entry point (PRs/pushes to `main`/`dev`, manual dispatch, reusable via `workflow_call`).
  - `config` — actionlint plus `docker compose --profile standalone config --quiet` validation.
  - `quality` — `uv sync --locked`, `ruff check`, `ruff format --check`, `ty check`.
  - `tests` — `pytest` with branch coverage and JUnit XML; reports uploaded as artifacts. All current tests are pure unit tests, so no database service is required.
  - `dependencies` — PR-only dependency review (`actions/dependency-review-action`, `fail-on-severity: moderate`); fails a PR that introduces a known-vulnerable package.
  - `secrets` — gitleaks over the **full history** (`fetch-depth: 0`): a deleted secret is still in every clone. Findings are reported, not suppressed; a false positive is fixed in `.gitleaks.toml` with a reason.
  - `production` — build the `prod` image, smoke-check it (`python -m compileall src`), and export it as an artifact on version tags.
  - `required` — a gate job that fails unless every job above succeeded. Configure **CI required** as the required branch-protection check; there are no path filters that could leave it pending.
- `codeql.yaml` — CodeQL security analysis for `python` and `actions` on pushes/PRs to `main`/`dev` plus a weekly scheduled scan (new queries only exist there).
- `docker-release.yaml` — on `vMAJOR.MINOR.PATCH` tags: run `ci.yaml` via `workflow_call`, then publish the exact image that passed CI to GHCR (`ghcr.io/<owner>/excel_loader`, version and full commit SHA tags). Images are not rebuilt after tests.
- `dependabot.yaml` — weekly updates for github-actions, docker, docker-compose, uv (patch versions are ignored).

Keep `just check` and `just test` green locally before pushing.

## Languages (standard)

Language is chosen by audience — a rule, not a wish:

| What | Language | Why |
|---|---|---|
| Identifiers, function/variable names | EN | already the case |
| Code comments and docstrings | EN | code is read by agents; one language beats a mixed one |
| Commit messages | EN | conventional commits; body Why/What/Verified — EN |
| Agent documentation (`AGENTS.md`, `docs/*`) | EN | hot path: re-read on every task, EN costs fewer tokens |
| User-facing strings: log messages, error texts | RU | the product is for Russian users — this is content, **never translate it** |
| Log event names | EN | already the case |

`AGENTS.md` and `docs/*` are maintained in English; log message text is Russian.

## Branching and commits

```
main        ← stable releases; tags vX.Y.Z
dev         ← integration (if used)
feature/*   ← from dev: regular conventional commits (Why/What/Verified)
```

1. **Branches** are short-lived: `fix/<short>`, `feat/<short>` (ascii, lowercase).
2. **Commits** — conventional commits: `type(scope): short summary`. Type: `feat`/`fix`/`refactor`/`docs`/`test`/`chore`; scope — a module (`quotes`, `pd`, `sales-op`, `infra`, `docs`, …). Body — three sections, enough context for an agent without bloat (header ≤ ~72 chars, body 3–10 lines):

```
fix(pd): value_double no longer loses comma decimals

Why: numeric strings like "1 234,5" were parsed as NULL, breaking pd_view.
What: _value_as_double strips spaces and normalizes comma before float().
Verified: ruff, ty, pytest tests/test_common.py; no DB needed.
```

3. **feature → dev/main:** only after green checks and user confirmation; merge — squash, message — a single conventional commit with a Why/What/Verified body.
4. **Don't commit or push without an explicit user request.**

### Checklist before delivery

- [ ] `just check` green (ruff format/check, ty);
- [ ] `just test` green;
- [ ] `AGENTS.md` and the relevant file in `docs/` updated for the new behavior;
- [ ] debug files removed;
- [ ] `git status` and the current branch checked before git operations.

## Working principles

- **Minimal changes:** edit existing files, don't add new entities without need; follow the file's style.
- **Verify after changes:** `just check` and `just test`. Changed parsing/mapping — run the corresponding `tests/test_*.py`; changed DDL/grants — check against a local PostgreSQL (`just db-standalone`).
- **Documentation is part of the change:** changed behavior, mapping or config — update `AGENTS.md` and the relevant file in `docs/`.
- **Git hygiene:** a shared repo, foreign edits are possible; don't revert other people's changes.
- **uv TLS:** verify through the system certificate store (`UV_SYSTEM_CERTS=1` is set in the `justfile`). Don't bring back `UV_INSECURE_HOST`, don't disable SSL globally; the corporate CA is mounted from `/etc/ssl/certs`.
