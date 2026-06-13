# PLAN.md — Terraform orchestration platform (Spacelift-like, simplified version)

> Project code name: **Stackd** (placeholder, to be renamed)
> Goal: a self-hostable Terraform orchestration platform with multi-environment stacks, variable sets, runs, workers, hooks, dynamic cloud credentials (OIDC), full audit, and inter-stack dependencies.

---

## 1. Vision and scope

### 1.1 What we are building (MVP)

A platform that allows you to:

1. Declare **stacks**: a Git repo + a subfolder = a unit of infrastructure, broken down into **environments** (dev, staging, prod), each with their own Terraform state and variables.
2. Factor out configuration via **variable sets**: reusable sets of variables attachable to N stacks/environments (inspired by Spacelift Contexts / HCP Variable Sets).
3. Trigger **runs** (plan / apply) per environment, manually or via Git webhook.
4. Execute these runs on remote **workers** (self-hosted agents, pull model) with **live-streamed logs** in the UI.
5. Customize the lifecycle via **hooks** (commands before/after init/plan/apply, declared in YAML) with blocking or soft-fail checks.
6. Provide **dynamic cloud credentials via OIDC**: the platform signs an identity token per run, exchanged for an IAM role — zero static credentials, neither on the platform side nor on the worker side.
7. **Audit**: who triggered, confirmed, applied what, when, on which environment — an immutable and queryable trail.
8. Manage **states in S3**, exposed to Terraform via the platform's **HTTP backend** (locking + scoped tokens), with a "bring your own S3 backend" mode.
9. Manage a **dependency graph** between environments with output propagation and **mock outputs** for bootstrapping (inspired by Terragrunt).
10. Authenticate with a **Google account** (OIDC), restricted to the organization's domain.

### 1.2 What we are NOT building in the MVP (anti-scope)

| Out of MVP scope | Reason | Future phase |
|---|---|---|
| OPA/Rego policies | Hooks + checks cover 80% of v1 governance needs | Phase 7+ |
| HTTP run tasks (blocking external webhooks like Infracost SaaS) | Command hooks are sufficient (Infracost CLI runs as a hook) | Phase 7 |
| Scheduled drift detection | Requires a robust scheduler | Phase 7+ |
| Multi-IaC (Pulumi, CloudFormation) | Terraform/OpenTofu only | Maybe never |
| SAML SSO / IdPs other than Google | Google OIDC is sufficient | Phase 7 |
| Public/shared worker pool | Private workers only | Never (self-hosted positioning) |
| OIDC to GCP/Azure | AWS first (target audience), generic interface planned | Phase 7 |
| Private module registry, no-code provisioning | Large undertaking, low value without users | — |
| SIEM export of the audit | DB audit + UI are sufficient | Phase 7 |

### 1.3 Guiding principles

- **Pull, not push**: workers pull jobs from the API.
- **The API is the single source of truth**: stateless and disposable workers.
- **Explicit state machine** for runs: each transition is a persisted event → the foundation of the audit system.
- **Auditability by construction**: every mutating action writes an immutable audit event.
- **Environment = execution unit**: stack = template (repo + code), environment = instance (state + variables + protections). A run belongs to an environment.
- **Zero static credentials as a goal**: OIDC for humans (Google), OIDC for workloads (per-run IAM roles). Static secrets remain possible (sensitive variable sets) but are the fallback, not the norm.
- **Layered configuration**: variable set → stack → environment, each layer able to override the previous one. Same logic for hooks.
- **Minimal blast radius**: one environment = one state = one scope. Dependencies via explicit outputs only.

---

## 2. Target architecture (overview)

