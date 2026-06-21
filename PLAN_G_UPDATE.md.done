# PLAN_G_UPDATE.md — Phase G: Front — code-splitting + test foundation

> Status: **todo** · Prio P2 · Effort S–M · Risk L. Spec: `SPECS_G_UPDATE.md`. Index: `PLAN_UPDATE.md`.
> When shipped, rename to `PLAN_G_UPDATE.md.done` (+ `SPECS_G_UPDATE.md.done`).

**Goal**: kill the single ~650 KB bundle and the zero-test situation (vitest is configured, unused).

- **In**: route-level `React.lazy` + `manualChunks` (isolate `@xyflow/react`+`dagre`,
  `react-virtuoso`+`anser`); vitest + Testing-Library tests for the identity components and pure
  logic (resolution provenance, `can_apply` gating in the UI); one **Playwright** happy path
  (dev-login → plan → confirm) in CI.
- **Out**: full visual-regression suite.
- **Touches**: `front/` only; CI adds a Playwright job; new dev deps `@playwright/test`,
  `@testing-library/react`.
- **Acceptance**: initial JS chunk < 250 KB gz; `task test` runs front unit tests; Playwright green.
