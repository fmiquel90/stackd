# SPECS_F_UPDATE.md — RBAC granularity + multi-space (Phase F)

> Plan: `PLAN_F_UPDATE.md`. Folds into `docs/SPECS.md` (§2/§6) when shipped. Highest blast radius —
> land last of A–F, behind a full `task e2e` permission pass.

## Data model
```
space_memberships
  id uuid PK, space_id FK, user_id FK,
  role enum(reader, writer, approver, admin),
  allowed_tiers text[] ,            -- per-space tier ceiling (overrides the global user.allowed_tiers)
  can_destroy bool default false,
  UNIQUE (space_id, user_id)
```
`users.role`/`allowed_tiers`/`can_destroy` become **instance defaults**; the effective permission is
the space membership when present. `can_apply(user, env)` (invariant #4) gains a space lookup:
`role∈{approver,admin}` **for that space** AND `env.tier ∈ membership.allowed_tiers`.

## Scoping
Every list/get/mutate resolves the active space from the resource (stack→space, env→stack→space) and
checks membership. The implicit `get_default_space` is removed; a migration creates one membership
per existing user for the existing space (no behaviour change for current data).

## API / front
- `GET /api/v1/spaces`, `POST /spaces` (admin), `POST/DELETE /spaces/{id}/members`.
- Front shell gains a **space switcher**; all queries key by space id.

## Migration
`space_memberships` table + backfill (one membership per existing user for the existing space).

## Open decision
Per-stack grants now, or **per-space only** for this phase (per-stack later)?

## Invariants
Four-eyes (`tiers.requires_four_eyes`, fail-closed) and the tier-as-set model (§2.4) unchanged — just
scoped per space. Full OPA policy engine stays Phase 7 in `docs/PLAN.md`.