```
┌─────────────┐         ┌───────────────────────────────────┐
│   Front     │  HTTPS  │               API                 │
│  React/Vite │────────▶│             FastAPI               │
│  (SPA)      │  + WS   │                                   │
└─────────────┘         │  ┌────────┐  ┌─────────────────┐  │
       ▲                │  │ REST   │  │ Scheduler       │  │
       │ OIDC           │  │ /api   │  │ (DAG, queue)    │  │
┌──────┴──────┐         │  └────────┘  └─────────────────┘  │
│   Google    │  tokens │  ┌────────┐  ┌─────────────────┐  │
│  Identity   │────────▶│  │ Worker │  │ State HTTP      │  │
└─────────────┘         │  │ API    │  │ backend (S3)    │  │
                        │  └────────┘  └─────────────────┘  │
┌─────────────┐ webhook │  ┌────────┐  ┌─────────────────┐  │
│ GitHub /    │────────▶│  │ Audit  │  │ OIDC issuer     │  │
│ GitLab      │         │  └────────┘  │ (JWKS, tokens   │  │
└─────────────┘         │              │ workload / run) │  │
                        │              └─────────────────┘  │
┌─────────────┐  poll   └───────┬──────────────┬────────────┘
│  Worker 1   │────────▶ ┌──────▼─────┐ ┌──────▼─────┐
│  (agent)    │          │ PostgreSQL │ │     S3     │
├─────────────┤          │ (app state,│ │ (tfstate,  │
│  Worker 2   │──┐       │  audit)    │ │  logs,     │
└─────────────┘  │       └────────────┘ │  artifacts)│
                 │                      └────────────┘
                 │ AssumeRoleWithWebIdentity
                 │ (workload token signed by the API)
                 ▼
          ┌────────────┐
          │  AWS STS   │──▶ temporary credentials scoped to the run
          └────────────┘
```

### 2.1 Components

| Component | Technology | Role |
|---|---|---|
| **Front** | React 19 + Vite 7 + TypeScript, TanStack Query, Tailwind v4 | SPA — design specified in **DESIGN.md** |
| **API** | FastAPI (Python 3.13+), Pydantic v2, SQLAlchemy 2 async | REST + WS, Google auth, webhooks, worker API, state backend, audit, **workload OIDC issuer** |
| **Scheduler** | Internal API module | DAG, queue, output propagation |
| **Workers** | Python agent (entire MVP); Go rewrite = post-MVP track, outside phases 0–6 | Poll, clone, hooks, terraform, OIDC→STS exchange, log streaming |
| **DB** | PostgreSQL 18 | All application state + queue (`SKIP LOCKED`) |
| **Object storage** | S3 (Garage in dev) | Versioned tfstate, archived logs, artifacts |

### 2.2 Decision: states in S3, exposed via HTTP backend

- **Physical storage: S3** (durability, versioning, lifecycle, SSE-KMS).
- **Terraform interface: the platform's HTTP backend**, which writes/reads in S3:
  1. **Credentials**: per-run scoped tokens (RO for PRs), no IAM rights on the states bucket distributed to workers.
  2. **Locking** in Postgres, visible in the UI, one-click force-unlock (audited).
  3. **Audit & versions**: each write linked to the run that produced it.
  4. **Control**: rejection of regressive serial, managed retention.
- **Compatibility mode**: `managed_state: false` for existing S3 backends.

### 2.3 Decision: dynamic cloud credentials (OIDC workload identity)

The platform becomes an **OIDC issuer** (the same mechanism as GitHub Actions OIDC):

- The API exposes `/.well-known/openid-configuration` + public JWKS.
- On each job claim, the API signs a **workload token** with rich claims: `sub=run:{env}:{stack}:{phase}`, `environment`, `stack`, `run_id`, `phase` (plan/apply), short TTL.
- On the AWS side: an OIDC Identity Provider + IAM roles whose trust policy filters on these claims. Example: the prod apply role is only assumable if `sub` matches `run:prod:*:apply`.
- The worker writes the token to a file and exports `AWS_WEB_IDENTITY_TOKEN_FILE` + `AWS_ROLE_ARN`: the AWS providers consume it natively, **zero specific code in the users' Terraform code**.
- Consequences: plan and apply can assume different roles (plan = ReadOnly + s3 modules, apply = write rights), a PR plan physically cannot modify the infrastructure, and credential rotation disappears from the model.

---

