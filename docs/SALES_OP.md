# SALES_OP (`sales_op`)

The per-client operational sales-plan module. Unlike `quotes`/`quotes_nsi`, the column structure and header mapping here are **read from an external specification on every run**, not hard-coded. Schema — `sales_op.schema` (usually `el` in development).

- Specification — `src/sales_op/specification.py` (also the source registry: codes and file masks come from the spec).
- Workbook parsing — `src/sales_op/excel_parser.py`, orchestration — `src/sales_op/runner.py`.
- The view and the month-partitioned storage — `src/sales_op/db.py`, models — `src/sales_op/models.py`.

## Source and folder layout

`sales_op.sources.data_dir` (default `data/sales_op`) contains:

- `Спецификация sales_op_raw_view.xlsb` (a required file; sheet `спека`);
- month subfolders named `YYYY-MM` (`2026-07`) with `.xlsx` plan files. The walk inside a selected month folder is **recursive**.

Source is **local only**: `source = "imap"` (or `--source imap`) fails the module with an error — IMAP attachments have no month folder. `file_pattern` defaults to `.*\.xlsx$`.

The loader never writes to `data_dir`: the Compose mount is read-only, so there is no "processed" rename and no state file. The underscore prefix is a **manual** "do not load" marker (see below).

## Month selection (`--force`, `--months`)

The unit of loading is a month folder; one run loads one or several months.

- **Default** — the current month from `common.timezone` (`settings.app_timezone`), folder `<YYYY>-<MM>`. A missing folder is an **error**.
- **`--months 2026-01,2027-02`** — a comma-separated list of month folders. Each item must match `^\d{4}-(0[1-9]|1[0-2])$`; duplicates are dropped. A missing folder is an **error**.
- **`--months all`** — every immediate `data_dir` subfolder matching the month pattern, sorted; no month folders at all is an **error**.
- **`--force`** — include files whose name starts with `_`. Without it such files are ignored at discovery. `--force` does not change month selection.
- Both flags are **sales_op-only**: with `sales_op` not selected (or not run) the CLI exits with code `2` before loading anything.

A file starting with `_` is a manual marker with no DB or filesystem bookkeeping: the next run skips it unless `--force` is given. The loader never renames files.

## Specification (`Спецификация sales_op_raw_view.xlsb`, sheet `спека`)

Field rows: cells in column 4 (`TARGET_COLUMN_INDEX = 3`) form the mapping range from `source_type` to `comments`. Each row is a target column with: DB field name (only `^[a-z_][a-z0-9_]*$`), data type, and a `Not Nullable` flag.

- Data type (column 5 (`TYPE_COLUMN_INDEX = 4`), normalized without spaces and casefolded): `date`, `datetime`, `float`, `hash`→`text`, `int`/`integer`→`int`, `text`.
- **`Not Nullable` (column 6, `NOT_NULLABLE_COLUMN_INDEX = 5`)**: an empty/falsy cell means the column is optional — it may be absent from the workbook or empty per row, and becomes `NULL`. A truthy cell (`Y`, `yes`, `да`, `true`, `1`, `+`) means the column is **required**: the workbook must contain the column and every kept row must have a value, otherwise the file fails with a named error. `ColumnDefinition.nullable = not required`.
- Duplicate target column names or an unknown type is a specification-read error (the whole module does not start, `failed_items = 1`).
- Sources are defined by the source columns starting at `FIRST_SOURCE_COLUMN_INDEX = 7` with step `SOURCE_COLUMN_STEP = 2`. For each source the spec gives: the source name (row 1), the KHD table (row 9, `el.sales_op_<code>` → the code is derived from the table name), and the file mask (row 8). A source with no file mask is **ignored**: no files are searched for and no target table is created.
- **File masks** (row 8, `SOURCE_FILE_MASK_ROW = 7`; Excel row 8): regular expressions used for file discovery. They are matched as written (case-insensitively); there is no backslash normalization. An empty mask means the source is not loaded at all.
- For each source, every specification row's column gets a mode (`SalesOpColumnMapping.mode`):
  - `plan_date` — for `doc_yearmonth` (`PLAN_DATE_COLUMN_NAME`; the date comes from the file);
  - `null` — when the expression is empty or literally `null`;
  - `constant` — when the column is in `CONSTANT_TARGETS` (`source_type`, `uom_code`) or the expression is not a string; the value is converted to the declared type;
  - `source` — otherwise, the value is a workbook column name.
  Only `source`/`plan_date` columns reach the physical table (`table_columns`); constants and `null` are expanded in `sales_op_raw_view`. A required column is always a `source`/`plan_date` column.

## File classification

A file belongs to the first source whose spec mask `fullmatch`es its name (case-insensitive). The codes come from the KHD tables in the spec:

| Code | Table | File name (mask in the spec) |
|---|---|---|
| `mfk` | `sales_op_mfk` | contains `мфк` |
| `bsk_hshp` | `sales_op_bsk_hshp` | contains `бск` and `хщп` |
| `bsk_polymers` | `sales_op_bsk_polymers` | contains `бск` and `полимеры` |
| `bsk_mth` | `sales_op_bsk_mth` | contains `бск` and `мтх` |
| `bsk_obn` | `sales_op_bsk_obn` | contains `бск` and `обн` |
| `bsk_soda` | `sales_op_bsk_soda` | contains `бск` and `сода` |

