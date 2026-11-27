# Remediation Phase 0 — Research (read-only)

Scope: `D:\Work\excel_loader`, branch `dev`. Date of inspection: 2026-11-27.
This is a research deliverable only: no code, config, Git or remote changes were made.

Legend for numeric facts: **[M]** MEASURED (observed in a file or command output),
**[E]** ESTIMATE, **[U]** UNKNOWN (no evidence available). File paths and line
numbers are citations, not counted facts. "Line refs" are 1-based.

## 1. Contract inventory (AGENTS.md and ADRs)

- **AGENTS.md levels:** exactly one, at `AGENTS.md` (root, 74 lines). No nested
  `AGENTS.md` in `src/`, `tests/`, `infra/` or `docs/` (glob `**/AGENTS.md` plus
  `git ls-files | Select-String "AGENTS\.md"`). **[M]**
- **ADRs:** none in this repository — no `docs/adr/`, no `*adr*` path, no file
  matching `adr|ADR|decision` in `git ls-files`. **[M]** (A formal decision
  register does not exist here; see DocMasker `docs/adr/` for contrast.)

Declared contracts:

| Contract | Where declared | What it says |
|---|---|---|
| Ports | `docs/ARCHITECTURE.md:142-145`; `infra/compose.db.yaml:22`; `infra/compose.monitoring.yaml` | Shared monitoring host ports 3000 / 3100 / 9090 / 9091 / 9093 + Alloy 12345; the three app stacks and the central DWH stack all publish the same set and must **not run simultaneously** (remap deliberately if needed). **[M]** Local Postgres publishes 5432:5432. **[M]** Loki/Prometheus retention defaults 168h / 15d. **[M]** |
| Metric names | `AGENTS.md:45-46`; `docs/ARCHITECTURE.md:148-160`; `src/metrics.py:43-47` | Five `el_loader_*` gauges plus the `module` label; opt-in push; alerts assume daily runs. See §5. **[M]** |
| CI gates | `AGENTS.md:60`; `docs/WORKFLOW.md:42-56`; `.github/workflows/ci.yaml:19-163`; `prek.toml` | Jobs: config (actionlint + `docker compose config --quiet`), quality (ruff/format/ty), tests (pytest + coverage), dependencies, secrets (gitleaks, full history), production, `required` gate (`jq -e 'all(...)'`, `ci.yaml:153-163`). Hook policy: checkers on staged files at commit, whole tree in CI, no `manual` stage. **[M]** |
| Deprecation policy | No formal policy found | Only scattered facts: legacy CLI flags still accepted (`AGENTS.md:29`), README retired (`AGENTS.md:34`), pyxlsb "deprecated in pandas" noted (`docs/ARCHITECTURE.md:70`), retired DB columns dropped (`docs/ARCHITECTURE.md:121,127`), legacy table columns handled (`docs/SALES_OP.md:87-88`). A declared support/deprecation window: **[U]** |
| Env vars | `AGENTS.md:41,45`; `docs/ARCHITECTURE.md:40-41,140-145`; `.env.example`; `src/config.py:317-318`; `src/metrics.py:37,80`; `compose.yaml`; `justfile:6` | App-side overrides: `POSTGRES_HOST`, `POSTGRES_USER` (`src/config.py:317-318`), `METRICS_PUSHGATEWAY_URL` (`src/metrics.py:37,80`). Compose/stack: `LOKI_URL`, `LOG_ENV`, `POSTGRES_USER/PASSWORD/DB`, `GF_SECURITY_ADMIN_USER/PASSWORD`, `ALERT_SMTP_*`, `SUPPORT_MAIL`, `SMTP_RELAY_*`, `PROMETHEUS_RETENTION`, `LOKI_RETENTION`. `UV_SYSTEM_CERTS=1` in `justfile:6`. **[M]** |

Two documentation inconsistencies found (factual, not fixed):

1. `docs/ARCHITECTURE.md:40` says "There are no other env overrides"; `src/config.py:318`
   implements `POSTGRES_USER` (covered by `tests/test_config.py:288-302`), and
   `docs/WORKFLOW.md:5` mentions `POSTGRES_USER` indirectly. **[M]**
