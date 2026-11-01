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

## Follow-up (November 2026) — extensibility and best-practices

Read after the roadmap above. These items complement P1–P3 with a focus on
"how does a fifth module land" and on dependency/tooling hygiene. Where an
item overlaps with A1–A11, the overlap is called out and the item is scoped to
what the audit did not already say.

### E1. Loader kinds have no shared contract

`DictionaryLoader`, `CombinedDictionaryLoader`, `PatternDictionaryLoader`
(`src/dictionaries/base.py`) are three unrelated frozen dataclasses. The runner
distinguishes them by iterating three separate tuples and calling three
separate `_process_*` functions. There is no protocol that says "a loader
resolves sources and builds a payload", so the only way to add a fourth loader
kind is to copy a `_process_*` function and teach `run_dictionaries` about a
new tuple.
**Status: closed by A1** (see "Implementation log"): the A1 refactor
introduced that protocol (`_LoaderStrategy`) and the three existing kinds
became thin adapters over it; a new kind is one adapter + registration, with
no orchestrator edit.

### E2. The CLI is hardcoded to four modules

`main._build_parser`, `_selected_modules` and `_apply_cli_overrides` each list
`quotes_nsi` / `quotes` / `pd` / `sales_op` by name. The A2 refactor collapses
the *dispatch* (`_run_modules`) into a registry, but the CLI surface still
needs a per-module flag added by hand.
**Done (November 2026):** `MODULE_CODES = ("quotes_nsi", "quotes", "pd",
"sales_op")` is now the single declared code list; `--module <code>`
(repeatable, argparse `choices=MODULE_CODES`) unions with the legacy
`--quotes-nsi`/`--quotes`/`--pd`/`--sales-op` flags, and no explicit selection
means all modules. `--source` overrides are applied in one generic loop over
the selected codes. The legacy flags stay for existing scripts; the contract
tests live in `tests/test_main_contracts.py`. `config.Settings` and
`_validate_imap_settings` (`src/config.py`) still enumerate the four modules —
see A8/E3.

### E3. `config.Settings` is a closed four-field shape

`Settings` declares `quotes_nsi`, `quotes`, `pd`, `sales_op` as four separate
`ModuleConfig` fields; `_validate_imap_settings` and the CLI override code
enumerate them again. A `modules: dict[str, ModuleConfig]` (ordered, validated)
would let a new module be a TOML block + one line, with no Python edits. This
is the A8 "settings surface" item made concrete: it is optional today, but it
is the real blocker for "add module #5 without touching shared internals".
Out of scope for this pass; flagged for the next refactor round.

### E4. `sales_op` installs a process-wide warning filter at import time

`src/sales_op/excel_parser.py` calls `filterwarnings(...)` at module top level
to silence the openpyxl "Unknown extension… xlsb" warning. This is hidden
global state: any other module imported afterwards inherits the filter, and
`pytest`'s `filterwarnings = ["error", ...]` (`pyproject.toml`) interacts
with it in order-dependent ways. A7 already moves sheet-reading toward the
shared `excel_parser`; the warning filter should move with it — ideally into a
short `with warnings.catch_warnings():` block around the openpyxl call (the PD
parser already does this in `_load_workbook`), not a module-level `filterwarnings`.
The calamine migration in the dictionary path (see below) removes the warning
at its source for `.xlsb` reads there.

### E5. `requires-python` is pinned to a single minor

`pyproject.toml` has `requires-python = "==3.15.*"`. This blocks `uv` from
resolving on 3.13/3.14 (which the rest of the stack supports) and makes CI
fail the moment CPython ships 3.16. A floor with a ceiling — `>=3.13,<4` — is
safer for a batch loader with no 3.15-only language feature in use. Verify the
`Self` import and PEP 695 `type` statements (both 3.12+) are the highest
version requirement before widening.
**Decision (November 2026): the pin stays** — `Dockerfile` builds on
`PYTHON_VERSION=3.15`, the dev environment is 3.15.0, and `uv sync --locked`
resolves identically across local, CI and image. Revisit only together with
the Python bump; do not widen it in an unrelated change.

### E6. `excel_parser.build_dictionary_payload` is still the risk hotspot

A6 calls this out (one ~150-line function with three `noqa` complexity
suppressions). It remains the single largest blocker to confident refactoring
of the dictionary path: every loader kind, every column option and every
per-row rule flows through it. Splitting it into `_validate_column_contract`,
`_convert_row` and the payload assembler — with a frozen per-row-order test —
is the highest-value P2 item and should land before any further column-option
is added.
**Status: closed by A6** — the split landed, the per-row order
(`where` → raw skip → convert → converted skip → PK duplicate) is frozen by
`tests/test_audit_followup.py` and guarded by landmine 21 in `AGENTS.md`.