## 3. Implementation phases

### Phase 0 — Foundations + Google Auth (1.5 weeks)

- [ ] Monorepo: `api/`, `worker/`, `front/`, `deploy/`, `docs/`
- [ ] `docker-compose.yml` dev: Postgres, Garage (local S3), hot-reload API, Vite front — **full dev mode specified in DEV.md** (dev login 3 personas, `file://` fixture repos, seed + e2e scenario, shortened timings)
- [ ] FastAPI: healthcheck, settings, modules (`auth/`, `stacks/`, `environments/`, `variable_sets/`, `runs/`, `workers/`, `audit/`, `oidc/`, ...)
- [ ] **Google OIDC auth** (Authorization Code + PKCE): full flow, upsert on `google_sub`, `hd` restriction to the domain, JWT sessions + rotating refresh (table `refresh_tokens`, reuse detection → family revocation, SPECS §2.5), CSRF on `/auth/refresh`, first-admin bootstrap
- [ ] Alembic migrations, `User`, `RefreshToken` models, **`Space` (`default` space created at bootstrap, SPECS §3.0)** — all `space_id` FKs depend on it starting in Phase 1
- [ ] Front: scaffold + **setup of the DESIGN.md design system** (tokens, theme, base components) + **Storybook/Ladle** of the identity components (PhaseRail, StateBadge, ProvenanceBadge...) — the visual contract required by DESIGN.md §8, login page
- [ ] GitHub Actions CI, root `CLAUDE.md`

**Deliverable: `docker compose up` → Sign in with Google → app shell with the final design.**

---

### Phase 1 — Stacks + Environments + Variable sets (3 weeks)

**Goal: the complete layered configuration model.**

- [ ] `Stack` model (template: repo, project_root, tool, version)
- [ ] `Environment` model (instance: **tier dev/staging/prod**, branch, autodeploy, protected, 4-eyes, managed_state, labels, position)
- [ ] **Per-tier apply permissions** (see SPECS §2.4): `users.max_apply_tier` + `users.can_destroy`; `can_apply(user, env)` helper called in the `unconfirmed → confirmed` transition; `protected` refocused on its own effects (forced confirmation + 4-eyes), with access control moving to the tier; admin Users page (role, tier, destroy) + audit of changes
- [ ] **Variable sets** (see SPECS §3.4):
  - named sets of variables (terraform + environment), at the space level
  - attachable to stacks (→ all their envs) or to specific environments
  - `auto_attach: true` = attached to all stacks in the space (e.g. `common-aws`)
  - attachment priority to order the sets relative to each other
  - final resolution: **variable sets (by priority) < stack < environment**
  - UI: provenance badge on each resolved variable ("inherited from `common-aws`", "overridden here")
- [ ] Stack variables + env overrides (existing model, integrated into the resolution)
- [ ] Git integration via token/deploy key (encrypted), check-repo endpoint
- [ ] AES-256-GCM encryption of sensitive values (write-only)
- [ ] Front: list of stacks × envs, creation wizard, stack page, **Variable Sets page** (CRUD + list of attachments + "where is this set used")
- [ ] Audit: CRUD stacks/envs/variables/variable sets + attachments

**Deliverable: a `common-aws` set (region, default tags, Datadog token) attached to 3 stacks, occasionally overridden by an env.**

---

### Phase 2 — Runs + Workers + Logs + Hooks (3.5 weeks) ⭐ critical phase

#### 2a. Run state machine
- [ ] `Run` model (per environment) + `RunEvent`
- [ ] States: `queued → preparing → planning → [checking] → unconfirmed → confirmed → applying → finished` (+ `failed`, `discarded`, `canceled`)
- [ ] Manual trigger, confirm/discard (guarded by `can_apply`: tier + role, see §2.4; automatic 4-eyes on prod tier)
- [ ] Concurrency: 1 active run per environment

#### 2b. Worker API (pull protocol)
- [ ] Register/heartbeat/claim (`SKIP LOCKED`), events, chunked logs, artifacts
- [ ] Dead worker detection → `worker_lost`