2. `docs/WORKFLOW.md:34` still says `ty check` is `stages = ["manual"]` in prek;
   `prek.toml` now has no manual stage and `AGENTS.md:60` forbids it. **[M]**

## 2. pytest / coverage invocation

- Local: `just test` → `uv run pytest -q --cov=src` (`justfile:60-62`,
  `docs/WORKFLOW.md:22`). `just check` = format + `ruff check` + `ty check` +
  `just test` (`justfile:64-68`). **[M]**
- CI, exact command from `.github/workflows/ci.yaml:66-70` (job env
  `COVERAGE_FILE: coverage-data`, line 57):
  `uv run --no-sync pytest -q --cov=src --cov-branch --cov-report=term --cov-report=xml:coverage.xml --junitxml=test-results.xml --durations=10`. **[M]**
- `--cov-fail-under`: **absent** from this repository. Evidence:
  `git grep -n "cov-fail-under" -- .` → no matches (exit 1); `rg -n "--cov-fail-under|fail-under|fail_under" .github docs justfile pyproject.toml prek.toml` → only
  `pyproject.toml:74` (`fail_under = 40`) and `docs/WORKFLOW.md:28`. The floor is
  enforced via `[tool.coverage.report] fail_under = 40` (`pyproject.toml:71-75`),
  not via a pytest flag. **[M]**
- Measured coverage: local `.coverage` exists (258,048 bytes, mtime
  2026-11-25 19:55:29). Command run: `uv run --no-sync coverage report` →
  `TOTAL 3719 statements, 1446 missing, 1026 branches, 102 partial, 58%`. **[M]**
  This is a local snapshot that may predate current HEAD `1568a46`; no
  `coverage.xml` exists in the working tree. CI publishes coverage only as a
  workflow artifact (`ci.yaml:71-78`, retention 7 days). **[M]**

## 3. Integration tests against real PostgreSQL

- **None exist.** `tests/` contains 19 unit-test modules plus `conftest.py`;
  `docs/WORKFLOW.md:47` states "All current tests are pure unit tests, so no
  database service is required". **[M]**
- `tests/conftest.py:16-32` builds a minimal in-memory `Settings` (schema `el`,
  `mock_data`) specifically so tests never read `config.toml` or contact
  PostgreSQL. `tests/test_db.py:24-79` tests the engine-lifecycle contract with
  `_FakeEngine` and monkeypatched `create_postgres_engine`. **[M]**
- Search for `TEST_DATABASE_URL|create_async_engine|create_engine|psycopg|@pytest.mark.integration` in `tests/` → no real DB connections. The only
  `postgres_session` imports are in `test_db.py` and are stubbed. **[M]**
- `just db-stand` (`justfile:17-18`) is a manual local stand; CI's `tests` job has
  no `services:` block (`ci.yaml:52-78`). **[M]**
- `docs/WORKFLOW.md:28,106` claims DB-layer code is "exercised against real
  PostgreSQL" / suggests `just db-stand` after DDL changes — no such automated
  test or invocation exists in the repo. Where that verification happens: **[U]**

## 4. How RC and DM run PostgreSQL integration tests in CI

Both workflows were read read-only:

- **ReportCollector** `.github/workflows/ci.yaml:39-103`: job `back` declares
  `services.postgres` (`postgres:18-alpine`, ports `5432:5432`, health check
  `pg_isready`) and `services.valkey`. Env includes `TEST_DATABASE_URL`
  (`postgresql+asyncpg://...@localhost:5432/dwh`), `TEST_SCHEMA=rc_test`,
  `POSTGRES_HOST/PORT/USER/PASSWORD/DB`, `COVERAGE_FILE=coverage-data`. Test step
  (line 94): `uv run --no-sync pytest -q --cov=src --cov-branch --cov-report=term --cov-report=xml:coverage.xml --junitxml=test-results.xml --durations=10`;
  coverage data is uploaded as an artifact. A separate `coverage` job
  (lines 105-146) requires a report from **every** matrix leg, then combines
  them and runs `coverage report --fail-under=80` (line 139). The `production`
  job additionally uses an isolated compose stack and always tears it down with
  `down --volumes` (lines 279-338). **[M]**