## openpyxl → python-calamine assessment

**Verdict: a full migration is not possible; a partial, performance-only
migration of the dictionary value-reading path is feasible but does not drop
either dependency.**

Probed against `python-calamine==0.9.0` over the project's `mock_data/`
workbooks (`.xlsx`, `.xlsb`):

| Capability needed by this project | openpyxl/pyxlsb | python-calamine 0.9 | Blocks migration? |
|---|---|---|---|
| Read `.xlsx` cell values | openpyxl | yes (`engine="calamine"`) | no |
| Read `.xlsb` cell values | pyxlsb | yes (`engine="calamine"`) | no |
| Sheet names + visibility | openpyxl `sheet_state` | `sheets_metadata[].visible` (`SheetVisibleEnum`) | no |
| Merged-cell ranges | `worksheet.merged_cells.ranges` | `CalamineSheet.merged_cell_ranges` | no |
| Workbook `lastModifiedBy` / `creator` (source history) | openpyxl `properties` / manual `docProps/core.xml` for xlsb | **not exposed** (`metadata`/`properties` absent on `CalamineWorkbook`) | **yes — for metadata** |
| Cell formatting (`font.bold` for PD asset-row detection) | openpyxl | **not exposed** (value-only reader) | **yes — for PD** |
| Formula text vs cached value (PD `formula_workbook`) | openpyxl `data_only=False` | only cached values | **yes — for PD** |
| `workbook.properties.modified` (sales_op `_workbook_modified_at`) | openpyxl | **not exposed** | **yes — for sales_op** |
| Pandas engine integration | openpyxl / pyxlsb | `pd.read_excel(engine="calamine")` (both formats) | no |

Concrete consequences for each call site:

- `src/excel_parser.read_single_sheet_frame`, `read_sheet_names`,
  `_first_used_column_index` and the `.xlsb` reads in
  `src/dictionaries/quotes/baiinfo.py`, `argus_pdf.py`, `sci.py` could switch
  to `engine="calamine"`, replacing **both** the openpyxl (`.xlsx`) and
  pyxlsb (`.xlsb`) engines with one. This is the only worthwhile win: a single
  faster engine for the shared dictionary read path, and the
  `Unknown extension… xlsb` warning disappears there because openpyxl is no
  longer opening `.xlsb` files.
- `src/excel_parser._read_openpyxl_user_name` (xlsx metadata) and
  `_read_xlsb_user_name` (xlsb metadata via `docProps/core.xml`) **must stay**
  — calamine does not expose `creator`/`lastModifiedBy`. The xlsx branch could
  be rewritten as a manual `ZipFile`+`core.xml` parse (mirroring the xlsb
  branch) if the goal is to stop loading openpyxl just for metadata, but that
  is a separate cleanup.
- `src/pd/excel_parser.py` **must keep openpyxl**: it reads
  `formula_sheet.cell(...).font.bold` (`_is_asset_row`) and opens a
  `data_only=False` workbook. Calamine is a value-only reader with no
  formatting access.
- `src/sales_op/excel_parser.py` **must keep openpyxl**: it reads
  `workbook.properties.modified` (`_workbook_modified_at`). Its value reading
  could in principle move to calamine, but loading two engines per file for a
  single extra field is not worth it.
- `src/sales_op/specification.py` reads the `.xlsb` spec via `pyxlsb`; this
  could move to calamine, but the spec is read once per run, so the perf gain
  is negligible and the dependency is not dropped.

Because PD and sales_op cannot drop openpyxl, and the metadata path cannot
either, **`openpyxl` and `pyxlsb` cannot be removed from `pyproject.toml`**
even after a partial migration. The migration is therefore a perf/clarity win
in the dictionary hot path, not a dependency reduction. Recommended sequencing
(after the P1/P2 refactor): introduce a private `_read_frame(path, sheet,
engine="calamine")` helper in `excel_parser.py`, swap the dictionary-path
reads to it, keep the per-format metadata readers, and leave PD/sales_op on
openpyxl. Add `python-calamine>=0.9` to `pyproject.toml` only at that point.

## P1 implementation log (this pass)

The first-priority items from the roadmap were applied as follows. `just
check` and `just test` are green after the changes; details in the report that
accompanies the archive.

