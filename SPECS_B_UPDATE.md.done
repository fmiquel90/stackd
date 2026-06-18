# SPECS_B_UPDATE.md — Drift detection (Phase B)

> Plan: `PLAN_B_UPDATE.md`. Folds into `docs/SPECS.md` when shipped.

## Data model
```
environments  (add)
  drift_status          text  default 'unknown'  -- unknown | in_sync | drifted | error
  last_drift_checked_at timestamptz null
  drift_run_id          uuid null                -- the proposed run that last detected drift
  drift_check_enabled   bool default true
config (env)
  STACKD_DRIFT_INTERVAL_SECONDS = 21600  (6h)     -- per-env minimum spacing
```
New notification/inbox kind: `drift_detected`.

## Scheduler task (`scheduler/tasks.py`, advisory-locked like the others)
A `detect_drift` task on the 10s loop, gated by `last_drift_checked_at + interval`: for each
`drift_check_enabled` env with no active run, enqueue a **read-only proposed run** (`RunType.proposed`,
`plan -refresh-only`). On completion:
- plan has changes → `drift_status='drifted'`, set `drift_run_id`, emit `drift_detected` **once** per
  transition into drift (debounced — no repeat while it stays drifted);
- no changes → `in_sync`; plan errored → `error`.
A successful **apply** sets `in_sync` and clears `drift_run_id`.

## Worker
No new job type — reuse the `proposed` plan with **`-refresh-only`** (true drift = state vs reality,
not state vs code); record only the summary, no artifacts. To keep drift runs behind user runs, the
claim query (§7.2) needs a priority/order-by (a `priority` column or "user runs first") — small but
real, not free with today's FIFO `SKIP LOCKED`.

## Front
A `drift` chip on `/stacks` env cells and the env header (DESIGN §3.2: neutral colour + label, not a
state colour). A "show drifted" filter.

## Migration
`environments.drift_status, last_drift_checked_at, drift_run_id, drift_check_enabled`.

## Invariants
Drift runs are read-only; **never auto-apply**. One active run per env unchanged — a drift run is
skipped when a run is already active.
