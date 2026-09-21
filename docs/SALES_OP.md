# SALES_OP (`sales_op`)

The per-client operational sales-plan module. Unlike `quotes`/`quotes_nsi`, the column structure and header mapping here are **read from an external specification on every run**, not hard-coded. Schema — `sales_op.schema` (usually `el` in development).

- Specification — `src/sales_op/specification.py` (also the source registry: codes and file masks come from the spec).
- Workbook parsing — `src/sales_op/excel_parser.py`, orchestration — `src/sales_op/runner.py`.
- The view — `src/sales_op/db.py`, models — `src/sales_op/models.py`.

## Source and folder layout

`sales_op.sources.data_dir` (default `data/sales_op`) contains:

- `Спецификация sales_op_raw_view.xlsb` (a required file; sheet `спека`);
- month subfolders with `.xlsx` plan files. The local walk is **recursive** (`recursive_local=True`).

Source — `local` or IMAP (`file_pattern` defaults to `.*\.xlsx$`).

## Specification (`Спецификация sales_op_raw_view.xlsb`, sheet `спека`)

Field rows: cells in column 4 (`TARGET_COLUMN_INDEX = 3`) form the mapping range from `source_type` to `comments`. Each row is a target column with: DB field name (only `^[a-z_][a-z0-9_]*$`), data type, and a `Not Nullable` flag.

- Data type (column 5 (`TYPE_COLUMN_INDEX = 4`), normalized without spaces and casefolded): `date`, `datetime`, `float`, `hash`→`text`, `int`/`integer`→`int`, `text`.
- **`Not Nullable` (column 6, `NOT_NULLABLE_COLUMN_INDEX = 5`)**: an empty/falsy cell means the column is optional — it may be absent from the workbook or empty per row, and becomes `NULL`. A truthy cell (`Y`, `yes`, `да`, `true`, `1`, `+`) means the column is **required**: the workbook must contain the column and every kept row must have a value, otherwise the file fails with a named error. `ColumnDefinition.nullable = not required`.
- Duplicate target column names or an unknown type is a specification-read error (the whole module does not start, `failed_items = 1`).
- Sources are defined by the source columns starting at `FIRST_SOURCE_COLUMN_INDEX = 7` with step `SOURCE_COLUMN_STEP = 2`. For each source the spec gives: the source name (row 1), the KHD table (row 9, `el.sales_op_<code>` → the code is derived from the table name), and the file mask (row 8). A source with no file mask is **ignored**: no files are searched for and no target table is created.
- **File masks** (row 8, `SOURCE_FILE_MASK_ROW = 7`; Excel row 8): regular expressions used for file discovery. They are matched as written (case-insensitively); there is no backslash normalization. An empty mask means the source is not loaded at all.
- For each source, every specification row's column gets a mode (`SalesOpColumnMapping.mode`):
  - `plan_date` — for `doc_yearmonth` (the date comes from the file);
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

## File selection

Per run, one file is loaded per type/plan date:

- The plan date is the value **to the right of the marker** `План продаж :` on the first visible sheet (`_find_plan_marker`, `_parse_plan_date`; supports `datetime`, an Excel serial, strings `%Y-%m-%d`, `%d.%m.%Y`, `%d/%m/%Y`, `%Y-%m`).
- Files are grouped by type, then by plan date; for each date the file with the latest modification time is chosen (ties broken by name). Several files for one date — a warning.
- No files for a type — a warning (not an error).
- A file's date cannot be determined — an error for that file; the rest keep being processed.
- Files are not renamed and get no markers in their name.

## Workbook parsing (`parse_sales_op_workbook`)

- The first visible sheet is used. The header row is right after the marker row; the range spans from column `Производитель` to `Комментарии` (inclusive).
- Specification columns are bound to headers by normalized name (spaces removed, casefolded). A **required** (Not Nullable) column missing from the file fails the file with an error; an **optional** missing column is left unbound and its value becomes `NULL`. If a name occurs several times, positions are used in order.
- Rows: values are converted via `convert_excel_value` (Excel errors → `NULL` + a warning); `doc_yearmonth` is filled from the plan date. Rows without `producer_code` or `seller_code` are skipped **before** the Not Nullable check, so a broken/blank row never trips a required-column error.
- For every kept row, each required (Not Nullable) column must have a non-null value, otherwise the file fails with a named error (file + field + row).
- Value normalization: outer spaces are removed, whitespace runs are collapsed, empty strings and `-` → `NULL`; types are coerced per the `Тип данных` column; errors carry the file name and cell coordinate.

## Load and view

- Before the load, `sales_op_raw_view` is dropped (`DROP VIEW IF EXISTS`).
- For each type with recognized files, a `sales_op_<code>` table is created/extended; rows from all selected files of one type accumulate and **replace** the table's data once per run. The physical table is created without a PK.
- `sales_op_raw_view` — a `CREATE VIEW` as `UNION ALL` over projections from all existing `sales_op_*` tables. Columns follow the specification: `source`/`plan_date` as is, `constant`/`null` as typed literals (`NULL::TYPE`). Every column gets a `COMMENT` from the specification description.
- After the load: `SELECT` grants on tables and the view, materialized-view refresh (`sales_op.mviews_to_refresh`).

## Warnings and errors

- A missing file type or an unparseable row is a `WARNING`; warnings do not fail the run and do not block post-actions. A missing required (Not Nullable) column or an empty required value is an error.
- Errors: a missing/unreadable specification, an undeterminable date, a missing required column or value, a table-replacement failure, a view-creation failure. They set `has_errors = true`, but the remaining types keep being processed.
- Only JSONL records with level `WARNING`, `ERROR`, `CRITICAL` are emailed.
