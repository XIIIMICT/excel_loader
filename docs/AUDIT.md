# Reanimation audit (November 2026)

The reanimation work itself (lint fixes, the logging/monitoring stack, CI,
pre-commit hooks) landed in the reanimation commit — see the git history.
This file is the remaining architecture audit: findings plus a prioritised
roadmap. `just check` and `just test` are green at the time of writing.

## Decisions taken during reanimation

1. **No SMTP in the loader.** Error notifications are delivered by the
   Alertmanager of the monitoring stack, driven by the pushed run metrics
   (`el_loader_run_status`, `..._last_success_seconds`). If the monitoring
   stack is unreachable over the network, the metrics push fails, which is
   logged (`ERROR`) to the run log file — and no e-mail goes out, exactly
   because Alertmanager sits in that stack.
   The old aiosmtplib path (`src/email_sender.py`, `_send_error_emails`) was
   removed together with the aiosmtplib dependency and the `common.smtp`
   config keys (from `config.py`, `config.example.toml` and the local
   `config.toml`).
2. **Metrics are pushed, not scraped.** The loader is a batch CLI run by
   cron: it starts, runs and exits. On every run it pushes the run state to
   the Pushgateway (`METRICS_PUSHGATEWAY_URL`, empty by default) and lets
   the Prometheus stack keep the run history. E-mails, dashboards and
   alert rules live entirely in the monitoring stack.
3. **CI does not validate workflows by itself.** The `actionlint` step was
   dropped from `ci.yaml` (leftover from the ReportCollector import);
   the Compose-validation step stays.

## Architecture findings and roadmap

Current layout (see `docs/ARCHITECTURE.md`): `main.py` drives four modules
(`quotes_nsi`, `quotes`, `pd`, `sales_op`), each with its own runner package;
dictionaries share an engine (`dictionaries/base.py` + `dictionary_runner.py`
+ `excel_parser.py`); storage is split across `storage.py`, `postgres.py`,
`schema_definition.py`; sources are resolved by `source_resolver.py`; PD and
sales_op carry their own parsers and DB layers.

The goal for each finding: additional modules must be **added, not
extended** — a new loader file + one registration line, without touching
shared internals.

### P1 — remove the duplication that gates every new module

**A1. Loader pipeline is triplicated (`src/dictionary_runner.py`).**
`_process_dictionary_loader`, `_process_combined_loader` and
`_process_pattern_loader` repeat ~75% of the same body: resolve sources →
build payload → build `SourceHistoryEntry` entries (three copies of the same
zip-with-`strict=True` builder) → `build_dictionary_table` → inspect storage
→ prepare when needed → `replace_table_rows` → identical 3-branch error
handling → identical success/warning logging.
*Do:* one `LoaderRun` orchestrator with a small strategy interface
(`resolve_sources(loader, config) -> Sequence[SourceFile]` +
`build_payload(...)`) and a single `persist_payload(engine, payload,
history_table, entries) -> bool` helper. Keep the documented per-kind
differences (single-file metadata logging, `allow_empty`, missing-part
warnings) as strategy quirks.
*Accept:* the three functions collapse to ≤ 3 × 30 lines; a new loader kind
is a strategy, not a copy of the pipeline.

**A2. Runners repeat the same failure-to-outcome mapping.** Every runner
couples `try/except BusinessLoadError → log + failed` with a bespoke summary
type (`DictionaryRunSummary`, `PdRunSummary`, `SalesOpRunSummary`) and
`main.py` re-wraps them into `ModuleRun` with three `if module ==` chains.
*Do:* a common `ModuleOutcome` dataclass returned by each runner, plus one
`_run_module(settings, spec)` dispatch over a module registry.
*Accept:* adding module #5 means a new registry entry + runner, no `main.py`
edits beyond the registry.

**A3. `str`-based control flow on error messages.**
`dictionary_runner._process_dictionary_loader` decides "file not found →
warning, skip" via `if "файл не найден" in str(error)` — a message is not an
API. `source_resolver` reports both fatal and skip-worthy conditions the same
way.
*Do:* distinct exception subclasses (`SourceFileNotFoundError` vs
`SourceError`) and `except SourceFileNotFoundError` in the runner. Keep
user-facing Russian text in messages, but never parse them.

### P2 — shared infrastructure worth consolidating

**A4. Engine lifecycle is ad-hoc.** Every runner (dictionary, post-actions,
`pd/*`, `sales_op/*`) creates + disposes its own SQLAlchemy engine with
`try/finally`.
*Do:* a single `postgres_session(settings)` async context manager (one
engine, guaranteed dispose on success and error) used by all runners and
post-actions. Per-run scoping is acceptable either way; the point is one
owner and one cleanup path.

**A5. Grants logic exists twice.** `storage.py::_grant_usage_on_schema` /
`_grant_select_on_table` (unfiltered) duplicate `postgres.py::
grant_*_to_existing_roles` (filtered by `pg_roles`). Semantics differ
silently; a fix in one misses the other.
*Do:* one grants helper (module of `postgres.py`); storage calls it. While
touching storage: `_ensure_table_exists` runs `create_all` unconditionally
and then a dead `if table_exists: return` check.

**A6. `excel_parser.build_dictionary_payload` is the risk hotspot.** One
suppressed-complexity function (~150 lines) does aliasing → missing-column
validation → where-filter → per-row conversion → unique/PK checks → column
definitions. Bugs land here (aliases-before-checks is even a landmine in
`AGENTS.md`).
*Do:* split into (a) `_validate_column_contract(...)` (missing/duplicate
checks, aliases), (b) `_convert_row(...)` (pure per-row conversion with
warnings), (c) the payload assembling. Freeze the documented per-row order
(`where` → raw skip → convert → converted skip → PK dup) with a targeted
unit test, so future refactors cannot reorder it silently.

**A7. PD and sales_op carry parallel infrastructure.** `pd/*` and
`sales_op/*` each implement their own engine setup, sheet reading and
warning filters; `sales_op` even installs a module-level openpyxl
`simplefilter` at import time (hidden global state).
*Do (incrementally):* move "read first sheet + workbook metadata" into the
shared `excel_parser` surface and have the runners consume A4/A5 helpers.
Do **not** generalise their parsing semantics — first sheet only, SCI stops
at a blank row, deferred packages — these are documented landmines.

### P3 — supporting items

- **A8. Settings surface.** `config.Settings` keeps ~25 one-line delegating
  properties. A2 removes half; the rest may become `ModuleConfig` methods.
  Optional.
- **A9. Outcome-matrix tests.** `main._run_app` exit-code matrix (all
  success / partial / post-action failure / common post-action failure) has
  no direct test; ~6 cases carry the whole CLI contract.
- **A10. Coverage floor.** Total coverage ≈ 45% (parsing ~87%, but
  `dictionary_runner`/`main`/db layers ≤ 20%). Add `--cov-fail-under=50` now
  and raise the number with each refactoring PR.
- **A11. README.md** is outdated (landmine 2). Rewrite its top after at
  least half of the roadmap above is burned, or retire it in favor of
  `docs/`.

## Operationally still unverified

- E-mail delivery end-to-end: a test alert requires a live `just monitoring`
  with real SMTP credentials in `.env`; the Alertmanager config template and
  its rendering were validated, an actual letter was not sent.
- Metrics path in docker: validated as config (compose) and with unit tests;
  the same live run as above should confirm the dashboard shows data.
