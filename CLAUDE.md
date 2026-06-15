# CLAUDE.md — Instructions for Claude Code

> This file frames Claude Code's work on **Stackd** (code name), a self-hostable Terraform orchestration platform. Read it in full before any task, then the relevant reference document.

---

## 0. Before coding: read the right document

Four documents are authoritative. **They take precedence over this file in case of a detail conflict**, and over your own assumptions about "how we usually do things".

| Document | When to consult it |
|---|---|
| **PLAN.md** | breakdown into phases, scope, implementation order, what is out of MVP scope |
| **SPECS.md** | data model, state machine, worker protocol, API, audit, OIDC, hooks, mocks — **the technical source of truth** |
| **DESIGN.md** | everything front-end: tokens, components, screens, visual rules |
| **DEV.md** | local environment, Taskfile, seed, e2e scenario |

Rule: **do not guess a structure that is already specified**. If you write a data model, a state transition or an endpoint, open SPECS.md and reuse the existing definition instead of inventing a variant of it. If something is missing or seems contradictory, flag it explicitly rather than filling the gap silently.

---

## 1. What the product is (in two sentences)

A platform that orchestrates Terraform/OpenTofu runs (`plan` → human confirmation → `apply`) on self-hosted workers in pull mode, with multi-environment stacks, inter-environment dependencies, full audit and dynamic cloud credentials via OIDC. **The API is the only source of truth; workers are stateless and disposable.**

---

## 2. Monorepo structure

```
api/        FastAPI (Python 3.13+) — REST + WS, auth, worker API, state backend, OIDC issuer, audit
worker/     agent (Python for the whole MVP, Go rewrite post-MVP) — poll, clone, hooks, terraform, OIDC→STS
front/      React + Vite + TypeScript (SPA)
deploy/     docker-compose.dev.yml, deployment Helm/Terraform (later)
docs/       PLAN.md, SPECS.md, DESIGN.md, DEV.md
Taskfile.yml  orchestration (see DEV.md)
```

API modules (cf. SPECS §2.1): `auth/ stacks/ environments/ variable_sets/ runs/ workers/ scheduler/ audit/ oidc/ statebackend/ webhooks/ hooks/ ws/`. One module = one domain, with its routes, Pydantic schemas, and logic. No catch-all `utils/` that grows endlessly.

---

## 3. Mandated technical stack

**Do not substitute these choices without explicit agreement** (they are the result of documented decisions):

- **API**: FastAPI, Pydantic v2, SQLAlchemy 2 **async**, Alembic (migrations), PostgreSQL 18 (cf. PLAN §6 — native `uuidv7()`, async I/O). **Python package and environment management: `uv`** (not pip, not Poetry) — `uv sync`, `uv run`, `uv add`; dependencies in `pyproject.toml` + committed `uv.lock`. No Redis/broker in the MVP — the queue is Postgres via `SELECT ... FOR UPDATE SKIP LOCKED` (SPECS §7.2).
- **Front**: React 19, Vite 7, strict TypeScript, TanStack Query (server state), Tailwind v4 (`@tailwindcss/vite` plugin) + CSS tokens, re-skinned shadcn/ui, react-flow + dagre (graphs), react-virtuoso + anser (ANSI logs). Package manager: **pnpm**. Details and unblocked directions: PLAN §6.
- **Worker**: Python + watchfiles (hot reload), Docker runner in prod / local in dev.
- **Object**: S3 (Garage in dev) for tfstate, archived logs, artifacts.
- **Target IaC**: **OpenTofu first**, Terraform as a user option (reason: BUSL license — see PLAN §5).

---

## 4. Non-negotiable invariants

These rules run through all the code. Violating them breaks the security or audit model.

1. **A run's state only changes through `transition(run, to_state, actor, payload)`** (SPECS §4.2). This single function checks legality, performs the atomic update guarded on `from_state`, writes the `run_event`, the `audit_event` if the action is human or terminal, publishes to the WS and calls the hooks. Never an `UPDATE runs SET state=...` anywhere else.
2. **Every mutating action writes an `audit_event` in the SAME DB transaction** as the action (SPECS §6.3). No event bus, no "after the fact" write.
3. **Secrets are never logged nor returned in clear text.** `sensitive` variables: write-only via the API, AES-256-GCM at rest, decrypted only when building the claim payload, masked in logs by the agent (SPECS §13).
4. **Apply permission = `can_apply(user, env)`**: `role ∈ {approver, admin}` AND `env.tier ∈ user.allowed_tiers` (set membership — tiers are a configurable, non-ordered catalog, SPECS §2.4). `destroy` additionally requires `can_destroy`. This control does **not** rely on `protected` (which only forces confirmation + 4-eyes). Four-eyes comes from the tier's `requires_four_eyes` flag (fail-closed if the tier row is missing), not a hardcoded `prod`.
5. **Concurrency: a single active run per environment** (partial unique index, SPECS §3.5). Two envs of the same stack can run in parallel.
6. **A run that has consumed mocks (`used_mocks=true`) cannot be applied** unless `environment.allow_mock_apply=true` (SPECS §9.3).
7. **Sensitive outputs: never stored, never propagated** in cascades (SPECS §9.1).
8. **No browser storage in the front** (localStorage/sessionStorage) — in-memory state via TanStack Query / React state.

