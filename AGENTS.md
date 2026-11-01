# AGENTS.md — excel_loader

Cheat sheet for agents. Read only the section you need; details — in `docs/*`.

## What it is

An Excel-to-PostgreSQL loader. A single CLI process (`src/main.py`) runs the selected modules, then module post-actions (grants, materialized-view refresh) and common post-actions. The loader sends no e-mail — alerts (incl. e-mail) are delivered by the monitoring stack (Alertmanager).

- `quotes_nsi` — quotes reference data, `quotes` — quotes, `pd` — production summary, `sales_op` — per-client operational sales plans.
- Python 3.14 + **uv**, SQLAlchemy/asyncpg, pandas/openpyxl/pyxlsb. Main code — `src/`.
- Runs in Compose; PostgreSQL and Loki are external services. Settings — `config.toml` (template `config.example.toml`).

## Commands

| What | Command |
|---|---|
| Lint | `uv run ruff check` |
| Format | `uv run ruff format` |
| Types | `uv run ty check` |
| Everything at once | `just check` |
| Tests | `just test` |
| CLI inside compose | `just loader --quotes` · `just loader --pd --source local` |
| Build image + CLI | `just loader-build …` |
| Postgres for standalone | `just db-standalone` |
| Seed PD reference data | `just seed` |
| Logs (Alloy / Loki+Grafana) | `just logging` · `just logging-standalone` |
| Full observability (Loki + Prometheus + Alertmanager) | `just monitoring` |

Default modules — all four. `--module <code>` (repeatable; codes: `quotes_nsi`, `quotes`, `pd`, `sales_op`) and the legacy `--quotes-nsi`, `--quotes`, `--pd`, `--sales-op` flags select modules (a union); `--source local|imap` overrides the source for the selected modules.

## Landmines (critical!)

