# Architecture (shared engine, config, sources, storage)

An Excel-to-PostgreSQL loader: the `el-loader` container (`compose.yaml`, `Dockerfile`) runs `python src/main.py` once, replaces the target tables' data, then exits. PostgreSQL and Loki are external services; they are started inside this project only for local debugging (`just db-standalone`, `just logging-standalone`).

```
loader (src/main.py)
  ├── load_settings            config.toml + POSTGRES_HOST
  ├── _run_modules             quotes_nsi → quotes → pd → sales_op
  ├── _run_post_actions        grants + REFRESH MATERIALIZED VIEW (per module)
  ├── _run_common_post_actions [[common.post_actions]] by run outcome
  └── _push_run_outcome_metrics run gauges to the Pushgateway (opt-in)
        │
        ├── PostgreSQL (asyncpg/SQLAlchemy) — target tables + el.excel_loader_sources_history
        └── sources: local (data_dir) or imap (email attachments)
```

## Execution order and exit code

`src/main.py`:

1. `load_settings()` reads `config.toml`, applies CLI overrides (`--source`).
2. `configure_logging()` creates `logs/<timestamp>.log` (JSON Lines).
3. Modules run in a fixed order: `quotes_nsi`, `quotes`, `pd`, `sales_op` (`_selected_modules`). With no flags — all four.
4. Module post-actions: `USAGE` on the schema, `SELECT` on tables/relations, `REFRESH MATERIALIZED VIEW`. A module with errors (`has_errors`) skips its post-actions.
5. Common post-actions (`[[common.post_actions]]`) run against the run outcome from `_module_run_outcome`: `success`, `partial_success` or `failure`; each action is selected by its own `run_on` (`always` also counts).
6. Run metrics are pushed to the Pushgateway when `METRICS_PUSHGATEWAY_URL` is set (see "Logs, metrics and alerts"). The loader itself sends no e-mail: error reports are delivered by the Alertmanager of the monitoring stack, which reacts to the pushed metrics — if the stack is unreachable, the failed push is logged (`ERROR`) to the run log file and no e-mail goes out.

Exit code `0` only when no module has errors/format changes and no post-action failed. Otherwise `1`; a settings-read failure — `2`.

## Configuration (`config.toml`)

`config.py`. TOML, `extra="forbid"`: an unknown key is a validation error. Relative paths (`log_dir`, `unloaded_data_dir`, `mock_data_dir`, `data_dir`, post-action `path`) resolve against the project root. The file is read as `utf-8-sig` (BOM tolerated).

- `[common]`: `log_dir`, `unloaded_data_dir`, `mock_data_dir`, `timezone` (empty/`null` → system), `[common.postgres]` (`host`/`port`/`user`/`password`/`db`/`schema`), `[common.imap]` (`host`/`port`/`user`/`password`/`mailbox`/`scan_depth`), `[[common.post_actions]]` (`path`, `run_on`).
- Module blocks `[quotes_nsi]`, `[quotes]`, `[pd]`, `[sales_op]`: `schema`, `mviews_to_refresh`, `users_to_grant`, `[<module>.sources]`.
- `sources`: `data_dir` (required), `source = "local" | "imap"`, `file_pattern`, `imap_subject_pattern`. Regexes compile with `IGNORECASE`; their syntax is validated first.
- `mviews_to_refresh` — names `name` or `schema.name`; without a schema the module's `schema` is prepended. Duplicates and more than two parts are errors.
- External-service validation: if any module reads from IMAP, `common.imap.host/user/password` and that module's `imap_subject_pattern` are required.
- Environment override: `POSTGRES_HOST` (when set) replaces `common.postgres.host`. There are no other env overrides.
- In Compose `./config.toml` is always mounted to `/app/config.toml`; `compose.yaml` sets `POSTGRES_HOST` (default `host.docker.internal`). `LOADER_CONFIG_PATH` is unused.

## Sources (local / IMAP)

`source_resolver.py`. Three modes:

- `resolve_source` — a single file: locally `data_dir / path`, or the first matching IMAP attachment.
- `resolve_all_pattern_sources` — all files of a folder by `file_pattern` (pattern loaders); locally `allow_empty=True` (empty → no `SourceFile`).
- `resolve_pattern_sources` — the same as above but without `allow_empty`.

Local mode: `data_dir` must exist, files are matched with `fullmatch` against `file_pattern` (default from the loader) and sorted by name (casefold). A missing required file is a `SourceError`.

IMAP mode: `IMAP4_SSL`, `common.imap.*`, the last `scan_depth` messages are scanned (in reverse); the subject is checked by `imap_subject_pattern.search`, attachments by `file_pattern.fullmatch` against the decoded name. Attachments are downloaded into a temp directory; `SourceFile.history_reference` for them is `imap/<original_name>`, else the file path. `SourceAuthError` — connection/authentication problems.

"Not found" rules:
- a single dictionary — `WARNING` and skip (`warning_files`), not an error;
- a pattern loader with no files — `WARNING` and skip;
- a combined dictionary: missing parts are skipped with a warning; if no file is found at all — skip.

## Shared dictionary engine

`dictionaries/base.py`, `dictionary_runner.py`. Three loader kinds, registries in `src/dictionaries/<module>_registry.py`:

| Kind | Meaning |
|---|---|
| `DictionaryLoader` | one file → one table |
| `CombinedDictionaryLoader` | a fixed set of files → one table (parts accumulate) |
| `PatternDictionaryLoader` | all folder files by pattern → one table |

Value frames are read through the pandas-native calamine engine (`excel_parser.EXCEL_ENGINE`; `python-calamine` reads `.xlsx`/`.xlsb` with the same values as the previous openpyxl/pyxlsb engines, while the pyxlsb engine is deprecated in pandas). openpyxl stays for workbook metadata (`read_workbook_metadata`: properties for `.xlsx`, `docProps/core.xml` for `.xlsb`), for PD (fonts, `data_only=False`) and sales_op (`properties.modified`); the sales_op spec reads via the direct pyxlsb API — see docs/AUDIT.md, the calamine assessment.

Each loader builds a `DictionaryPayload` via `build_dictionary_payload` (`excel_parser.py`): `table_reference`, `selected_frame`, `primary_key_columns`, `metadata` and optionally:

- `not_null_columns` — columns that cannot be `NULL` (in addition to the PK);
- `not_null_filtered_columns` — rows where **all** these columns are empty are skipped (after conversion; before — by raw text);
- `unique_columns` — duplicate-value check (single column; a repeat is an error with both coordinates);
- `column_kinds` — column type, default `text`;
- `column_aliases` — `{source_name: target_name}`; applied **before** all checks, so PK/NOT NULL/UNIQUE/`where`/`column_kinds` are expressed in target names; a missing source column or duplicated target names is an error;
- `where_columns` — `{column: expected_value}`; rows without a match (after `normalize_text_value`) are skipped;
- `column_converters` — `{column: callable}` instead of normal conversion (the callable itself is responsible for nullability);
- `dropped_columns` — columns not written to the table but used by `where`/`not_null_filtered`;
- `preserve_text_values` — for `text` columns keep the source string as is (no `strip`, no `-`/empty → `NULL`) — needed where the empty string and `-` are data.

Per-row order: `where` filter → skip a fully empty `not_null_filtered` row → convert each cell (converter or `convert_excel_value`) → skip `not_null_filtered` by converted values → PK duplicate check. The first PK/UNIQUE/required-field violation aborts the table load with an error and a cell coordinate.

After the payload is built: `inspect_storage` → `prepare_storage` when needed → `replace_table_rows`.

### Value conversion (`excel_parser.convert_excel_value`)