- **DocMasker** `.github/workflows/ci.yaml:35-91`: `services.postgres`
  (`postgres:18-alpine`, published as `127.0.0.1:15432:5432`, health check)
  plus valkey; env `TEST_DATABASE_URL` and `DATABASE_URL`
  (`postgresql+psycopg://dm:...@127.0.0.1:15432/dm_ci`); migrations applied with
  `alembic upgrade head` (line 80); test step (line 82) uses
  `pytest -q --cov=src --cov-branch --cov-fail-under=80 --cov-report=term --cov-report=xml:coverage.xml --junitxml=test-results.xml --durations=10`. **[M]**

**Pattern EL could copy:** add a `services.postgres` container with health
check to the `tests` job, pass an isolated `TEST_DATABASE_URL`/`POSTGRES_HOST`
env pointing at it, have tests create and drop a dedicated schema (EL's
`conftest.py` already reserves schema `el` for fixtures), add an explicit
coverage gate (`--cov-fail-under=<n>` as DM does, or `coverage report
--fail-under=<n>` as RC does), and upload `coverage.xml` + JUnit as artifacts.
Note the version choice needs a decision: EL's DDL contract is PostgreSQL 14
(`AGENTS.md:38`, `docs/ARCHITECTURE.md:123`) while its local compose image and
both sibling CIs use `postgres:18-alpine`. **[M]**

## 5. Metrics module and shared monitoring contract

`src/metrics.py` (all series are **Gauge**; opt-in via `METRICS_PUSHGATEWAY_URL`,
empty by default in `compose.yaml`): **[M]**

| Metric | Type | Labels / value convention |
|---|---|---|
| `el_loader_run_status` | Gauge | no labels; `0` success, `1` partial_success, `2` failure (`OUTCOME_STATUS_VALUES`, line 41) |
| `el_loader_run_duration_seconds` | Gauge | no labels; wall duration |
| `el_loader_run_timestamp_seconds` | Gauge | no labels; Unix time of push |
| `el_loader_run_last_success_seconds` | Gauge | no labels; pushed **only** on full success; Pushgateway retains previous value |
| `el_loader_module_status` | Gauge | one label `module`; `1` failed, `0` success; only selected modules pushed |

Conventions: `push_to_gateway(..., job="excel_loader", timeout=10 s)` with a
single grouping key (each push replaces prior series); failures logged ERROR and
never change the exit code (`src/metrics.py:78-127`). Prometheus scrapes the
Pushgateway every 15 s with `honor_labels: true` so `job=excel_loader` from the
loader is preserved (`infra/prometheus/prometheus.yml:21-27`). **[M]**
Stack-self series use the standard `up{job="pushgateway|prometheus|loki|alertmanager|grafana"}`
(`infra/prometheus/alert-rules.yml:38-81`). **[M]**
Eight alert rules are project-prefixed (`ExcelLoaderRunFailed`,
`ExcelLoaderLastRunTooOld`, `ExcelLoaderNoSuccessfulRun`,
`ExcelLoaderPushgatewayDown`, `ExcelLoaderPrometheusSelfScrapeDown`,
`ExcelLoaderLokiDown`, `ExcelLoaderAlertmanagerDown`, `ExcelLoaderGrafanaDown`);
stale-run threshold 93600 s = 26 h, assumes daily runs
(`infra/prometheus/alert-rules.yml:14-81`). **[M]**
Grafana dashboard `excel-loader-overview` queries the same five names
(`infra/grafana/provisioning/dashboards-json/overview.json:17,36,51,76,100`). **[M]**

Shared monitoring contract docs: `docs/ARCHITECTURE.md:135-166` is the canonical
description for this repo (including stable JSON log fields
`timestamp/level/logger/module/message/run_id` and the Alloy label scheme).
Cross-stack enforcement: `D:\Work\infra\check-drift.py` (`STACKS` includes DWH,
ReportCollector, excel_loader, DocMasker; `IDENTICAL_FILES` requires byte-identical
`infra/loki/config.yaml` and `infra/alertmanager/entrypoint.sh`), invoked locally
by `just drift-check` (`justfile:28-30`). **[M]**

## 6. PostgreSQL backup state in EL's compose

One PostgreSQL instance is defined in EL's compose (`compose.yaml:1-4` includes
`infra/compose.db.yaml` and `infra/compose.monitoring.yaml`): **[M]**