- **A1 — consolidate the triplicated loader pipeline.**
  `src/dictionary_runner.py`'s three near-identical functions
  (`_process_dictionary_loader`, `_process_combined_loader`,
  `_process_pattern_loader`) are replaced by one orchestrator
  `_run_loader_strategy` plus a small `_LoaderStrategy` protocol and three
  adapters (`_SingleLoaderStrategy`, `_CombinedLoaderStrategy`,
  `_PatternLoaderStrategy`). The shared body — `build_dictionary_table` →
  `inspect_storage` → `prepare_storage` → `replace_table_rows` → the
  three-branch error handling and success/warning logging — lives once in
  `_persist_payload`. The documented per-kind quirks (single-file metadata
  log, combined missing-part warnings, pattern no-files warning) stay in the
  adapters. A new loader kind is one adapter + registration.

- **A2 — `ModuleOutcome` + module registry.**
  `src/module_models.py` defines `ModuleOutcome` (the runner result: errors,
  warnings, format changes, tables and relations to grant on). Each runner
  (`run_dictionaries`, `run_pd`, `run_sales_op`) now returns `ModuleOutcome`
  directly and owns the construction of its `tables_to_grant` /
  `relation_references_to_grant` (PD builds them from `build_pd_tables` /
  `pd_view_reference`; sales_op from its loaded tables + view). `main._run_modules`
  dispatches over a `_module_entries(settings)` registry (`ModuleEntry` =
  code + static post-action config + runner) instead of an `if module ==`
  chain. Adding module #5 is one `ModuleEntry` + one runner; `_run_modules`
  itself no longer needs editing.

- **A3 — exception subclasses instead of `str` matching.**
  `SourceFileNotFoundError` is moved from `business_errors` (where it was a
  `BusinessLoadError`) to `source_resolver` as a `SourceError` subclass, and
  is raised by `_resolve_folder_source` instead of the generic
  `SourceError("файл не найден: …")`. The orchestrator catches
  `except SourceFileNotFoundError` **before** `except SourceError`, so the
  "missing file → WARNING + skip" path is now type-driven, not
  `if "файл не найден" in str(error)`. `excel_parser.read_workbook_metadata`
  and the `baiinfo` / `argus_pdf` part readers import the exception from its
  new home; a file that vanishes between resolve and read is now a WARNING
  (consistent with resolve-time missing), where previously it was logged as
  ERROR via the `BusinessLoadError` path.

- **A4 — one engine lifecycle owner.**
  `db.postgres_session(settings, *, database=None, isolation_level=None)`
  is an async context manager that creates one engine, yields it and disposes
  it on both normal and exception paths. Every hand-rolled
  `engine = create_postgres_engine(...); try/finally: await engine.dispose()`
  is replaced by `async with postgres_session(settings) as engine:`: the two
  post-action walkers in `main`, `dictionary_runner.run_dictionaries`,
  `pd.runner.run_pd`, `sales_op.runner.run_sales_op` and the PD seed script.
  `create_postgres_engine` stays the single place that builds an engine;
  per-run scoping is preserved and the cleanup path exists once.

- **A5 — grants semantics exist once.**
  The connection-level helpers `grant_usage_on_schema` /
  `grant_select_on_table` plus the shared `POSTGRES_ROLE_EXISTS_SQL` constant
  live in `storage.py` (`postgres.py` already imports `storage`; the reverse
  import would be a cycle). `postgres.py` keeps the engine-level wrappers
  `grant_select_on_table_to_existing_roles` /
  `grant_usage_on_schema_to_existing_roles`, but they now delegate to the
  storage helpers, so the `pg_roles` filtering cannot drift between storage
  preparation and post-actions. The dead trailing
  `if table_exists: return` in `_ensure_table_exists` is removed —
  `create_all` runs unconditionally and is idempotent.

- **A6 — the payload hotspot is split and the row order is frozen.**
  `build_dictionary_payload` keeps its keyword-only signature (stable API for
  the five loader kinds; kept under `noqa: PLR0913`) but is now assembled
  from `_validate_column_contract` — every configured column must exist among
  the already-aliased target columns — and a `_RowConverter` state object
  (excel-error warning counting, custom converters, required-null deferral,
  unique-column duplicates, then the primary-key duplicate check). The caller
  keeps the surrounding per-row order: ``where`` filter → raw skip → convert
  → converted skip → primary-key duplicate. That order is locked by targeted
  tests in `tests/test_audit_followup.py`
  (`test_build_dictionary_payload_row_order_is_frozen`,
  `test_build_dictionary_payload_converted_skip_drops_excel_error_cell_as_warning`).

- **A7/E4 — one openpyxl entry point, no import-time warning filter.**
  `excel_parser.load_openpyxl_workbook(source_path, *, data_only,
  read_only=False, keep_links=True)` opens any workbook inside a scoped
  `catch_warnings` block that silences openpyxl's "… extension is not
  supported and will be removed" `UserWarning`. PD's private `_load_workbook`
  (and its `OPENPYXL_UNSUPPORTED_EXTENSION_WARNING` constant) are gone;
  `read_workbook_metadata._read_openpyxl_user_name` goes through the same
  helper instead of the previously unsuppressed direct `load_workbook`.
  sales_op keeps its parsing semantics (`read_only=True`, `keep_links=False`,
  first visible sheet) and loses its module-level `filterwarnings` (E4) —
  global state on import is no longer installed. Parsing behaviors themselves
  are untouched (first-sheet-only, SCI blank-row stop, deferred packages).

