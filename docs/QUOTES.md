# QUOTES (`quotes_nsi` and `quotes`)

Two quote-dictionary modules. Registries: `src/dictionaries/quotes_nsi_registry.py` (flag `--quotes-nsi`) and `src/dictionaries/quotes_registry.py` (flag `--quotes`). All target tables live in the `quotes_nsi.schema` / `quotes.schema` schema (usually `el` in development). One file (or set of files) replaces its table wholesale per run.

## `quotes_nsi` — quotes reference data

The `DICTIONARIES` list, each entry is one file from `data_dir` (default `data/`):

| Table | File | PK | Notes |
|---|---|---|---|
| `quotes_nsi_sources` | `quotes_nsi/quotes_nsi_sources.xlsx`, sheet `dic` | `source_code` | columns `source_code`, `source_fullname`, `source_shortname`; all NOT NULL, `source_code`/`source_fullname` UNIQUE; `dest_table` is absent from the file and the engine adds it as a nullable UNIQUE column (always `NULL` on load); `select_columns_between` is not used — the three columns are taken by name |
| `quotes_nsi_prod_versions` | `quotes_nsi/quotes_nsi_prod_versions.xlsb` | `prod_version`, `source_code` | columns from `prod_version` to `source_code`; `inserted` — `datetime`, `is_top_raw_quotes` — `bool` NOT NULL; `nomenclature_en`, `basis` (uppercase), `departure_location_name_en`, `location_name_en`, `spot_contract_flag` are normalized |
| `quotes_nsi_nomenclature` | `quotes_nsi/quotes_nsi_nomenclature.xlsb` | `nomenclature_en` | columns excluding `check*`; `inserted` — `datetime`; `nomenclature_en`, `nomenclative_group_en` are normalized |
| `quotes_nsi_nomenclative_groups` | `quotes_nsi/quotes_nsi_nomenclative_groups.xlsb` | `nomenclative_group_en` | columns excluding `check*`; `nomenclative_group_en` is normalized |
| `quotes_nsi_units` | `quotes_nsi/quotes_nsi_units.xlsx` | `unit` | — |
| `quotes_nsi_locations` | `quotes_nsi/quotes_nsi_locations.xlsb` | `location_name_en` | `location_name_en` is normalized |
| `quotes_nsi_frequencies` | `quotes_nsi/quotes_nsi_frequencies.xlsx` | `frequency_name_id` | `preserve_text_values=True` (empty string and `-` are data) |
| `quotes_nsi_frequencies_rank` | `quotes_nsi/quotes_nsi_frequencies_rank.xlsx` | `frequency_name_ru` | `preserve_text_values=True`; `frequency_rank`, `rank` — `int` |
| `quotes_nsi_core_products` | `quotes_nsi/quotes_nsi_core_products.xlsb` | `source_code`, `prod_version` | columns from `source_code` to `rep_currency`; `rank` — `int` |
| `quotes_nsi_argus_fc_exception` | `quotes_nsi/quotes_nsi_argus_fc_exception.xlsb` | `source_code`, `prod_version` | columns from `source_code` to `is_change_scenario_to_fact`; `is_change_report_date_to_pubdate` and `is_change_scenario_to_fact` — `bool` |

`select_columns_between` (start/end by name) and `select_columns_excluding_checks` (drop columns whose name starts with `check`) are `excel_parser.py` helpers.

## `quotes` — quotes

The registry has three groups:

### `DICTIONARIES` (single files)

- **`quotes_cheminsight_p`** — `quotes/ChemInsight/quotes_cheminsight_p.xlsb`. Columns from `rep_date` to `prod_version`. PK `rep_date` + `prod_version`; `prod_version` NOT NULL; rows where both `min_price` and `max_price` are empty are skipped; `rep_date`, `min_price`, `avg_price`, `max_price` — `float`/`date`.
- **`quotes_excel_daily`** — `quotes/Daily Quotes/Ежедневные котировки.xlsb`, sheet `base` only, replaces `raw.quotes_excel_daily` (note the `raw` schema, not `el`). Aliases `Дата` → `pub_date`, `Продукт` → `prod_version`, `Цена` → `avg_price`; no PK; `pub_date` — `date`, `avg_price` — `float`.

### `COMBINED_DICTIONARIES` (several fixed files → one table)

- **`quotes_excel_sci`** — `quotes/SCI/SCI СКИ.XLSX` + `quotes/SCI/SCI СКС_new.XLSX`. In each file, sheets are selected by patterns (the set depends on the file name: zeon/этилен-3 and east china). The header is located by the names `Дата`, `Товар`, `Марка`, `CIF China, $/т`; a row with any of the date, product or CIF price empty is skipped (monthly/aggregate rows keep only calculated prices and a month name instead of a date), while the first row where all three are empty ends the data block — everything after it (repeated data, totals) is ignored, even if other columns still hold values. `prod_version` = "product + brand" (without the market suffix) + ` CIF China, USD/т`. PK `pub_date` + `prod_version` + `product_id`, all key fields NOT NULL; a PK repeat is an integrity error that cancels the table load. Aliases `Дата` → `pub_date`, `CIF China, $/т` → `avg_price`; the `Товар`+`Марка` concatenation stays in the builder.

### `PATTERN_DICTIONARIES` (all folder files → one table)

- **`quotes_excel_baiinfo`** — files `Baiinfo.<Product>.xlsb` from `quotes/Baiinfo/` (5 products: Меламин, Метанол, Параформальдегид, Пентаэритритол, Уротропин). The first sheet is read, the header is located by `Date`/`Latest Price`/`Unit`; dates look like `04-Jan-22`. `prod_version` is a constant set from the file name; aliases `Date` → `pub_date`, `Latest Price` → `avg_price`; rows with `Unit != USD/T` are skipped, the `Unit` column never reaches the table (`dropped_columns`). PK `pub_date` + `prod_version`.
- **`quotes_excel_argus_pdf`** — files `Argus PDF*.xlsb` from `quotes/Argus PDF/`. The header is located by `rep_date`/`prod_version`/`min_price`/`avg_price`/`max_price`; `rep_date` is an Excel serial (epoch `1899-12-30`); `min_price`/`max_price` — `INTEGER`, `avg_price` — `DOUBLE PRECISION` (fractional prices are kept), `avg_price` is required. PK `rep_date` + `prod_version`.

## How the engine treats them

Single files — `DictionaryLoader`, combined — `CombinedDictionaryLoader`, patterns — `PatternDictionaryLoader` (`dictionaries/base.py`). Build/validation logic lives in `build_dictionary_payload`; see `docs/ARCHITECTURE.md`. For `quotes` the module also passes `combined_loaders` and `pattern_loaders`; for `quotes_nsi` — only `dictionary_loaders`.

Column normalization from `columns_to_normalize` (`capitalize`, `basis` — `uppercase`) and `preserve_text_values` are described in `docs/ARCHITECTURE.md`.
