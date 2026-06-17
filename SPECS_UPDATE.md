# SPECS_UPDATE.md — Post-MVP specs (index)

> Companion to `docs/SPECS.md`. One spec file per phase: `SPECS_<X>_UPDATE.md` (paired with
> `PLAN_<X>_UPDATE.md`). Folded into `docs/SPECS.md` and renamed `*.done` when the phase ships.
> Conventions unchanged: UUIDv7, `timestamptz` UTC `_at`, RFC 9457, Pydantic ≠ ORM, state only via
> `transition()`, audit in the same tx, secrets never logged/returned.

## Specs by phase
- **A** — VCS feedback loop (PAT: commit status + PR comment) → `SPECS_A_UPDATE.md`
- **B** — Drift detection → `SPECS_B_UPDATE.md`
- **C** — Security hardening (masking + runner trust) → `SPECS_C_UPDATE.md`
- **D** — HCL-syntax variables → `SPECS_D_UPDATE.md`
- **E** — Worker concurrency → `SPECS_E_UPDATE.md`
- **F** — RBAC granularity + multi-space → `SPECS_F_UPDATE.md`
- **G** — Front: splitting + tests → `SPECS_G_UPDATE.md`
- **H** — Observability + API guardrails → `SPECS_H_UPDATE.md`
- **I** — Later: not specced until promoted (see `PLAN_I_UPDATE.md`)

## Migrations introduced
| Phase | Change |
|---|---|
| A | `runs.pr_number, vcs_provider, vcs_comment_id, vcs_head_sha` |
| B | `environments.drift_status, last_drift_checked_at, drift_run_id, drift_check_enabled` |
| F | `space_memberships` table + backfill |

(C/D/E/G/H need no schema change. Head is currently **0020**; assign the next free revision when each
phase actually lands — don't pre-number.)

## Global invariants preserved
`transition()` is the only state writer; audit in the same tx; `can_apply` (role∈{approver,admin} ∧
`env.tier ∈ allowed_tiers`); one active run per env (§3.5); `SKIP LOCKED` claim (§7.2); secrets
never logged/returned; OPA-style policy stays Phase 7 in `docs/PLAN.md`.

## Decisions
Resolved: **A = PAT** (App → Phase I), one PR comment edited in place. Open: **C** tripwire
warn-vs-fail, **F** per-stack-vs-per-space scope.