- Excel errors (`#NULL!`, `#DIV/0!`, `#VALUE!`, `#REF!`, `#NAME?`, `#NUM!`, `#N/A`, `#GETTING_DATA`) → `NULL` + `WARNING` (counted in `warning_count`).
- `text`: basic normalization (`strip`, whitespace collapse), empty string and `-` → `NULL`; `preserve_text` disables this.
- `bool`: `bool` as is; a number → `!= 0`; empty/`None` → `NULL` (nullable) or `false` (NOT NULL); then the general rule below.
- `int`/`float`: spaces removed, `,` → `.`; a non-integer for `int` is an error.
- `date`/`datetime`: `datetime`/`date`/`pd.Timestamp` as is; a number — an Excel serial from `1899-12-30`; a string — `pd.to_datetime(dayfirst=True)`. Failure — `CellTypeConversionError` with a coordinate.
- Boolean text values: after `strip().lower()` — `y`/`yes`/`да` → `true`, `n`/`no`/`нет` → `false`, the other text `false` → `false`, everything else → `true`.
- General text normalization: `None`/`NaN` → `None`; whitespace collapsed; empty string and `-` → `None`.

### Column normalization in the dictionary engine

`dictionary_runner._normalize_payload_columns`: for columns in `columns_to_normalize` the value is converted to `capitalize` or `uppercase` (per `normalization_styles`), and the marker `(н/д)` → `NULL`. This is a separate pass on top of normal conversion.

### Cell coordinates and sheet names in errors

Errors and warnings must point at the real cell in the source workbook, not at a column position after selection/renaming. Two mechanisms carry that:

- `read_single_sheet_frame` attaches a `FrameOrigin` (`excel_parser.frame_origin`) to the frame via `DataFrame.attrs`: the sheet name, the 1-based header row and the original Excel letter of every column. `DataFrame.attrs` survives `loc`-slicing, `copy`, `rename` and `concat`, so `select_columns_between`/`select_columns_excluding_checks` keep the real letters. `build_dictionary_payload` uses the origin to compute `"<sheet>!<letter><row>"`; aliases re-key the origin letters so target names resolve too.
- Builders that concatenate several files or drop rows (`baiinfo`, `sci`, `argus_pdf`) pass `cell_locations` — a per-row mapping of column name to the original `"<sheet>!<cell>"` reference, built with `excel_parser.build_cell_locations`. These take precedence over the frame origin.

`build_dictionary_payload` threads the file (`source_name`) and the sheet into `CellTypeConversionError`, `RequiredCellValueError`, `DuplicateCellValueError` and `DuplicatePrimaryKeyError`, so a message reads like `quotes_nsi_argus_fc_exception.xlsb: dic!A2`. Coordinates are for diagnostics only — never stored in the database.

## Storage and DDL

`schema_definition.py`, `storage.py`, `postgres.py`.

- Types by `ColumnKind`: `text` → `TEXT`, `bool` → `BOOLEAN`, `date` → `DATE`, `datetime` → `TIMESTAMP`, `timestamp_tz` → `TIMESTAMPTZ`, `float` → `DOUBLE PRECISION`, `int` → `INTEGER`.
- `prepare_storage`: create the schema (`CREATE SCHEMA IF NOT EXISTS`), grant `USAGE` and `SELECT` to existing roles (`users_to_grant`), create the table if missing. The grants run through the shared connection-level helpers `grant_usage_on_schema` / `grant_select_on_table` (role-filtered via `POSTGRES_ROLE_EXISTS_SQL` in `storage.py`); table creation is an unconditional idempotent `create_all`.
- `replace_table_rows`: in one transaction `TRUNCATE` the target table → `align_table_structure` → insert rows → write source-history rows. Target tables are **replaced wholesale**, not appended to.
- `align_table_structure`: add missing columns, change type (`USING cast`), fix nullability, create/verify the PK, create missing single-column `UNIQUE` constraints; drop "retired" columns (`user_name`, `date_upload`, `date_modified`, plus `source_report_name` for `quotes_nsi_sources`).
- A mismatch between the existing PK and the expected one, or a PK where none is expected, is a `DatabaseOperationError`. A PK `IntegrityError` during insert is a `DuplicatePrimaryKeyError`.
- DDL must stay compatible with PostgreSQL 14: no `UNIQUE NULLS NOT DISTINCT` or features newer than 14.

## Source history

`source_history.py`. One row per successfully loaded file in `el.excel_loader_sources_history`: `loaded_at`, `src_file`, `dest_table`, `file_modified_at`, `file_modifier`. `file_modifier` is the workbook's `lastModifiedBy`/`creator`; for `.xlsb` it is read from `docProps/core.xml`. The table is created/extended on every run; the retired `module` column is dropped. File metadata is not duplicated in the target tables.