---

## 5. Code conventions

**Python (api, worker)**
- Formatting/lint: **ruff** (format + check), via `uv run ruff`. Typing: annotations everywhere, `mypy` in CI. Every Python command goes through `uv run` (never a direct invocation of a system python or a manually activated venv).
- Async end to end: async routes, async SQLAlchemy, no blocking I/O in the event loop.
- Pydantic schemas separated from SQLAlchemy models (never expose an ORM model directly).
- IDs: UUIDv7. Timestamps: `timestamptz` UTC, `_at` suffix. API errors: RFC 9457 (problem+json).
- Tests: pytest, testcontainers for Postgres, moto for AWS/STS. No DB mocking — real DB in tests.

**TypeScript (front)**
- Lint: eslint + the project's formatter. Strict TS, no unjustified `any`.
- Functional components + hooks. WS events **invalidate** TanStack queries (they do not patch the cache by hand), except for logs which are streamed (DESIGN §6).
- **No hard-coded color**: only CSS tokens (`--color-state-running`, etc.). Color never carries information on its own — always a label/icon in addition (DESIGN §7).
- The identity components (`PhaseRail`, `StateBadge`, `LogViewer`, `PlanDiff`, `ProvenanceBadge`, `RunActionBar`, `EnvCell`) have a Storybook story: it is their contract.

**Git / commits**
- Conventional commits (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`).
- One Alembic migration per schema change, never editing an already-merged migration.
- Commit messages in English, short and factual.

---

## 6. Development workflow

Everything goes through the **Taskfile** (see DEV.md, do not reintroduce a Makefile):

```
task dev          # full local stack (compose + migrations + seed)
task test         # pytest + vitest
task e2e          # full non-regression scenario (the functional contract)
task seed         # idempotent demo data
task reset        # start from scratch
```

- In dev, auth is done via **dev login** (3 personas: admin/alice/bob, distinct tiers) — no need for Google. Git repos as `file://` fixtures, Terraform without cloud (`local_file`, `random`). Details: DEV.md.
- Before marking a task done: `task test` passes, and if the task touches the core (runs, permissions, cascade), `task e2e` passes too.
- The `dev_auth` module is **removed from the prod build** — never make it reachable when `STACKD_ENV=production`.

---

## 7. Pitfalls specific to this project

- **`phase` is overloaded**: in the claim payload, `phase ∈ {plan, apply}` is the *job type*; in the state machine and the logs, phases are fine-grained (preparing/planning/checking/...). Do not confuse them (SPECS §7.2).
- **The state lives in S3 but Terraform talks to the API's HTTP backend**, not to S3 directly (SPECS §11). Do not suggest pointing Terraform at a native `s3` backend for `managed_state=true` envs.
- **Hooks have two sources**: platform (DB, non-bypassable) and repo (`.stackd.yml`). Security checks go on the platform side (SPECS §8).
- **`tier` is a configurable catalog, not linear** (the `tiers` table; was a fixed `dev<staging<prod` enum). A user holds a **set** of `allowed_tiers` (non-contiguous OK), and `env.tier ∈ allowed_tiers` is the gate — there is no rank/ceiling. Do not reintroduce ordering or a single `max_apply_tier`. A full per-space policy system (OPA-style) is still deferred to Phase 7.
- **Mocks**: real value > mock > explicit error. A mock only serves to bootstrap a cascade and blocks apply by default (SPECS §9.3).

---

## 8. What NOT to do

- Invent a field, a state or an endpoint when SPECS already defines one — re-read first.
- Add a heavy dependency (broker, alternative ORM, front framework) without agreement.
- Widen the MVP scope: no OPA, no SAML, no multi-IaC, no module registry (PLAN §1.2).
- Put business logic in migrations, or change a run's state outside of `transition()`.
- Log a secret, return a sensitive variable in clear text, or bypass `can_apply`.
- Reintroduce Make, browser storage, or hard-coded colors in the front.
- Finish a task without a test, or with `task e2e` broken on a task that touches the core.

---

## 9. When you are not sure

Say so. A "this part is not specified in SPECS §X, I propose Y, confirm?" is better than a silent choice that will have to be undone. The project was designed with strong inter-document coherence — preserving it matters more than moving fast on an assumption.
