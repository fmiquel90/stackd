# PLAN_UPDATE.md — Post-MVP plan (index)

> Companion to `docs/PLAN.md`. The detail lives in one file per phase: `PLAN_<X>_UPDATE.md` (plan) +
> `SPECS_<X>_UPDATE.md` (spec). **When a phase ships, rename both its files to `*.done`** and flip
> its Status here. Same invariants as `CLAUDE.md` (state via `transition()`, audit in tx, no secret
> in logs, `can_apply`). Each phase is shippable alone and gated by `task test` + `task e2e`.

## Why this exists
MVP is architecturally sound; the gaps that block adoption / enterprise trust, in order: (1) lives
beside Git not inside it (no PR feedback); (2) no drift detection; (3) security sharp edges
(masking residual, untrusted repo code); (4) correctness/UX (HCL vars, worker throughput); (5)
scale (per-stack RBAC, multi-space, observability).

## Phases

| Phase | Theme | Prio | Effort | Risk | Status | Files |
|---|---|---|---|---|---|---|
| A | VCS feedback (PR comment + commit status, **PAT**) | P1 | M | M | **shipped** | `PLAN_A_UPDATE.md.done` · `SPECS_A_UPDATE.md.done` (folded into `docs/SPECS.md §18`) |
| B | Drift detection | P1 | S–M | L | **shipped** | `PLAN_B_UPDATE.md.done` · `SPECS_B_UPDATE.md.done` (folded into `docs/SPECS.md §19`) |
| C | Security hardening (masking + runner trust) | P1 | M | M | **shipped** | `PLAN_C_UPDATE.md.done` · `SPECS_C_UPDATE.md.done` (folded into `docs/SPECS.md §5.1/§7.4/§8.3`) |
| D | HCL-syntax tfvars | P2 | S | L | **shipped** | `PLAN_D_UPDATE.md.done` · `SPECS_D_UPDATE.md.done` (folded into `docs/SPECS.md §3.4`) |
| E | Worker concurrency | P2 | M | M | todo | `PLAN_E_UPDATE.md` · `SPECS_E_UPDATE.md` |
| F | RBAC granularity + multi-space | P2 | L | M | todo | `PLAN_F_UPDATE.md` · `SPECS_F_UPDATE.md` |
| G | Front: splitting + tests | P2 | S–M | L | todo | `PLAN_G_UPDATE.md` · `SPECS_G_UPDATE.md` |
| H | Observability + API guardrails | P3 | M | L | todo | `PLAN_H_UPDATE.md` · `SPECS_H_UPDATE.md` |
| I | Later (registry, run-tasks, SSO, GitHub App) | P3 | L | — | backlog | `PLAN_I_UPDATE.md` (no spec yet) |

Effort: S ≈ 1–2 d, M ≈ 3–5 d, L ≈ 1–2 wk (single dev).

## Recommended sequence
```
A ─┐
B ─┼─ ship together (the "now part of the workflow" story)
C ─┘  (security runs in parallel — different files)
D, G  quick wins, anytime (low risk, parallelizable)
E     after C (same worker loop)
F     last of the P1/P2 block (highest blast radius)
H     ongoing / before any real prod exposure
```

## Decisions
- **Resolved — Phase A: PAT** (the stack's `repo_secret`), commit Status API + PR comment. GitHub
  App (bot identity + Checks API) deferred to Phase I.
- **Resolved — Phase A: one comment edited in place** (not append).
- **Resolved — Phase C**: cleartext tripwire = **warn by default**, configurable to hard-fail via
  `STACKD_LEAK_TRIPWIRE=fail` (fail aborts on plan only; apply is always warn-only post-change).
- **Open — Phase F**: per-stack grants now, or per-space only (per-stack later).

## Cross-cutting rules
- A migration per schema change (never edit a merged one); `task e2e` green when a phase touches
  runs/permissions/state; on ship, fold the phase's spec into the authoritative `docs/SPECS.md`/
  `DESIGN.md` and rename its `_UPDATE` files to `*.done`.
- New heavy deps are noted in the phase file (H: `prometheus-client`, `opentelemetry-*`; G:
  `@playwright/test`, `@testing-library/react`).
