# PLAN_E_UPDATE.md — Phase E: Worker concurrency

> Status: **todo** · Prio P2 · Effort M · Risk M. Spec: `SPECS_E_UPDATE.md`. Index: `PLAN_UPDATE.md`.
> When shipped, rename to `PLAN_E_UPDATE.md.done` (+ `SPECS_E_UPDATE.md.done`).

**Goal**: a worker runs several jobs at once (throughput) without breaking the per-env single-active
invariant.

- **In**: `STACKD_MAX_CONCURRENT_JOBS` (default 1 = today); the poll loop dispatches each job to a
  bounded thread pool (heartbeat already independent). One active run per env stays enforced by the
  partial unique index (§3.5) + `SKIP LOCKED` (§7.2) — concurrency is across *different* envs.
- **Out**: cancelling a running job mid-flight (separate `cancel_job` command, later).
- **Touches**: `worker/agent/main.py` (loop), heartbeat capacity reporting.
- **Acceptance**: e2e variant — 2 envs + 1 worker, `MAX_CONCURRENT_JOBS=2` → both plan in parallel;
  busy/idle reflects the in-flight count.