## Post-actions, grants and materialized views

- `users_to_grant`: `USAGE` on the schema + `SELECT` on loaded tables and, for PD/sales_op, the analytic view. All grants go through the same `storage.py` helpers (engine-level wrappers in `postgres.py`); missing roles are skipped with a log entry, not treated as an error.
- `mviews_to_refresh`: `REFRESH MATERIALIZED VIEW` when the view exists; otherwise skip with a log entry. A refresh failure is a failed module post-action.
- `[[common.post_actions]]`: an arbitrary SQL file (`path`) executed after the module post-actions against the run outcome (`run_on`: `success`/`partial_success`/`always`). Example — `sql/post_actions/assign_object_owner.sql`.

## Logs, metrics and alerts

- `app_logging.py` writes JSON Lines to `logs/<timestamp>.log` (one file per run); the module code is attached to the log (`set_log_module`). At startup the folder is pruned: `.log` files older than `LOG_RETENTION_DAYS = 14` are removed (the folder grows unboundedly otherwise).
- The loader sends no e-mail of its own: run reports and error notifications are delivered by the Alertmanager of the monitoring stack (see below). Its own SMTP path (aiosmtplib) was removed together with the `common.smtp` config keys.
- `compose.yaml` forwards logs to `LOKI_URL` via Alloy; `infra/` holds the Alloy/Loki/Grafana/Prometheus/Alertmanager configs. The external service must be reachable at `LOKI_URL` (for example via a published host port).
- Loki keeps logs 7 days: `infra/loki/config.yaml` sets `retention_period: 168h` **and** enables the `compactor` — without the compactor the retention setting is inert and chunks are never deleted.
- `infra/alloy/config.alloy` promotes `level`/`module` into labels and keeps `run_id` plus `exception` as Loki structured metadata (queryable, but not labels — a label per run would explode the index). Trace a run end to end: `{job="excel_loader"} | run_id="<run_id>"`.

### Metrics (opt-in, Pushgateway)

The loader is a batch CLI: it exits, so Prometheus cannot scrape it. When
`METRICS_PUSHGATEWAY_URL` is set (empty default in `compose.yaml`), the run
pushes (via `src/metrics.py`) gauges to the Pushgateway
(`infra/compose.monitoring.yaml`, profile `standalone`): `el_loader_run_status`
(0 success / 1 partial_success / 2 failure), `el_loader_run_duration_seconds`,
`el_loader_run_timestamp_seconds`, `el_loader_run_last_success_seconds` (pushed
only on success — the Pushgateway keeps the previous value, which is what makes
"no successful run for N hours" detectable) and `el_loader_module_status{module}`
(1 failed) for the selected modules. Each push replaces the previous series
(single grouping key), Prometheus (15 s scrape, 15-day retention) keeps the run
history. Push failures are logged ERROR and never change the exit code.

### Alerts and dashboards

- `infra/prometheus/alert-rules.yml`: `ExcelLoaderRunFailed`, `ExcelLoaderLastRunTooOld`, `ExcelLoaderNoSuccessfulRun` (all thresholds assume **daily** runs, 26 h) and `PushgatewayDown` / `PrometheusSelfScrapeDown` for the stack itself. Alertmanager (`infra/alertmanager/`, port 9093) groups them (30 s wait, 4 h repeat) and delivers e-mail; its config is **rendered from `.env` at container start** — edit `ALERT_SMTP_*`/`SUPPORT_MAIL`, then restart the stack. Smarthost must be 587 (STARTTLS); `ALERT_SMTP_USER` must equal the sender mailbox.
- Grafana (`infra/grafana/`): Loki datasource + Prometheus datasource + the provisioned dashboard `excel-loader-overview` (run status, duration, age of the last successful run, per-module statuses, warnings heatmap, warning/error log panel). Dashboards come from git — edit the JSON, not the browser.
- `just monitoring` starts the whole observability stack; `just logging` / `just logging-standalone` remain log-only.
