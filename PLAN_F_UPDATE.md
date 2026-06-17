# PLAN_F_UPDATE.md — Phase F: RBAC granularity + real multi-space

> Status: **todo** · Prio P2 · Effort L · Risk M (highest blast radius — do last of A–F).
> Spec: `SPECS_F_UPDATE.md`. Index: `PLAN_UPDATE.md`.
> When shipped, rename to `PLAN_F_UPDATE.md.done` (+ `SPECS_F_UPDATE.md.done`).

**Goal**: move from space-wide role+tier to per-space grants, and wire spaces end-to-end (drop the
implicit `get_default_space`).

- **In**: a `space_memberships` table (user × space × role + per-space tier ceiling + can_destroy);
  space scoping on every list/mutation; space switcher in the UI; migration backfilling the existing
  single space.
- **Out**: full OPA policy engine (stays Phase 7 in `docs/PLAN.md`); per-resource ACLs beyond
  stack/space (per-stack grants → open decision).
- **Touches**: `auth/deps.py`, every router's scoping, `spaces/`, front shell, migrations.
- **Acceptance**: a user in space X can't see/mutate space Y; tier ceiling enforced per space; full
  `task e2e` permission pass green.