1. **`.gitignore` is deny-all with a whitelist.** A new root file or folder won't reach git until `!path` is added (`!/docs/` and `!/docs/**` already exist). Nested `.gitignore` files work the same way.
2. **There is no `README.md`** — it was retired in November 2026 (audit A11): the entry point is this `AGENTS.md`, details live in `docs/*`. Don't create a new README without an explicit request; `.gitignore` no longer whitelists it.
3. **`quotes_nsi_frequencies` and `quotes_nsi_frequencies_rank` keep the source text verbatim**, including the empty string, `Null` and `-`. These are data, not SQL `NULL` markers; don't "clean" them with normal normalization (`preserve_text_values`).
4. **Aliases in `build_dictionary_payload` are applied before all checks.** `primary_key_columns`, `not_null_columns`, `not_null_filtered_columns`, `unique_columns`, `column_kinds`, `where_columns` are expressed in **target** names. A missing source column or duplicated target names is an error.
5. **Boolean text values:** after `strip().lower()` — `y`/`yes`/`да` → `true`, `n`/`no`/`нет` → `false`, the other text `false` → `false`, everything else → `true`. An empty required (NOT NULL) field → `false`, an empty nullable one → `NULL`.
6. **DDL is compatible with PostgreSQL 14.** Don't use `UNIQUE NULLS NOT DISTINCT` or any features newer than 14 until the minimum version is explicitly bumped.
7. **Parser coordinates (`xls_row`, `xls_col`) are never stored in the DB** — only inside parsing and PD deferred-package recovery.
8. **`pd_view` is created via `CREATE OR REPLACE VIEW`.** If the replace fails, create the first free fallback name with a repeated `_upd` suffix (`CREATE VIEW`) and log/send the format change as an error.
9. **Compose always mounts `./config.toml` to `/app/config.toml`.** `LOADER_CONFIG_PATH` is unnecessary. `POSTGRES_HOST` (when set) overrides `common.postgres.host` from the TOML.
10. **`src/scripts/seed.py` is the only seed script.** Shared schema/table helpers live in `src/storage.py`; don't create a new top-level `seed.py`.
11. **PD parses only the first sheet** of a workbook (`_select_first_sheet`); other sheets are ignored. SCI stops reading at the first row without a date/product/brand — a blank separator row ends the data block rather than being treated as data.
12. **`sales_op` is spec-driven.** Source codes, KHD tables and file masks live in `Спецификация sales_op_raw_view.xlsb` (sheet `спека`), not in code (`registry.py` was removed). A source without a file mask is ignored entirely (no file search, no table). The `Not Nullable` column says whether a target column is required: empty/falsy → optional (absent column or empty value → `NULL`), truthy (`Y`/`yes`/`да`/`true`/`1`/`+`) → the column must exist and every kept row must have a value, otherwise the file fails. The producer/seller row filter runs before the Not Nullable check.
13. **Metrics push is opt-in.** `compose.yaml` passes `METRICS_PUSHGATEWAY_URL` to the loader; empty by default — the run pushes nothing until it is set in `.env` (see `infra/compose.monitoring.yaml`). The Alertmanager config is rendered from `ALERT_SMTP_*`/`SUPPORT_MAIL` **at container start** — after editing `.env` the monitoring stack must be restarted (`just monitoring`); smarthost must advertise STARTTLS (587) and `ALERT_SMTP_USER` must equal the sender mailbox.
14. **Loader metrics are pushed, not scraped** — the process is a batch CLI: `src/metrics.py` pushes gauges (`el_loader_run_status`, `..._duration_seconds`, `..._last_success_seconds`, `..._module_status{module}`) to the Pushgateway in `infra/compose.monitoring.yaml`; alerts live in `infra/prometheus/alert-rules.yml` (thresholds assume daily runs).
15. **`SourceFileNotFoundError` lives in `source_resolver`, not `business_errors`.** It is a `SourceError` subclass (not a `BusinessLoadError`): a missing source file is a WARNING + skip, not a data error. `excel_parser.read_workbook_metadata` and the `baiinfo`/`argus_pdf` part readers raise it. In any new runner, catch `except SourceFileNotFoundError` **before** `except SourceError` / `except BusinessLoadError` — never match on the Russian text `"файл не найден"`.
16. **The dictionary pipeline has one orchestrator, not three.** `src/dictionary_runner.py` runs every loader kind through `_run_loader_strategy` + a `_LoaderStrategy` adapter (`_SingleLoaderStrategy` / `_CombinedLoaderStrategy` / `_PatternLoaderStrategy`). A new loader kind is a new adapter + a registration line in `run_dictionaries` — do not copy a `_process_*` function. Per-kind quirks (single-file metadata log, combined missing-part warning, pattern no-files warning) live in the adapter's `resolve`.
17. **Modules are a registry, not an `if module ==` chain.** `main._run_modules` dispatches over `_module_entries(settings)` (`ModuleEntry` = code + post-action config + runner). Runners return `ModuleOutcome` (`src/module_models.py`), carrying their own `tables_to_grant` / `relation_references_to_grant`. Adding module #5 = one `ModuleEntry` + one runner; `_run_modules` itself is not edited.
18. **The engine lifecycle has one owner.** Runners and post-actions never create/dispose engines by hand — use `async with postgres_session(settings) as engine` from `src/db.py` (it disposes on both success and error). `create_postgres_engine` stays the only engine factory.
19. **Grants logic exists once.** Connection-level `storage.grant_usage_on_schema` / `grant_select_on_table` (role-filtered via `POSTGRES_ROLE_EXISTS_SQL`) are the single implementation; `postgres.py`'s `grant_*_to_existing_roles` are thin engine-level wrappers. Don't resurrect unfiltered/filtered copies.
20. **Open any openpyxl workbook only through `excel_parser.load_openpyxl_workbook`** — it carries the only scoped suppression of openpyxl's "… extension is not supported and will be removed" warning. No module-level `filterwarnings` anywhere (the old sales_op import-time filter is gone). Per-module read modes stay: PD loads fully (`read_only=False`), sales_op opens `read_only=True, keep_links=False`. Dictionary **value** frames are a separate path: pandas' native calamine engine (`excel_parser.EXCEL_ENGINE`) reads all formats; pyxlsb remains only for the direct sales_op spec reader (`sales_op/specification.py`).
21. **The per-row order of `build_dictionary_payload` is frozen by tests** (`tests/test_audit_followup.py`): `where` filter → raw skip → convert → converted skip → primary-key duplicate. The body is split into `_validate_column_contract` + `_RowConverter`; refactors must keep both the order and the alias-before-checks rule (landmine 4).

## Working rules

- **Don't commit or push without an explicit request.** Before git operations — `git status` and the current branch.
- **Before delivery:** `just check` and `just test` green. Fix `ruff`/`ty` errors in code; add ignores only when a fix would hurt readability or correctness.
- **uv TLS:** verify through the system certificate store (`UV_SYSTEM_CERTS=1`) — already set in the `justfile`. Don't bring back `UV_INSECURE_HOST`, don't disable SSL verification globally; the corporate CA is mounted from `/etc/ssl/certs`.
- **Documentation is part of the change:** changed behavior/mapping/config — update `AGENTS.md` and the relevant file in `docs/`. Write landmines down immediately.
- **Minimal changes:** follow the file's style, don't add new entities without need.

## Documentation

- `docs/ARCHITECTURE.md` — the shared dictionary engine, config, sources (local/IMAP), storage and DDL, source history, post-actions, logs, metrics and alerts.
- `docs/QUOTES.md` — the `quotes_nsi` and `quotes` modules: registries, files, mappings, primary keys.
- `docs/PD.md` — `pd`: schema, parsing, `value_double`, `pd_view` and fallback, deferred packages.
- `docs/SALES_OP.md` — `sales_op`: specification, file classification, normalization, `sales_op_raw_view`.
- `docs/WORKFLOW.md` — environment, quality, prek, tests, languages, commits and branching.
- `docs/AUDIT.md` — the November 2026 reanimation audit (findings + roadmap), the November 2026 follow-up (extensibility, calamine assessment, P1 implementation log).

**When behavior changes, update the relevant file in `docs/` — that's a requirement, not a wish.**
