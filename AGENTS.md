# AGENTS.md — excel_loader

Cheat sheet for agents. Read only the section you need; details — in `docs/*`.

## What it is

An Excel-to-PostgreSQL loader. A single CLI process (`src/main.py`) runs the selected modules, then module post-actions (grants, materialized-view refresh) and common post-actions. The loader sends no e-mail — alerts (incl. e-mail) are delivered by the monitoring stack (Alertmanager).

- `quotes_nsi` — quotes reference data, `quotes` — quotes, `pd` — production summary, `sales_op` — per-client operational sales plans.
- Python 3.15+ **uv**, SQLAlchemy/asyncpg, pandas/openpyxl/pyxlsb. Main code — `src/`.
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
| Postgres for local | `just db-stand` |
| Seed PD reference data | `just seed` |
| Logs (Alloy / Loki+Grafana) | `just monitoring` |
| Full observability (Loki + Prometheus + Alertmanager) | `just monitoring-stand` |

Default modules — all four. `--module <code>` (repeatable; codes: `quotes_nsi`, `quotes`, `pd`, `sales_op`) and the legacy `--quotes-nsi`, `--quotes`, `--pd`, `--sales-op` flags select modules (a union); `--source local|imap` overrides the source for the selected modules. `--force` and `--months 2026-01,2026-02|all` are sales_op-only: with `sales_op` not selected the CLI exits with code `2`.

## Landmines (critical!)

