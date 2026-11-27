# Workflow (environment, quality, process)

## Environment

Python 3.15 (`requires-python = "==3.15.*"` — a deliberate pin: the production image runs `PYTHON_VERSION=3.15` and the dev venv is 3.15.0, so the pin guarantees `uv sync --locked` resolves identically everywhere; widening it is a decision to make together with a version bump, not a linter suggestion), package manager and runner — **uv**. The app runs in Docker Compose (`el-loader`); PostgreSQL and Loki are external services.

1. Config: copy `config.example.toml` to `config.toml` and fill in PostgreSQL/IMAP. `.env` is needed by Compose for container variables (Loki, monitoring stack, optionally Postgres) and does **not** replace `config.toml`.
2. Dependencies: `just deps` (`uv sync`).
3. Run the CLI in Compose: `just loader --pd --source local` (image build — `just loader-build …`).
4. External services for local: `just db-stand` (Postgres), `just monitoring-stand` (Loki + Grafana + Alloy + Pushgateway + Prometheus + Alertmanager; alert e-mail is rendered from `.env` at container start).
5. Seed PD reference data: `just seed`.

`POSTGRES_HOST` (when set) overrides `common.postgres.host`; `compose.yaml` mounts `./config.toml` to `/app/config.toml`. Stop the environment with `just down`, full cleanup (with volumes) with `just clean`.

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
- `pytest`: `testpaths = ["tests"]`, `pythonpath = ["src"]`, `filterwarnings = ["error", ...]` — any unexpected warning is a failure (one openpyxl `Unknown extension…` warning is ignored for the `.xlsb` mock workbooks). The `integration` marker is registered in `pyproject.toml`; the default unit run excludes it (`-m "not integration"`). Tests in `tests/`: parsing/mapping per module (`test_common.py`, `test_quotes_nsi.py`, `test_daily_excel.py`, `test_baiinfo.py`, `test_argus_pdf.py`, `test_argus_fc_exception.py`, `test_sci.py`, `test_sales_op.py`), the PD parser and deferred packages (`test_pd_excel_parser.py`, `test_pd_unsent.py`), local source resolution (`test_source_resolver.py`), pure sales_op runner edges (`test_sales_op_runner_edges.py`), workbook metadata (`test_workbook_metadata.py`), metrics (`test_metrics.py`), contract tests (`test_audit_followup.py`, `test_config.py`, `test_db.py`, `test_app_logging.py`, `test_main_contracts.py`), and the PostgreSQL smoke test (`test_postgres_smoke.py`, marker `integration`). Shared helpers/fixtures live in `tests/conftest.py`: `workbook_metadata` (fixed timestamps), `MOCK_DATA_DIR`, and the `settings` fixture — a `Settings` equal to a minimal `config.toml` (schema `el`, sources `mock_data`, UTC) so the loader tests do not read `config.toml` (CI has none; `test_config.py` is the only place that tests the real `load_settings()`). Parsing/mapping tests use mock data from `mock_data/` and need no database.
- Coverage config lives in `pyproject.toml` (`[tool.coverage.run]`: source `src`, branch mode, `src/scripts/*` excluded; `[tool.coverage.report]`: `fail_under = 65`). `just test` runs pytest with `--cov=src`, so the floor is enforced locally, in CI and in `just check`. The floor is a regression threshold, not a target. **Decision (P0-4, 2026-11-27): do NOT add `--cov-fail-under=80` and do NOT chase a percentage.** Research measured 57.70% before the new tests and 67.76% after (`uv run --no-sync pytest -q -m "not integration" --cov=src --cov-branch --cov-report=term`); the valuable blind spot was the untested PD parser (`src/pd/excel_parser.py` went 15% → 93%, `src/pd/unsent.py` 27% → 100%), while DB/SQL code is exercised by the real-PostgreSQL smoke job below. `fail_under` was ratcheted 40 → 65, leaving deliberate headroom for a future module. The single `fail_under` in `pyproject.toml` is the gate — there is no duplicate `--cov-fail-under` flag in CI and never an 80 threshold.
- `sales_op` tests read the specification and workbooks from `mock_data/sales_op`.
- Tests must not touch production services. If a test needs a database, use a dedicated database/schema and clear it explicitly.

## Pre-commit (prek)

