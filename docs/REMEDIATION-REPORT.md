# Backend remediation — final report: excel_loader (2026-11-27)

Scope: only the confirmed audit findings in `backend-remediation-brief`. Base `1568a46`;
final local `dev` = `f59beba`. All task branches merged with `--no-ff`; nothing pushed;
no production data or remote settings other than branch protection (cross-repo, below).

## Phase 0 — research (`a447866`)
`docs/REMEDIATION-PHASE0-RESEARCH.md`: single root AGENTS.md, no ADRs; five `el_loader_*`
gauges with a `module` label; CI had **no** `--cov-fail-under`; **no** real-PostgreSQL
integration tests; one PG18 instance (`el-postgres-data`) with no automated backup.
Two documentation inconsistencies were recorded (env-override denial in
`docs/ARCHITECTURE.md`, stale `manual` prek stage in `docs/WORKFLOW.md`).

## P0-1 — branch protection (cross-repo, applied remotely)
Research contradicted the brief: `main` exists and was unprotected in EL, but RC and DM
have **no `main`** (default `dev`). Per owner decision, `main` was created in RC/DM from
the then-current remote `dev` and protected in all three repos. Exact settings, verified
via `gh api repos/XIIIMICT/<repo>/branches/main/protection`:
`CI required` as the sole required check, `strict: true`, 1 approving review, dismiss
stale approvals, `enforce_admins: true`, force pushes and deletions disabled.
Residual: `dev` remains the default branch in RC/DM and is **not** protected; a PR to
`dev` does not hit this gate. EL's default is `main`, so enforcement is effective there.
A live "direct push rejected" test was not performed (SSH remote unavailable); settings
were verified via the API instead.

## P0-4 — coverage gate + real-PostgreSQL smoke (`f798e75`)
**Decision (re-planned, not implemented as briefed):** do NOT set
`--cov-fail-under=80`. Measured baseline 57.70% with `fail_under=40`; research showed the
valuable blind spot was the entirely untested PD parser, while DB/SQL paths are better
exercised by a real database. Reaching 80% would require covering ~73% of all missing
statements, mostly DB/IMAP/defensive glue — percentage-chasing, which the owner rejected.
Implemented instead:
- per-PR `postgres-smoke` job: health-checked `postgres:18-alpine`, disposable schema
  `el_smoke`, loader end-to-end on a small checked-in fixture; asserts exact counts,
  second-run idempotency via monthly partition replacement, surviving neighbour month
  and surviving legacy column. Runtime ~4 s, so it stays per-PR.
- 99 meaningful unit tests for `pd/excel_parser.py` (15%→93%), `pd/unsent.py`
  (27%→100%), local `source_resolver.py` paths and `sales_op/runner.py` pure edges.

**Measured:** coverage 57.70% → **67.76%**; `fail_under` 40 → **65** (measured ratchet,
not an aspiration). Negative proof: changing `EXPECTED_PD_ROWS` 2730→2729 failed the
smoke test, then was restored. Full nightly run including the integration job measures
**77.97%**.

## P1-7 — per-stage duration metrics (`3a8982e`)
New histogram `el_loader_stage_duration_seconds` (+bucket/sum/count/created), labels
`module` (bounded set) and `stage` (`parse`, `parse_sheet`, `validate`, `load`, `commit`);
no path/date/sheet labels. Pushed in the same request as the existing five gauges, no new
env var, endpoint or dependency. Two Grafana panels and alert `ExcelLoaderStageP95Slow`
(p95 > 60 s / 10 m) landed with contract docs in the same change; drift check passes.
Before: no stage timing at all. After: in-process demonstration captured all five stage
series from the real 2730-row workbook. Live Pushgateway scrape and `load`/`commit`
transaction timing on a real DB run were **not** exercised locally.

## P1-9 — nightly heavy suite (`f59beba`)
`03:47 UTC` daily + `workflow_dispatch`; not part of `CI required`. Jobs: full coverage
(integration included), report-only `pip-audit`, report-only dependency freshness,
production-image build + `--help` smoke. Artifacts retained 14 days. No CI→Alertmanager
wiring exists (no repository webhooks; SMTP carries Prometheus alerts only) — the exact
missing wiring is documented rather than invented. Local validation: actionlint 0,
`just check` green (283 passed, 1 skipped, 67.58%), nightly command with a disposable PG
gave 284 passed / 77.97%, `pip-audit` 0 findings, 5 outdated packages reported, prod image
and smoke green. A scheduled night was not observed (no push); no night is claimed green.

## P2 triggers — NOT TRIGGERED
| Item | Trigger | Status |
|---|---|---|
| P2-3 OpenTelemetry | cross-service latency debugging need | not yet triggered; no measured cross-service bottleneck |
| P2-4 SLOs / error budgets | baseline + weeks of monitoring data | not yet triggered; no monitoring history exists |
| Broker (section 7) | EL has no broker design; no T1/T2 condition | no broker introduced |

## Remaining OPEN / owner gates
- RPO/RTO for EL's PostgreSQL are assumed (≤24 h), not agreed with an operator; off-host
  backup copy and a real restore rehearsal for EL are not implemented (P1-8 was scoped to
  RC/DM).
- The two doc inconsistencies found in Phase 0 are recorded but were left unchanged (out
  of the audit's fix list).
- Real Pushgateway/Grafana/Alertmanager delivery and target-host rehearsals were not run.

## Runbooks / docs updated
`docs/REMEDIATION-PHASE0-RESEARCH.md`, `docs/WORKFLOW.md`, `docs/ARCHITECTURE.md`,
`AGENTS.md`. No new runtime dependency, no weakening of gates, no pushes.