#### 2c. The worker agent
- [ ] Loop claim → clone → tool setup → **hooks** → init → plan → upload → confirmation → **hooks** → apply → report
- [ ] Ephemeral workspace, Docker runner, secret masking in the logs

#### 2d. Hooks & checks (custom flows, see SPECS §8)
- [ ] **YAML** declaration in two places, merged: `.stackd.yml` file in the repo (versioned with the code) + hooks defined at the stack/environment level in the UI (governance imposed by the platform, not bypassable by a PR)
- [ ] Anchor points: `before_init`, `after_init`, `before_plan`, `after_plan`, `before_apply`, `after_apply`
- [ ] Each hook = a command executed in the workspace, with read access to `plan.json` (for post-plan checks: tfsec, checkov, infracost, in-house scripts)
- [ ] Failure modes: `fail` (run → failed), `warn` (continue, visible warning, manual confirmation forced even if autodeploy)
- [ ] Hook logs integrated into the viewer (dedicated sections)

#### 2e. Terraform job logs
- [ ] Live: multiplexed WebSocket, line-by-line tracking per phase
- [ ] Viewer: virtualized, ANSI, search, shareable anchors, follow-tail
- [ ] Two-tier storage (hot DB 7 d → S3 gz 1 year), download, configurable retention

#### 2f. Runs front
- [ ] Run page: phase timeline (hooks included), viewer, plan summary, action bar
- [ ] **/queue** page: in-progress and pending runs with **blocking reason** computed by the API (active run on the env, locked env, no compatible worker, apply affinity reservation) — see DESIGN.md §5.5

**Deliverable: a run with an `after_plan: infracost breakdown` hook in warn mode, live logs, confirmed apply.**

---

### Phase 3 — S3 states via HTTP backend + Audit trail UI (2 weeks)

#### 3a. Managed state
- [ ] Complete HTTP backend protocol (GET/POST/LOCK/UNLOCK/423), S3 SSE-KMS storage
- [ ] Application versioning linked to runs, Postgres locking visible in UI, audited force-unlock
- [ ] Per-run scoped tokens (RO for proposed), automatic `-backend-config` injection
- [ ] "Bring your own S3 backend" mode
- [ ] Front: State tab per env (versions, lock, admin download)

#### 3b. Audit: "who applied what"
- [ ] `audit_events` completed: trigger, confirm (Google identity), discard, apply, force-unlock, rotations, roles
- [ ] Filterable /audit page + CSV export, Activity tab per env, per-user view
- [ ] Immutability, 2-year retention

**Deliverable: "who applied what to prod last week, with which plan?" in 10 seconds.**

---

### Phase 4 — Dependencies + outputs + mock outputs (2.5 weeks) ⭐ the differentiator

- [ ] Dependencies between **environments** + "link homonyms" helper
- [ ] `OutputReference` (upstream output → downstream variable mapping), anti-cycle
- [ ] Output capture after apply, hash, never the sensitive ones
- [ ] **Mock outputs** (inspired by Terragrunt, see SPECS §9.3):
  - each `output_reference` can carry a `mock_value`
  - used when the upstream has **never produced** the output (bootstrapping a new cascade) or on **proposed runs** if the upstream is not applied
  - a run that has consumed at least one mock is marked `used_mocks: true`: visible badge, **apply forbidden** (validation plan-only) unless explicit opt-in per env
  - solves the chicken-and-egg problem: write `app/dev` and plan its config before `network/dev` has run
- [ ] Propagation scheduler: topological cascade, policies, multi-parents, stop on failure
- [ ] Run groups + graph view; the cascade never bypasses the protections

**Deliverable: plan `app/dev` with `vpc_id = "vpc-mock00000"` before the first apply of `network/dev`, then a real cascade.**

---

### Phase 5 — Git webhooks + proposed runs (2 weeks)

