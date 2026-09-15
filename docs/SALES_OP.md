# SALES_OP (`sales_op`)

The per-client operational sales-plan module. Unlike `quotes`/`quotes_nsi`, the column structure and header mapping here are **read from an external specification on every run**, not hard-coded. Schema — `sales_op.schema` (usually `el` in development).

- Specification — `src/sales_op/specification.py`, file classification — `src/sales_op/registry.py`.
- Workbook parsing — `src/sales_op/excel_parser.py`, orchestration — `src/sales_op/runner.py`.
- The view — `src/sales_op/db.py`, models — `src/sales_op/models.py`.

## Source and folder layout

`sales_op.sources.data_dir` (default `data/sales_op`) contains:

- `Спецификация sales_op_raw_view.xlsb` (a required file; sheet `спека`);
- month subfolders with `.xlsx` plan files. The local walk is **recursive** (`recursive_local=True`).

Source — `local` or IMAP (`file_pattern` defaults to `.*\.xlsx$`).

## Specification (`Спецификация sales_op_raw_view.xlsb`, sheet `спека`)

- Cells in column 4 (`TARGET_COLUMN_INDEX = 3`) form the mapping range from `source_type` to `comments`. Each row is a target column: DB field name (only `^[a-z_][a-z0-9_]*$`), description, data type.
- Data type (column 5 (`TYPE_COLUMN_INDEX = 4`), normalized without spaces and casefolded): `date`, `datetime`, `float`, `hash`→`text`, `int`/`integer`→`int`, `text`.
- `source_type`, `doc_yearmonth`, `producer_code`, `seller_code` are NOT NULL; the rest are nullable.
- Duplicate target column names or an unknown type is a specification-read error (the whole module does not start, `failed_items = 1`).
- Sources (`mfk`, `bsk_hshp`, `bsk_polymers`, `bsk_mth`, `bsk_obn`, `bsk_soda`) are looked up in the sheet's first row by required name parts (`SOURCE_DEFINITIONS`, `required_name_parts`). A missing or duplicated source is an error.
- For each source, every specification row's column gets a mode (`SalesOpColumnMapping.mode`):
  - `plan_date` — for `doc_yearmonth` (the date comes from the file);
  - `null` — when the expression is empty or literally `null`;
  - `constant` — when the column is in `CONSTANT_TARGETS` (`source_type`, `uom_code`) or the expression is not a string; the value is converted to the declared type;
  - `source` — otherwise, the value is a workbook column name.
  Only `source`/`plan_date` columns reach the physical table (`table_columns`); constants and `null` are expanded in `sales_op_raw_view`.

## File classification

`SalesOpSourceDefinition.matches_file_name` — `fullmatch` of the file regex (case-insensitive). Required name parts:

| Code | Table | File name |
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
- Specification columns are bound to headers by normalized name (spaces removed, casefolded). A column missing from the file does not stop the load: its value → `NULL`, a warning. If a name occurs several times, positions are used in order.
- Rows: values are converted via `convert_excel_value` (Excel errors → `NULL` + a warning); `doc_yearmonth` is filled from the plan date. Rows without `producer_code` or `seller_code` are skipped.
- Value normalization: outer spaces are removed, whitespace runs are collapsed, empty strings and `-` → `NULL`; types are coerced per the `Тип данных` column; errors carry the file name and cell coordinate.

## Load and view

- Before the load, `sales_op_raw_view` is dropped (`DROP VIEW IF EXISTS`).
- For each type with recognized files, a `sales_op_<code>` table is created/extended; rows from all selected files of one type accumulate and **replace** the table's data once per run. The physical table is created without a PK.
- `sales_op_raw_view` — a `CREATE VIEW` as `UNION ALL` over projections from all existing `sales_op_*` tables. Columns follow the specification: `source`/`plan_date` as is, `constant`/`null` as typed literals (`NULL::TYPE`). Every column gets a `COMMENT` from the specification description.
- After the load: `SELECT` grants on tables and the view, materialized-view refresh (`sales_op.mviews_to_refresh`).

## Warnings and errors

- A missing file type, a required column, or an unparseable row is a `WARNING`; warnings do not fail the run and do not block post-actions.
- Errors: a missing/unreadable specification, a classification conflict, an undeterminable date, a table-replacement failure, a view-creation failure. They set `has_errors = true`, but the remaining types keep being processed.
- Only JSONL records with level `WARNING`, `ERROR`, `CRITICAL` are emailed.
