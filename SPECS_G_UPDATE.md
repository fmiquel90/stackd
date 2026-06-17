# SPECS_G_UPDATE.md — Front: splitting + tests (Phase G)

> Plan: `PLAN_G_UPDATE.md`. Folds into `docs/DESIGN.md` (§8) when shipped. No schema change.

- **Bundle**: route-level `React.lazy`/`Suspense`; Vite `build.rollupOptions.output.manualChunks`
  isolating `@xyflow/react`+`dagre` (graph) and `react-virtuoso`+`anser` (logs). Target initial
  chunk **< 250 KB gz** (today ~200 KB gz in one ~650 KB file).
- **Tests** (vitest is a dep, currently unused): Testing-Library unit tests for the identity
  components (`PhaseRail`, `StateBadge`, `PlanDiff`, `ProvenanceBadge`) and pure logic (variable
  resolution provenance, `can_apply` gating in the UI). One **Playwright** happy path (dev-login →
  trigger plan → confirm) as a CI job against `task dev`.
- New dev deps: `@playwright/test`, `@testing-library/react`. No product behaviour change.