- [ ] GitHub/GitLab webhook: HMAC, branch → environments mapping, filtering by `project_root`
- [ ] **Git staleness**: tracking of `head_sha` per env (webhook + ls-remote polling 15 min + manual refresh), `↑N` chip on lagging envs, "stale plan" banner + re-plan on stale unconfirmed runs — see SPECS §9.6 / DESIGN §5.1-5.2
- [ ] Push → tracked runs; PR → **proposed runs** plan-only (RO state, secrets not injected by default, mocks allowed)
- [ ] (Optional) PR comment: plan summary + check results (warn/fail hooks)

**Deliverable: `git push` → automatic plans + checks visible in the PR.**

---

### Phase 6 — Dynamic cloud credentials OIDC (1.5 weeks)

- [ ] **OIDC issuer**: `/.well-known/openid-configuration`, JWKS (RS256 keys, rotation), signing of workload tokens at claim time
- [ ] `CloudIntegration` model per environment: provider `aws`, `plan_role_arn`, `apply_role_arn` (see SPECS §10)
- [ ] Agent: token writing, export of `AWS_WEB_IDENTITY_TOKEN_FILE`/`AWS_ROLE_ARN` (or explicit AssumeRoleWithWebIdentity + export of the 3 variables if compatibility is needed)
- [ ] Documentation + provided Terraform module: create the AWS Identity Provider + example roles with trust policies filtered on the claims
- [ ] UI: integration config per env, "dynamic credentials" vs "static variables" indicator, AssumeRole test
- [ ] Audit: `cloud_integration.created/updated`, assumed role traced in the run context

**Deliverable: a prod env with no AWS secret stored anywhere — the plan assumes a ReadOnly role, the apply a write role, backed by a trust policy.**

---

### Phase 7 — Production-ready (ongoing)

- [ ] RBAC per space, Google group mapping — extends per-tier permissions (§2.4) toward per-space/team scopes, and enables the "sensitive but not prod" env that the linear tier does not cover
- [ ] **Environment matrix** (track, not specified): declare `{eu-west-1, us-east-1} × {dev, prod}` on a stack and generate/synchronize the corresponding environments — multi-region is done by naming convention + variable sets in the MVP
- [ ] HTTP run tasks (blocking external webhooks), advanced policies (OPA) if there is a real need
- [ ] OIDC to GCP/Azure (the `CloudIntegration` interface is already generic)
- [ ] Scheduled drift detection
- [ ] Audit export, automated retention/purge
- [ ] Platform observability (Prometheus, OTel, Datadog dashboard)
- [ ] Helm chart / Terraform deployment module (dogfooding)
- [ ] **Go rewrite of the worker** (single binary, easier distribution) — the MVP Python agent remains the functional reference; the Go port does not change the protocol (§7)

---

## 4. Overall estimate

| Phase | Duration (1 dev, realistic part-time) |
|---|---|
| 0 — Foundations + Google OIDC | 1.5 weeks |
| 1 — Stacks + Envs + Variable sets | 3 weeks |
| 2 — Runs + Workers + Logs + Hooks | 3.5 weeks |
| 3 — S3/HTTP State + Audit UI | 2 weeks |
| 4 — Dependencies + outputs + mocks | 2.5 weeks |
| 5 — Webhooks + proposed runs | 2 weeks |
| 6 — OIDC workload credentials | 1.5 weeks |
| **Total demonstrable MVP** | **~16 weeks** |

> Intermediate milestone: end of Phase 2 (≈ 8 weeks) = product usable solo with live logs and hooks. Phases 3–6 = the differentiators (audit, cascade+mocks, zero credential).

---

## 5. Risks and open decisions