1. **`.gitignore` is deny-all with a whitelist.** A new root file or folder won't reach git until `!path` is added (`!/docs/` and `!/docs/**` already exist). Nested `.gitignore` files work the same way.
2. **There is no `README.md`** — it was retired in November 2026: the entry point is this `AGENTS.md`, details live in `docs/*`. Don't create a new README without an explicit request; `.gitignore` no longer whitelists it.
3. **`quotes_nsi_frequencies` and `quotes_nsi_frequencies_rank` keep the source text verbatim**, including the empty string, `Null` and `-`. These are data, not SQL `NULL` markers; don't "clean" them with normal normalization (`preserve_text_values`).
4. **Aliases in `build_dictionary_payload` are applied before all checks.** `primary_key_columns`, `not_null_columns`, `not_null_filtered_columns`, `unique_columns`, `column_kinds`, `where_columns` are expressed in **target** names. A missing source column or duplicated target names is an error.
5. **Boolean text values:** after `strip().lower()` — `y`/`yes`/`да` → `true`, `n`/`no`/`нет` → `false`, the other text `false` → `false`, everything else → `true`. An empty required (NOT NULL) field → `false`, an empty nullable one → `NULL`.
6. **DDL is compatible with PostgreSQL 14.** Don't use `UNIQUE NULLS NOT DISTINCT` or any features newer than 14 until the minimum version is explicitly bumped.
7. **Parser coordinates (`xls_row`, `xls_col`) are never stored in the DB** — only inside parsing and PD deferred-package recovery.
8. **`pd_view` is created via `CREATE OR REPLACE VIEW`.** If the replace fails, create the first free fallback name with a repeated `_upd` suffix (`CREATE VIEW`) and log/send the format change as an error.
9. **Compose always mounts `./config.toml` to `/app/config.toml`.** `LOADER_CONFIG_PATH` is unnecessary. `POSTGRES_HOST` (when set) overrides `common.postgres.host` from the TOML.
10. **`src/scripts/seed.py` is the only seed script.** Shared schema/table helpers live in `src/storage.py`; don't create a new top-level `seed.py`.
11. **PD parses only the first sheet** of a workbook (`_select_first_sheet`); other sheets are ignored. SCI skips rows where any of `pub_date`, `product` or `avg_price` is empty and stops reading at the first row where all three are empty (a fully blank row is the common case); other columns may still hold values. sales_op skips rows with an empty «Наименование контрагента» and stops at the first fully empty row (the producer/seller filter stays before the Not Nullable check).
12. **`sales_op` is spec-driven and month-partitioned.** Source codes, KHD tables and file masks live in `Спецификация sales_op_raw_view.xlsb` (sheet `спека`), not in code (`registry.py` was removed). A source without a file mask is ignored entirely (no file search, no table). The `Not Nullable` column says whether a target column is required: empty/falsy → optional (absent column or empty value → `NULL`), truthy (`Y`/`yes`/`да`/`true`/`1`/`+`) → the column must exist and every kept row must have a value, otherwise the file fails. The producer/seller/client-name row filter runs before the Not Nullable check (all three are text columns: a blank cell **or a text `0` after `strip()`** is a missing value, and a text `0` in any text column becomes `NULL` while a numeric zero in `float`/`int` columns is kept). **Months:** `data_dir` holds `YYYY-MM` folders; a run loads the current month (`settings.app_timezone`) by default, `--months 2026-01,2027-02|all` overrides it, `--force` also takes files starting with `_` (the prefix marks an already loaded file: after a successful group replacement the loader renames each file to `_<name>`, leaves already prefixed files alone, and reports rename failures as one WARNING list). A missing month folder is always an error; IMAP is not supported for sales_op. Each `(source, month)` is replaced in one transaction: additive table changes only, `DELETE` by the `doc_yearmonth` range, insert, history; if any file of that group fails, the group is left untouched. `TRUNCATE` is gone and other months survive. After the replacements the runner logs `Не найдено данных для <schema>.<table> за <YYYY-MM>.` for every specification source/month pair without a file. Tables carry `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`; the view exposes it after the spec columns.
13. **Metrics push is opt-in.** `compose.yaml` passes `METRICS_PUSHGATEWAY_URL` to the loader; empty by default — the run pushes nothing until it is set in `.env`. The Alertmanager config is rendered from `ALERT_SMTP_*`/`SUPPORT_MAIL` **at container start** — after editing `.env` the monitoring stack must be restarted (`just monitoring-stand`); smarthost must advertise STARTTLS (587) and `ALERT_SMTP_USER` must equal the sender mailbox.
14. **Loader metrics are pushed, not scraped** — the process is a batch CLI: `src/metrics.py` pushes gauges (`el_loader_run_status`, `..._duration_seconds`, `..._last_success_seconds`, `..._module_status{module}`) to the Pushgateway in `infra/compose.monitoring.yaml`; alerts live in `infra/prometheus/alert-rules.yml` (thresholds assume daily runs).
15. **`SourceFileNotFoundError` lives in `source_resolver`, not `business_errors`.** It is a `SourceError` subclass (not a `BusinessLoadError`): a missing source file is a WARNING + skip, not a data error. `excel_parser.read_workbook_metadata` and the `baiinfo`/`argus_pdf` part readers raise it. In any new runner, catch `except SourceFileNotFoundError` **before** `except SourceError` / `except BusinessLoadError` — never match on the Russian text `"файл не найден"`.
16. **The dictionary pipeline has one orchestrator, not three.** `src/dictionary_runner.py` runs every loader kind through `_run_loader_strategy` + a `_LoaderStrategy` adapter (`_SingleLoaderStrategy` / `_CombinedLoaderStrategy` / `_PatternLoaderStrategy`). A new loader kind is a new adapter + a registration line in `run_dictionaries` — do not copy a `_process_*` function. Per-kind quirks (single-file metadata log, combined missing-part warning, pattern no-files warning) live in the adapter's `resolve`.
17. **Modules are a registry, not an `if module ==` chain.** `main._run_modules` dispatches over `_module_entries(settings, sales_op_options=…)` (`ModuleEntry` = code + post-action config + runner). Runners return `ModuleOutcome` (`src/module_models.py`), carrying their own `tables_to_grant` / `relation_references_to_grant`. Adding module #5 = one line in `config.MODULE_CODES` + one `ModuleEntry` + one runner + a `[<code>]` TOML block; `_run_modules` itself is not edited. Per-module config lives in `settings.modules[code]` (`config.py` collects flat TOML blocks there) — no per-module properties on `Settings`; CLI-only run options are arguments, not config fields.
18. **The engine lifecycle has one owner.** Runners and post-actions never create/dispose engines by hand — use `async with postgres_session(settings) as engine` from `src/db.py` (it disposes on both success and error). `create_postgres_engine` stays the only engine factory.
19. **Grants logic exists once.** Connection-level `storage.grant_usage_on_schema` / `grant_select_on_table` (role-filtered via `POSTGRES_ROLE_EXISTS_SQL`) are the single implementation; `postgres.py`'s `grant_*_to_existing_roles` are thin engine-level wrappers. Don't resurrect unfiltered/filtered copies.
20. **Open any openpyxl workbook only through `excel_parser.load_openpyxl_workbook`** — it carries the only scoped suppression of openpyxl's "… extension is not supported and will be removed" warning. No module-level `filterwarnings` anywhere (the old sales_op import-time filter is gone). sales_op additionally captures openpyxl's "Cell … is marked as a date but the serial value … is outside the limits for dates" UserWarning (it has no file name) via `_workbook_warnings_suppressed(source_path)`: that warning and the extension warning are hidden from openpyxl's default output, the date warning is re-logged by our JSON logger with the file name/cell/serial — but only for the 1-based columns the parser actually binds (`_workbook_warnings_suppressed(..., relevant_columns=…)` is filled from `bound_columns`): a stray date-formatted cell far right of the plan block (`BE15` and the like) is counted at DEBUG, and the relevant ones aggregate into one WARNING line per file; unrelated warnings are re-emitted unchanged. Per-module read modes stay: PD loads fully (`read_only=False`), sales_op opens `read_only=True, keep_links=False`. **sales_op inspection is the one calamine exception**: `inspect_sales_op_workbook` reads the plan date via `_read_plan_date_with_calamine` (milliseconds vs ~100 ms per candidate file) and falls back to `_read_plan_date_with_openpyxl` on *any* calamine exception — never relax that to "business errors from the fast path", and keep the parse path on openpyxl (calamine trims trailing empty columns and converts number formats, which would change the "first fully empty row" rule and the cell values). Dictionary **value** frames are a separate path: pandas' native calamine engine (`excel_parser.EXCEL_ENGINE`) reads all formats; `read_single_sheet_frame` opens one `pandas.ExcelFile` handle for sheet names, the first-row probe and the frame; pyxlsb remains only for the direct sales_op spec reader (`sales_op/specification.py`). Workbook **metadata** has its own path: only `excel_parser.read_core_properties` (`docProps/core.xml` via zipfile+defusedxml) — it serves `.xlsx`/`.xlsm`/`.xlsb` alike, feeds `read_workbook_metadata` (`file_modifier`, accepting already-read `core_properties`) and the sales_op `modified_at`; openpyxl's `properties.*` is not used for metadata, and a package without a user name raises `FileMetadataMissingError`. Excel-error cells go through `ExcelErrorWarnings`: one summary log line per file/table (count + first five coordinates), while `warning_count` still counts every cell.
21. **The per-row order of `build_dictionary_payload` is frozen by tests** (`tests/test_audit_followup.py`): `where` filter → raw skip → convert → converted skip → primary-key duplicate. The body is split into `_validate_column_contract` + `_RowConverter`; refactors must keep both the order and the alias-before-checks rule (landmine 4).
22. **Two handlers, two formatters — don't unify them.** `app_logging.configure_logging` attaches a `FileHandler` with `JsonFormatter` (the machine stream: Alloy parses this file into Loki, its fields are part of the monitoring contract) and a `StreamHandler` with `ConsoleFormatter` (human text on stderr, ANSI colors only when `sys.stderr.isatty()`). The console output is never parsed; a change to the JSON schema must keep `timestamp`/`level`/`logger`/`module`/`message`/`run_id` and the matching `infra/alloy/config.alloy` stages.

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
- `docs/SALES_OP.md` — `sales_op`: month folders and CLI flags, specification, file classification, normalization, `sales_op_raw_view` and `created_at`.
- `docs/WORKFLOW.md` — environment, quality, prek, tests, languages, commits and branching.

**When behavior changes, update the relevant file in `docs/` — that's a requirement, not a wish.**