`~$...` Excel owner files are always skipped.

## File selection

Files are discovered per selected month folder and grouped by `(source, month)`:

- The plan date is the value **to the right of the marker** `План продаж :` on the first visible sheet (`_find_plan_marker`, `_parse_plan_date`; supports `datetime`, an Excel serial, strings `%Y-%m-%d`, `%d.%m.%Y`, `%d/%m/%Y`, `%Y-%m`).
- **Month check:** the plan date's year and month must equal the month folder being loaded. A mismatch is an error for that file, and the whole `(source, month)` is not loaded — the old rows stay.
- Files of one `(source, month)` are grouped by plan date; for each date the file with the latest modification time is chosen (ties broken by name). Several files for one date — a warning. Several plan dates in one month — all their rows are loaded into that month.
- No files for a source in a month — a warning (not an error).
- A file's date cannot be determined — an error; that `(source, month)` is blocked, the other sources/months keep being processed.

## Workbook parsing (`parse_sales_op_workbook`)

- The first visible sheet is used. The header row is right after the marker row; the range spans from column `Производитель` to `Комментарии` (inclusive).
- Specification columns are bound to headers by normalized name (spaces removed, casefolded). A **required** (Not Nullable) column missing from the file fails the file with an error; an **optional** missing column is left unbound and its value becomes `NULL`. If a name occurs several times, positions are used in order.
- Rows: values are converted via `convert_excel_value` (Excel errors → `NULL` + a warning); `doc_yearmonth` is filled from the plan date. Rows without `producer_code`, `seller_code` or «Наименование контрагента» (`client_name`) are skipped **before** the Not Nullable check, so a broken/blank row never trips a required-column error. The first fully empty row ends the data block: everything after it is ignored.
- For every kept row, each required (Not Nullable) column must have a non-null value, otherwise the file fails with a named error (file + field + row).
- Value normalization: outer spaces are removed, whitespace runs are collapsed, empty strings and `-` → `NULL`; types are coerced per the `Тип данных` column; errors carry the file name and cell coordinate.

## Load and view

- One transaction per `(source, month)`: create the schema/table if missing, add only missing columns, `DELETE` rows with `doc_yearmonth` in `[month_start, next_month_start)`, insert the new rows, write the source-history rows. `TRUNCATE` is never used: rows of other months survive.
- Table changes are **additive only**: existing columns are never dropped, retyped or re-nullable'd. An existing column absent from the spec gets its DB `DEFAULT` or `NULL` on insert; a legacy `NOT NULL` column without a `DEFAULT` (and not in the spec) makes the insert fail loudly — no default is invented.
- Every `sales_op_<code>` table has `created_at TIMESTAMPTZ NOT NULL DEFAULT now()` (physical metadata, not part of the specification). A legacy table gets the column added on the next load; the default fills the existing rows.
- If any file of a `(source, month)` fails (month mismatch, parse error, missing required column), that `(source, month)` is not touched at all: the `DELETE` does not run, the old rows stay. Other sources and months still load.
- `sales_op_raw_view` — `CREATE OR REPLACE VIEW` as `UNION ALL` over projections from all existing `sales_op_*` tables. Columns follow the specification: `source`/`plan_date` as is, `constant`/`null` as typed literals (`NULL::TYPE`), plus `created_at` at the end. Every column gets a `COMMENT` from the specification description (`created_at` has its own comment).
- After the load: `SELECT` grants on tables and the view, materialized-view refresh (`sales_op.mviews_to_refresh`).

## Warnings and errors

- A missing source in a month, several files for one plan date, or an unparseable cell value is a `WARNING`; warnings do not fail the run and do not block post-actions. A missing required (Not Nullable) column or an empty required value is an error.
- Errors: a missing/unreadable specification, an IMAP source, a missing month folder, an undeterminable date, a plan date outside the month folder, a missing required column or value, a table-replacement failure, a view-creation failure. They set `has_errors = true`; the remaining sources/months keep being processed.
- **openpyxl date-serial warnings.** openpyxl's `read_only=True` reader emits `UserWarning: Cell … is marked as a date but the serial value … is outside the limits for dates` (no file name) from `openpyxl.worksheet._reader` during lazy `iter_rows`. `_workbook_warnings_suppressed(source_path)` wraps the whole workbook lifecycle: it hides this warning and the "Unknown extension … will be removed" one from openpyxl's default stderr, and re-logs the date warning through our JSON logger as `План продаж: openpyxl в файле <path>: <text>` — with the file name, cell coordinate and serial value. The cell itself becomes `#VALUE!` → `NULL` via the usual excel-error path. Unrelated warnings are re-emitted unchanged; there is no module-level `filterwarnings`.
- Run failures go out as e-mail only through the Alertmanager of the monitoring stack (rule-driven); the loader itself does not mail anything.