| Risk / decision | Impact | Mitigation / current position |
|---|---|---|
| Execution security (providers/provisioners = arbitrary code) | High | Self-hosted workers, Docker per run, no multi-tenant. Platform hooks (non-bypassable) are the governance safeguard |
| Repo hooks (`.stackd.yml`) modifiable by PR | Medium | The **platform** hooks (stack/env) always run and are not bypassable; critical security checks go there, not in the repo |
| OIDC issuer: compromise of the signing key = access to all roles | High | Keys in KMS (`kms:Sign` signing, key never in memory) or encrypted volume, rotation with overlap, JWKS with kid, token TTL ≤ run duration, precise claims in the trust policies (`sub` wildcard tolerated only on the stack segment, never tier/phase) |
| OIDC issuer and state backend in the same API process | Medium | In the MVP, all surfaces (human API, worker API, state backend, OIDC issuer, webhooks) share one process → a flaw in any one of them approaches the signing key. Strong mitigation = KMS (the private key never resides in the process). Isolating the signer in a dedicated service = Phase 7 track if the threat model requires it |
| Mock outputs applied by mistake | Medium | `used_mocks` → apply forbidden by default, very visible UI badge, opt-in per env only |
| Dependency on Google for auth | Medium | Abstract `AuthProvider` interface, other IdPs in Phase 7 |
| Explosion of per-env dependency edges | Low | Homonyms helper + naming conventions |
| Postgres as a queue at large scale | Low (MVP) | Abstract `JobQueue` interface |
| Terraform license (BUSL) | Medium | **OpenTofu first**, Terraform as a user option |

---

## 6. Technical foundation — reference versions and unlocked improvements

Target versions (June 2026). Same technology choices as originally, current version. The table links each bump to what it **concretely unlocks** in Stackd.

| Component | Initial | Target | What it unlocks (and where) |
|---|---|---|---|
| **PostgreSQL** | 16 | **18** (GA 2025-09-25) | Native `uuidv7()` → `DEFAULT` on the DB side, end of application-side ID generation (SPECS §1). Asynchronous I/O (seq scans / vacuum 2-3×) → faster audit and `run_logs` scans (set `io_method=worker`/`io_uring`). `RETURNING` old+new tuple → `transition()` emits the `from→to` `run_event` in a single query (SPECS §4.2). OAUTHBEARER and virtual generated columns: not required in the MVP |
| **Python** | 3.12 | **3.13** (3.14 adoptable) | better error messages, refined typing, REPL. Free-threading (`python3.14t`, official in 3.14) **not relevant** for an async I/O-bound API — to be considered only if the worker becomes CPU-bound (unlikely: it delegates to terraform) |
| **React** | 18 | **19** (GA early 2025) | Actions + `useActionState` → native pending/error handling of forms, directly serves the `loading` state of actions (DESIGN §7) and the wizards (DESIGN §5.7). `ref` as a prop → less `forwardRef` in the 7 identity components. Document metadata → shareable `title` per run. **To avoid: `useOptimistic` on the runs state** — the invariant is that WS events *invalidate* the queries, they do not patch the cache (DESIGN §6). **React Compiler**: opt-in to evaluate (auto-memoization useful on the heavily-refreshed rail/logs), not a prerequisite |
| **Vite** | (not fixed) | **7.x** | Node 20.19+/22.12+. Tailwind v4 via the **`@tailwindcss/vite`** plugin (not PostCSS — avoids the known conflict) |
| **Tailwind** | v4 | **v4.1.x** | already the right major; `@tailwindcss/vite` integration. No contract change (CSS tokens, DESIGN §8) |
| **OpenTofu** (dev image) | (not fixed) | **1.12.x** | the dev image pre-installs a recent version; `tool_version` remains driven **per stack** (SPECS §3.1, user choice). 1.10 introduced **OCI distribution** of modules/providers — a track if we host private modules (otherwise out of scope) |

**Unchanged (already at the right major)**: FastAPI, Pydantic v2, SQLAlchemy 2 async, Alembic, uv, TanStack Query v5, react-flow + dagre, react-virtuoso, anser, pnpm.

**Update rule**: these targets are **floors** at startup, not a continuous-tracking policy. No major bump mid-phase without reason (a vulnerability, a required feature); minors/patches follow the committed `uv.lock` / `pnpm-lock.yaml`. The only bump with a structural effect on the already-specified code is **PG16→18** (native IDs + `RETURNING`), reflected in SPECS §1 and §4.2.
