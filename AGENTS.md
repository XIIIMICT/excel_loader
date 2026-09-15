# AGENTS.md — excel_loader

Cheat sheet for agents. Read only the section you need; details — in `docs/*`.

## What it is

An Excel-to-PostgreSQL loader. A single CLI process (`src/main.py`) runs the selected modules, then module post-actions (grants, materialized-view refresh), common post-actions, and optionally an email with the log.

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

Default modules — all four. `--quotes-nsi`, `--quotes`, `--pd`, `--sales-op` narrow the set; `--source local|imap` overrides the source for the selected modules.

## Landmines (critical!)

1. **`.gitignore` is deny-all with a whitelist.** A new root file or folder won't reach git until `!path` is added (`!/docs/` and `!/docs/**` already exist). Nested `.gitignore` files work the same way.
2. **`README.md` is outdated** — don't rely on it unless explicitly asked.
3. **`quotes_nsi_frequencies` and `quotes_nsi_frequencies_rank` keep the source text verbatim**, including the empty string, `Null` and `-`. These are data, not SQL `NULL` markers; don't "clean" them with normal normalization (`preserve_text_values`).
4. **Aliases in `build_dictionary_payload` are applied before all checks.** `primary_key_columns`, `not_null_columns`, `not_null_filtered_columns`, `unique_columns`, `column_kinds`, `where_columns` are expressed in **target** names. A missing source column or duplicated target names is an error.
5. **Boolean text values:** after `strip().lower()` — `y`/`yes`/`да` → `true`, `n`/`no`/`нет` → `false`, the other text `false` → `false`, everything else → `true`. An empty required (NOT NULL) field → `false`, an empty nullable one → `NULL`.
6. **DDL is compatible with PostgreSQL 14.** Don't use `UNIQUE NULLS NOT DISTINCT` or any features newer than 14 until the minimum version is explicitly bumped.
7. **Parser coordinates (`xls_row`, `xls_col`) are never stored in the DB** — only inside parsing and PD deferred-package recovery.
8. **`pd_view` is created via `CREATE OR REPLACE VIEW`.** If the replace fails, create the first free fallback name with a repeated `_upd` suffix (`CREATE VIEW`) and log/send the format change as an error.
9. **Compose always mounts `./config.toml` to `/app/config.toml`.** `LOADER_CONFIG_PATH` is unnecessary. `POSTGRES_HOST` (when set) overrides `common.postgres.host` from the TOML.
10. **`src/scripts/seed.py` is the only seed script.** Shared schema/table helpers live in `src/storage.py`; don't create a new top-level `seed.py`.

## Working rules

- **Don't commit or push without an explicit request.** Before git operations — `git status` and the current branch.
- **Before delivery:** `just check` and `just test` green. Fix `ruff`/`ty` errors in code; add ignores only when a fix would hurt readability or correctness.
- **uv TLS:** verify through the system certificate store (`UV_SYSTEM_CERTS=1`) — already set in the `justfile`. Don't bring back `UV_INSECURE_HOST`, don't disable SSL verification globally; the corporate CA is mounted from `/etc/ssl/certs`.
- **Documentation is part of the change:** changed behavior/mapping/config — update `AGENTS.md` and the relevant file in `docs/`. Write landmines down immediately.
- **Minimal changes:** follow the file's style, don't add new entities without need.

## Documentation

- `docs/ARCHITECTURE.md` — the shared dictionary engine, config, sources (local/IMAP), storage and DDL, source history, post-actions, logs and email.
- `docs/QUOTES.md` — the `quotes_nsi` and `quotes` modules: registries, files, mappings, primary keys.
- `docs/PD.md` — `pd`: schema, parsing, `value_double`, `pd_view` and fallback, deferred packages.
- `docs/SALES_OP.md` — `sales_op`: specification, file classification, normalization, `sales_op_raw_view`.
- `docs/WORKFLOW.md` — environment, quality, prek, tests, languages, commits and branching.

**When behavior changes, update the relevant file in `docs/` — that's a requirement, not a wish.**