The root `prek.toml` — ruff format (staged files only, `require_serial`), ruff check `--fix`, uv lock (all via `uv run`) plus builtin hooks: large files, merge-conflict, yaml, eof, trailing whitespace, `mixed-line-ending --fix=lf`, `fix-byte-order-marker`. **`ty check` is `stages = ["manual"]`**: `ty` resolves imports across the whole package, cannot narrow to staged files, and prek stashes the working tree to run such project-wide checks — that breaks parallel agents. It still runs on every change in CI and in `just check`. Install/run on `git commit` (`prek install`). If pre-commit fixed something itself, recheck the diff and commit again.

### Line endings and encoding (LF, UTF-8, no BOM)

Write files as **UTF-8 without BOM**, line endings LF. prek (`mixed-line-ending --fix=lf`, `fix-byte-order-marker`) normalizes this on commit; `.gitattributes` (`* text=auto eol=lf`) does the same at git level, overriding `core.autocrlf`. `ruff.format.line-ending = "lf"`.

## Tests and CI

`.github/workflows/`:

- `ci.yaml` — the single CI entry point (PRs/pushes to `main`/`dev`, manual dispatch, reusable via `workflow_call`).
  - `config` — actionlint plus `docker compose config --quiet` validation.
  - `quality` — `uv sync --locked`, `ruff check`, `ruff format --check`, `ty check`.
  - `tests` — `pytest -m "not integration"` with branch coverage and JUnit XML; reports uploaded as artifacts. All tests here are pure unit tests, so no database service is required.
  - `postgres-smoke` — per-PR end-to-end smoke test against a health-checked `postgres:18-alpine` service (published on `127.0.0.1:15432`), `TEST_DATABASE_URL` + `TEST_SCHEMA=el_smoke`. Job timeout is **3 minutes**; the measured local run is ~4 s, so it stays in per-PR CI rather than a nightly job. `tests/test_postgres_smoke.py` drops/recreates the disposable `el` (source history) and `el_smoke` (module tables) schemas, then runs the real module registry (`main._run_app`, no stubs) twice with `TEST_SCHEMA` isolated from any shared database. EL has no migration tool: the first loader run applies its own DDL (`prepare_source_history`, PD `_ensure_pd_storage`, sales_op `ensure_additive_table_structure`) and the test verifies every expected relation exists. Fixtures are `mock_data/pd/pd.xlsx` plus tiny workbooks generated from the checked-in sales_op specification — never production data. Assertions: exact counts (2730 `pd_data`, 14 assets, 87 distinct material references, 30 indicators, 131 change-log rows, 1 sales_op row per month, 3 history rows), then a second run for July only — identical data, the June neighbour survives, and a legacy `legacy_note` column added between the runs is still present with its original value. Job env points at `postgresql+asyncpg://…@127.0.0.1:15432/dwh`; the local equivalent uses any free port via `TEST_DATABASE_URL` (unset → the module skips).
  - `dependencies` — PR-only dependency review (`actions/dependency-review-action`, `fail-on-severity: moderate`); fails a PR that introduces a known-vulnerable package.
  - `secrets` — gitleaks over the **full history** (`fetch-depth: 0`): a deleted secret is still in every clone. Findings are reported, not suppressed; a false positive is fixed in `.gitleaks.toml` with a reason.
  - `production` — build the `prod` image, smoke-check it (`python -m compileall src`), and export it as an artifact on version tags.
  - `required` — a gate job that fails unless every job above (including `postgres-smoke`) succeeded. Configure **CI required** as the required branch-protection check; there are no path filters that could leave it pending.