- `infra/compose.db.yaml:11-31`: service `postgres`, container `el-postgres`,
  image `postgres:18-alpine`, `restart: unless-stopped`, host port `5432:5432`,
  health check `pg_isready` (10 s interval, 5 s timeout, 5 retries),
  named volume `postgres-data` → `el-postgres-data` at
  `/var/lib/postgresql/data`. **[M]**
- **Automated backup: none configured.** No `pg_dump`, `pgbackrest`, WAL
  archiving, backup sidecar service, cron/job or backup env var exists in EL
  (`git grep -n -i "backup|pg_dump|pgbackrest|wal_archive|retention" -- .`
  matches only Loki/Prometheus retention, log retention, artifact retention and
  an unrelated Excel-file name). Backup schedule and retention: **[U]** (no
  declaration — not "0 days", just undefined).
- `just clean` runs `docker compose down --volumes --remove-orphans`
  (`justfile:36-38`) and would delete `el-postgres-data`; it was **not** run. **[M]**

## 7. Repository ownership and branch state

- `git remote -v`: `origin git@github.com:XIIIMICT/excel_loader.git (fetch)` and
  `(push)` — one remote. **[M]**
- Current branch `dev`, working tree clean before this document was created. **[M]**
- Current dev head: `1568a46e215b6f97837c83215c98daef00f0031e`, author
  `XIIIMICT`, `2026-11-25T16:56:13+03:00`, subject
  `docs(mail): consolidate SMTP caveats and add docstring policy`. **[M]**
- Local refs: `main` = `78279d6d65895dcf584a5efd1bacd5f96d9813fe`;
  `fix/cross-remediation` and `refactor/drop-future-annotations` point at the
  same commit as `dev` (`git show-ref --heads`). **[M]**
- **Remote default branch: [U].** `git symbolic-ref refs/remotes/origin/HEAD`
  returned nothing; `git ls-remote --symref origin HEAD` failed with
  `ssh: connect to host github.com port 22: Connection timed out` (no network
  access). Local convention (`docs/WORKFLOW.md:73-79`) treats `main` as stable
  and `dev` as integration. **[M]**

## Evidence — commands run

`git remote -v`; `git branch --show-current`; `git rev-parse HEAD`;
`git symbolic-ref refs/remotes/origin/HEAD`; `git status --short`;
`git show-ref --heads`; `git log -1 --format="%H%n%an%n%ad%n%s" --date=iso-strict`;
`git ls-remote --symref origin HEAD` (failed, timeout);
`git grep -n "cov-fail-under" -- .`; `git grep -n -i "backup|pg_dump|pgbackrest|wal_archive|retention" -- .`;
`git ls-files` filtered for `AGENTS.md|adr|ADR|decision`;
`uv run --no-sync coverage report`;
`Get-ChildItem -Force` on repo root, `docs/`, `.github/workflows/`, `tests/`;
plus direct reads of every file cited above. No destructive commands, no data
changes, no `gh api` calls, no commits, no pushes.

## Addendum: P0-4 remediation finding (2026-11-27, branch `fix/pg-smoke-and-meaningful-tests`)

The research above frames the coverage question; the decision taken on it is
now recorded in `docs/WORKFLOW.md`. Short finding: the 57.70% baseline was not
a reason to add `--cov-fail-under=80` (a percentage target the maintainer
explicitly rejected). The measurable blind spot was `src/pd/excel_parser.py`
(~15% covered, zero PD tests) and `src/pd/unsent.py` (27%); DB/SQL code was
better served by a real-PostgreSQL smoke test than by more unit tests.
After adding meaningful tests (PD parser/round-trip, local source resolution,
sales_op runner edges) the measured coverage is **67.76%** (271 passed, 1
skipped; `-m "not integration"`), i.e. **57.70% → 67.76%**. `fail_under` was
ratcheted 40 → 65 with headroom; no `--cov-fail-under` flag and no 80 gate.
The new `postgres-smoke` CI job (health-checked `postgres:18-alpine`,
`TEST_DATABASE_URL`, disposable `el`/`el_smoke` schemas, two real loader runs)
completes in ~4 s locally, so it stayed in per-PR CI with a 3-minute timeout
instead of moving to a nightly job. This addendum is the only change to the
read-only research deliverable; the findings above are unchanged. **[M]**
