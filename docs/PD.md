# PD (`pd`)

The production-summary module. Unlike the quote dictionaries, PD is an **incremental** loader: the asset/material/indicator dictionaries are created and extended automatically, while facts are replaced for a specific report date. Schema — `pd.schema` (usually `el` in development).

- Parsing: `src/pd/excel_parser.py`, models — `src/pd/models.py`.
- DB load and `pd_view` — `src/pd/db.py`, table schema — `src/pd/schema.py`.
- Orchestration, deferred packages — `src/pd/runner.py`, `src/pd/unsent.py`.
- Source: every `.xlsx` from `pd.sources.data_dir` (default `data/pd`), or IMAP. The seed sample — `mock_data/pd/pd.xlsx`.

## Schema

`ref_assets` (global asset dictionary; PK `id`, UNIQUE `name`), `ref_materials` (raw-material items and groups; PK `id`, UNIQUE `name`+`parent_id`, self-FK `parent_id`), `ref_indicators` (indicators; PK `id`, UNIQUE `category_1..3`+`name` and `db_name`), `pd_data` (facts; PK `loaded_for`+`ordinal_number`+`indicator_id`; FKs to the three dictionaries), `pd_reference_change_log` (audit of auto-created references and fallback views).

- `db_name` exists only on `ref_indicators`: only indicators are pivoted into `pd_view` columns. Assets and materials are addressed by `id` and unique names.
- `pd_data.ordinal_number` is computed on every parse and refers to the row of a file/date, not to `ref_materials`.
- `pd_data.value` is the raw value as text; `pd_data.value_double` is the numeric interpretation (`DOUBLE PRECISION`, added via `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`). `pd_view` pivots `value_double`.
- PD does not return `table_code`.

## Parsing (`parse_report`)

The **first sheet** of the workbook is read, in two passes: `data_only=False` (for bold font and formulas) and `data_only=True` (values). Only the first sheet is parsed — any other sheets are ignored. Constants: column `D` — ordinal number, `E` — name, values — from `F` to `AI`, report date — `I4`, indicator header rows — `6` and `7`, first possible asset row — `10`, at most 3 indicator-name levels.

- **Asset** — a row whose `E` name is non-empty, `D` has no number and the `E` name cell is bold (bold comes from the formula workbook). The asset name is stripped of surrounding quotes/«» and a tail like `(на 01.01.26)`.
- **Material/nomenclature** — rows after an asset with a non-empty `E` name. The hierarchy is derived from the number of leading spaces in the name; this yields `material_path` (e.g. `(Group, Item)`) and `parent_name` (the parent path's last element). Names `Орехо-Зуево`, `Орехово-Зуево`, `Губаха` are skipped.
- **Indicator** — a value column (`F..AI`) whose name is assembled from rows 6–7 (merged cells are expanded to the top-left value). At most 3 levels: `category_1..3` + `name`; the full name is `full_name` (` | `-joined non-empty parts). Duplicate `full_name` after normalization is an error. Columns after `AI` (the legacy tail of old 2025 workbooks) are intentionally ignored.
- **Data** — the Cartesian product of material rows and indicators; the value is normalized (empty string/`-`/Excel errors like `#...!` → `NULL`) and stored as text via `value_to_text`.

Parse errors (`ValueError`, `FileNotFoundError`) are logged and mark the module run as failed; other files keep being processed.

## DB load (`load_report_to_db`)

One transaction:

1. `_ensure_pd_storage` — schema, tables, `value_double`, `is_gross`/`is_commercial`/`product_grade` columns, grants.
2. `_resolve_report_references` — find/create assets, materials and indicators:
   - asset by name; material by the full `material_path` (parents are created along the path); indicator by `category_1..3` + `name`. New entities are written to `pd_reference_change_log` (`action=created`).
   - default material flags: `is_gross` when the name contains `вал`, `is_commercial` when it contains `товар`/`тов.`; `product_grade` — `NULL` (filled in manually).
   - the indicator `db_name` is normalized to ≤63 Postgres bytes (no transliteration, the excess is truncated, a hash suffix is added on collision).
3. `_replace_data_for_date` — `DELETE` facts for `loaded_for = report_date`, then insert the new ones.
4. `create_or_replace_pd_view` — rebuild `pd_view` (below).
5. Write to `pd_reference_change_log` (created entities + fallback view), a source-history row, `GRANT SELECT` on the view.

`has_format_changes` = whether the change log has entries (a new entity or a fallback view). Format changes are logged as `ERROR` (and included in the email) but do not stop the load.

## `pd_view` and fallback

`pd_view` pivots `pd_data` into a wide format: rows (`loaded_for`, `ordinal_number`, `asset`, `material`, `parent_material`, `loaded_at`) and one column per indicator `db_name` (`MAX(value_double) FILTER (WHERE indicator_id = ...)`), plus `material_is_gross`, `material_is_commercial`, `material_product_grade`. Columns get comments.

- The base view is created with `CREATE OR REPLACE VIEW`. If the replace fails (e.g. the new column set is incompatible), the **first free fallback name** with a repeated `_upd` suffix is created (`pd_view_upd`, `pd_view_upd_upd`, ...) via a plain `CREATE VIEW`, and the format change is written to `pd_reference_change_log` (`action=view_fallback`) and logged as an error.
- Analytics must read `pd_view` (or the current fallback name).

## Deferred packages (`unloaded_data`)

If the DB is unavailable (network/`OperationalError`/`InterfaceError` etc.), the parsed report is saved to `unloaded_data/<timestamp>_<name>/` (`manifest.json`, `assets.json`, `raw_materials.json`, `indicators.json`, `data.json`) so nothing is lost. On the next PD run the deferred packages are loaded **first** (in directory-name order), then fresh files are read from the source; a successfully loaded package is deleted.

Source-history metadata (`history_reference`, `file_modified_at`, `file_modifier`) may be missing for very old packages — then the history row is skipped with a `WARNING`.

## Seed

`just seed` (`uv run src/scripts/seed.py`) fills the PD reference data from `mock_data/pd/pd.xlsx`. This is the only seed script; shared schema/table helpers live in `src/storage.py`.