- `codeql.yaml` — CodeQL security analysis for `python` and `actions` on pushes/PRs to `main`/`dev` plus a weekly scheduled scan (new queries only exist there). A `visibility` job queries the GitHub API and runs the analysis **only for public repositories**: free CodeQL cannot upload results from a private one. The job is skipped, not downgraded; the API query itself must succeed.
- `docker-release.yaml` — on `vMAJOR.MINOR.PATCH` tags: run `ci.yaml` via `workflow_call`, then publish the exact image that passed CI to GHCR (`ghcr.io/<owner>/excel_loader`, version and full commit SHA tags). Images are not rebuilt after tests.
- `dependabot.yaml` — weekly updates for github-actions, docker, docker-compose, uv (patch versions are ignored).
- `nightly.yaml` — **nightly-only** heavy suite: `schedule` daily at **03:47 UTC** plus `workflow_dispatch`. Its workflow name is `Nightly`; **no job here is referenced by branch protection** — the only required check remains `CI required` from `ci.yaml`. Per-PR gates are neither duplicated nor weakened: PR CI keeps its short `pytest -m "not integration"` run and the 3-minute PostgreSQL smoke.
  - `full-coverage` — `pytest -q --cov=src --cov-branch` with **no marker filter**, so the integration-marked `tests/test_postgres_smoke.py` runs here against the same health-checked `postgres:18-alpine` service (`127.0.0.1:15432`, `TEST_SCHEMA=el_smoke`) used by `ci.yaml`. Artifact: `nightly-test-reports` (`coverage.xml`, `test-results.xml`; 14 days).
  - `security` — `pip-audit` on the **locked runtime environment** (`uv export --locked --no-dev --no-emit-project`, audited via `uvx --from pip-audit`; no new project dependency). **Report-only on day one:** exit status is captured to `pip-audit.exit`, findings to `pip-audit.json`, and both are uploaded; findings *and* tool errors never fail the nightly. Promotion to a gate requires triaging the baseline and an explicit policy change — no unhandled red gate. Artifact: `nightly-security-reports` (`locked-requirements.txt`, `pip-audit.json`, `pip-audit.exit`; 14 days). The per-PR gitleaks job stays the secret-scanning gate; nightly does not duplicate it.
  - `dependency-freshness` — `uv sync --locked` followed by `uv pip list --outdated --format json`; report-only. Artifact: `nightly-dependency-freshness` (14 days).
  - `production-image` — builds the `prod` image, then runs `python -m compileall -q src` and `python src/main.py --help` inside it (benign: argparse exits before settings are read). Uses a separate GHA cache scope (`excel-loader-nightly`) so nightly builds do not affect the per-PR cache.

### Nightly notifications

There is **no CI-failure → Alertmanager wiring** in this repo today. Alertmanager plus the local SMTP relay (`infra/alertmanager/`, `infra/compose.monitoring.yaml`) deliver Prometheus alerts produced by the **loader's own run metrics** (see `docs/ARCHITECTURE.md`); they do not watch GitHub Actions. Research: no repository webhooks are configured (`gh api repos/<owner>/<repo>/hooks` returns an empty list), and no workflow step posts to Alertmanager, e-mail, chat or issues — `GITHUB_TOKEN` is used only for gitleaks, dependency review, CodeQL and GHCR publishing. Nightly failures therefore surface in the Actions UI and in GitHub's own notifications to subscribed watchers (subject to each user's GitHub settings), not via the monitoring SMTP route.

The exact missing wiring (deliberately **not** added here): a receiver/webhook integration in `infra/alertmanager/alertmanager.tpl.yml` plus a repository secret holding its URL/auth, and a `failure()`-triggered step or `workflow_run` job that posts the run metadata to it. Choose the route with the maintainer; never hardcode webhook URLs or credentials and do not invent a secret name. Until that is in place, `gh run list --workflow=nightly.yaml` and the Actions UI are the source of truth for nightly failures.

### Dispatching the nightly manually

Actions → **Nightly** → *Run workflow*, or `gh workflow run nightly.yaml --ref <branch-containing-the-workflow>`, then `gh run list --workflow=nightly.yaml --limit 5`. The workflow file must exist on the chosen ref: a local-only branch cannot be dispatched until it is pushed. Scheduled runs always use the default branch.

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
- **Verify after changes:** `just check` and `just test`. Changed parsing/mapping — run the corresponding `tests/test_*.py`; changed DDL/grants — check against a local PostgreSQL (`just db-stand`).
- **Documentation is part of the change:** changed behavior, mapping or config — update `AGENTS.md` and the relevant file in `docs/`.
- **Git hygiene:** a shared repo, foreign edits are possible; don't revert other people's changes.
- **uv TLS:** verify through the system certificate store (`UV_SYSTEM_CERTS=1` is set in the `justfile`). Don't bring back `UV_INSECURE_HOST`, don't disable SSL globally; the corporate CA is mounted from `/etc/ssl/certs`.