- **A9 — exit-code/outcome matrix tests for `_run_app`.**
  `tests/test_main_contracts.py` (7 cases): all-success → exit 0 / pushed
  `success`; warnings alone → exit 0; module error → exit 1 / `partial_success`;
  all failed → exit 1 / `failure`; format change → exit 1 +
  `partial_success`; module post-action failure → exit 1 +
  `partial_success` (the pushed outcome includes post-action failures via
  `_module_run_outcome`); common post-action failure → exit 1. The module
  registry and the DB/metrics seams are monkeypatched, so no PostgreSQL/IMAP
  is needed. Two notes captured while writing them: the pushed run-level
  outcome is computed from module runs and module post-actions only — a failed
  *common* post-action does not change the pushed `outcome` (existing
  behavior, deliberately not frozen in the test); and
  `_push_run_outcome_metrics`' `duration_seconds` is keyword-only — test fakes
  must keep the parameter name.

- **A10 — coverage floor at 40%, branch-aware.**
  `[tool.coverage.report] fail_under = 40` + `[tool.coverage.run] branch =\n  true` in `pyproject.toml`; `just test` runs `pytest --cov=src`, and the CI
  test job (which already collects coverage) picks the floor up via the same
  config. It is a regression floor, not a target: parsing/config stays well
  above it, while DB-layer code (`dictionary_runner`, `pd/db`, `storage`,
  `source_resolver` …) sits near 15–25% because it is only exercised against
  real PostgreSQL. The owner set 40 instead of the originally proposed 50 so a
  future module does not fail the floor on arrival. The tests added to get
  past the floor were contract tests (`config` validation,
  `postgres_session` lifecycle, `app_logging`, `_run_app` matrix), reviewed to
  assert behavior rather than chase the number.

- **E5 — decision: keep `requires-python = "==3.15.*"`.**
  The audit suggested widening to `>=3.13,<4`; the owner decided otherwise:
  production runs on `PYTHON_VERSION=3.15` (Dockerfile), the dev environment
  is 3.15.0, and the pin guarantees `uv sync --locked` resolves identically
  locally, in CI and in the image. Recorded here so the widening is not
  re-applied by a future pass; revisit together with the Python version bump.

- **A11 — README retired in favor of AGENTS.md + docs/.**
  The (already untracked and absent) `README.md` is not resurrected: the
  project entry point is `AGENTS.md`, details live in `docs/*`. The stale
  `!/README.md` whitelist entry is removed from `.gitignore` (deny-all with
  whitelist), and landmine 2 now documents the absence so no future pass
  recreates the file by accident.

- **Calamine — dictionary value-reading path moved to `engine="calamine"`.**
  Per the assessment above: all value frames in the dictionary path are now
  read through the pandas-native calamine engine — `excel_parser`
  (`read_single_sheet_frame`, `read_sheet_names`, `_resolved_sheet_name`,
  `_first_used_column_index`, exported as the `EXCEL_ENGINE` constant) plus
  the direct `pd.read_excel`/`ExcelFile` calls in `quotes/baiinfo.py`,
  `quotes/argus_pdf.py` and `quotes/sci.py`; the private `_excel_engine` is
  gone. `keep_default_na=False, na_filter=False` semantics are unchanged.
  Value parity versus openpyxl/pyxlsb was probed over `mock_data/`, and the
  whole parsing suite (103 tests reading the real mocks) confirms it.
  Metadata is untouched: openpyxl (`_read_openpyxl_user_name`) for `.xlsx`,
  `docProps/core.xml` (`_read_xlsb_user_name`) for `.xlsb`; PD and sales_op
  keep their openpyxl formatting/property reads. The calamine `nrows=1`
  frame describes the whole used range (wider than the previous per-row
  trim) — harmless, because `_first_used_column_index` scans left for the
  first non-empty cell. `sales_op/specification.py` stays on the direct
  pyxlsb API (read once per run): python-calamine applies number-format
  conversions (e.g. dates→datetime), which could silently change the values
  the spec parser matches on. Therefore neither `openpyxl` nor `pyxlsb` is
  removed from `pyproject.toml`; the pandas exposure to the deprecated pyxlsb
  engine is gone.

Items **not** done in this pass: A8 (settings-surface trims), E1–E3, E6.
