# SPECS_E_UPDATE.md — Worker concurrency (Phase E)

> Plan: `PLAN_E_UPDATE.md`. Folds into `docs/SPECS.md` (§7) when shipped. No schema change.

## Config
```
STACKD_MAX_CONCURRENT_JOBS = 1   (worker; today's behaviour)
```

## Claim loop (`worker/agent/main.py`)
- The poll loop maintains up to `MAX_CONCURRENT_JOBS` in-flight jobs, each on its own thread (the
  heartbeat thread is already independent). It claims while it has a free slot, long-polls otherwise.
- `claim` stays one-run-at-a-time on the wire; the API's `SELECT … FOR UPDATE SKIP LOCKED` (§7.2)
  and the one-active-run-per-env partial unique index (§3.5) already make concurrent claims safe.
- Heartbeat reports `busy`/`idle` from the in-flight count; the worker may advertise `capacity` so
  the scheduler/queue can reason about it (optional heartbeat field).

## Invariants
One active run per environment is unchanged — concurrency is **across environments** only. State
backend locking (§11.2) already guards same-env races.
