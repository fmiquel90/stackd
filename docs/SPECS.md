# SPECS.md — Detailed technical specifications

> Companion to PLAN.md. Specifies: **Google OIDC auth**, **data model (stacks → environments, variable sets)**, **state machine**, **logs**, **audit**, **hooks**, **worker protocol**, **S3 state via HTTP backend**, **dependencies + mock outputs**, **dynamic cloud credentials via OIDC**.

---

## 1. General conventions

- IDs: UUIDv7. **PostgreSQL 18** provides a native `uuidv7()` → `DEFAULT uuidv7()` on the DB side (temporal ordering of indexes preserved); application-side generation remains possible to fix the ID before insert, never `gen_random_uuid()` (UUIDv4 is non-monotonic).
- Timestamps: `timestamptz` UTC, `_at` suffix.
- REST API: JSON, `/api/v1` (humans), `/worker/v1` (agents). Errors per RFC 9457.
- Secrets at rest: AES-256-GCM, master key `STACKD_ENCRYPTION_KEY`. **Random 96-bit nonce per encryption**, stored with the ciphertext (`nonce || ciphertext || tag`) — never reuse a nonce with the same key (loss of GCM confidentiality).
- Terraform/OpenTofu: invoked exclusively via CLI by the worker.

---

## 2. Authentication — Google OIDC

### 2.1 Flow

Authorization Code + PKCE, the API is the confidential client:

```
1. Front  → GET /api/v1/auth/google/start  (state + nonce + verifier in signed session)
2. Front  → redirect to accounts.google.com (scopes: openid email profile)
3. Google → /api/v1/auth/google/callback?code&state
4. API    → exchange code+verifier, validate the id_token (JWKS, iss, aud, exp, nonce)
5. API    → admission: email_verified == true, hd ∈ STACKD_ALLOWED_DOMAINS → otherwise 403
6. API    → upsert User on google_sub (stable, unlike the email)
7. API    → session: access JWT 15 min (front memory, never in browser storage)
            + refresh httpOnly 14 d, persisted in `refresh_tokens` (§2.5) with
            rotation and reuse detection → family revocation
8. Front  → GET /api/v1/me
```

**CSRF**: the access token travels in the `Authorization: Bearer` header (no CSRF risk on API calls). The refresh relies on an httpOnly cookie → `/auth/refresh` and `/auth/logout` require `SameSite=Strict` **and** a double-submit CSRF token (the only pair of cookie-bound endpoints). `Secure` cookie, `Path=/api/v1/auth`.

Bootstrap: the first user of an allowed domain = `admin`, subsequent ones = `reader`. No local auth; internal `AuthProvider` interface for other IdPs later. Login/logout/denial → `audit_events`.

### 2.2 Table `users`

```
id uuid PK, google_sub text unique, email text, display_name, avatar_url,
role enum(admin|approver|writer|reader),    -- global capabilities (§2.3)
allowed_tiers text[] not null default '{}',  -- set of tiers where the user can confirm an apply (§2.4)
can_destroy bool default false,             -- right to trigger/confirm a destroy run (§2.4)
disabled bool, last_login_at, created_at
```

### 2.3 Roles (global capabilities)

The `role` carries what a user can do **in nature** (read, manage config, administer). *Which environments* they can apply to comes from the tier (§2.4), not the role.

| Action | reader | writer | approver | admin |
|---|---|---|---|---|
| View stacks/runs/logs/audit | ✅ | ✅ | ✅ | ✅ |
| Trigger a plan (any env) | | ✅ | ✅ | ✅ |
| Confirm an apply | | | ✅ | ✅ | ← **subject to the tier (§2.4)** |
| Manage stacks/envs/variables/variable sets/hooks | | ✅ | ✅ | ✅ |
| Workers, force-unlock, roles, cloud integrations, settings | | | | ✅ |

Triggering a plan ≠ confirming: any writer+ can **prepare** a plan on any env (a plan changes nothing), prod included. Only the **confirmation** of the apply is guarded by the tier — which lets a writer build a prod plan that an authorized approver will confirm.

### 2.4 Per-environment permissions — tier & destroy

The "apply everywhere except prod" need depends on the targeted environment, not just on the user. We express it through a **tier** on the environment and a **set of allowed tiers** on the user, rather than through a full policy system (sufficient for the vast majority of orgs; a full OPA-style policy engine remains Phase 7).

> **Per-space RBAC (Phase F).** `users.role`/`allowed_tiers`/`can_destroy` are **instance defaults**. The effective permission is the caller's `space_memberships` row for the resource's space when present (`UNIQUE (space_id, user_id)`; columns `role`, `allowed_tiers text[]`, `can_destroy`). Every list/get/mutate resolves the space from the resource (stack→space, env→stack→space, run→env→…) and requires membership; an **instance admin** reaches every space. List endpoints are filtered to the caller's accessible spaces. `can_apply(user, env, membership)` uses the *effective* role/tiers/can_destroy. A migration backfills one membership per existing user × existing space (no behaviour change); a newly-provisioned user is enrolled into every existing space with their instance defaults. Spaces CRUD + membership management live under `/api/v1/spaces` (create = instance admin; member management = space admin or instance admin). Per-stack grants and the OPA engine stay Phase 7.
> A create request naming an explicit `space_id` that does not exist gets **404** (after the membership gate, so a non-member still gets 403).
> **Residual (Phase F)**: `GET /api/v1/audit` (and its CSV export) stays instance-wide and is
> **admin-only** — audit rows carry no `space_id` (proper scoping needs a denormalization
> migration). The WS `run:`/`environment:` subscriptions are membership-gated like the REST routes
> (§5.3, §17.2); everything that returns config/secret/output data is membership-gated.

> **Set-based, not linear** (evolved from the original MVP design): tiers are a **configurable catalog** (`tiers` table — `name` unique, `requires_four_eyes`, `position`), not a fixed `dev<staging<prod` enum, and a user carries a **set** of allowed tiers, not a single ceiling. This expresses custom tiers (`qa`, `preprod`, `sandbox`) and **non-contiguous** grants (e.g. `dev`+`prod` but not `staging`) that the old linear ceiling could not.

- `environments.tier` text — references `tiers.name` (validated against the catalog on env create/update). No ordering.
- `users.allowed_tiers` text[] — the set of tiers where the user can **confirm an apply**. Empty = none (can read and plan, never apply). Each entry is validated against the catalog; deleting a tier strips it from every user's set (no FK on the array, so this is enforced in app code).
- Apply rule: `confirm` allowed iff `role ∈ {approver, admin}` **AND** `env.tier ∈ allowed_tiers` (set membership). Example: Bob `{dev, staging}` confirms in dev/staging, denied in prod; Alice `{dev, staging, prod}` confirms everywhere; a user `{dev, prod}` skips staging.
- `users.can_destroy` — a run `type=destroy` (trigger AND confirm) requires `can_destroy=true` **in addition** to the tier rule. A destruction is more dangerous than an apply: a distinct, explicit right.

**Relationship with `protected`**: we **decouple** sensitivity and access right. `environments.protected` carries only its own effects — forcing confirmation (no autodeploy) and enabling 4-eyes; *who* can confirm comes from the tier set. An env can be `tier=prod` without being `protected` and vice versa.

**4-eyes / tier consistency**: a tier with `requires_four_eyes=true` (seeded on `prod`; was hardcoded to the prod tier, now a per-tier flag) forces self-confirmation off by default (the triggerer ≠ the confirmer), as does the per-env `require_second_pair_of_eyes`. The check **fails closed**: if an env points at a tier that no longer exists in the catalog, four-eyes is required rather than silently dropped.

**Scope of 4-eyes**: the "triggerer ≠ confirmer" rule only bites on runs with a **human** triggerer (`triggered_by=manual`, `trigger_user_id` set). A run with no human at its origin (`webhook`, `dependency`) has no `trigger_user_id`: any confirmer authorized at the tier can confirm it (there is no one to oppose). This is not a bypass — the access guard remains the tier (`can_apply`); 4-eyes only prevents *the same person* from triggering **and** confirming.

**Double-lock boundary**: when workload OIDC is active (§10), the apply restriction must **also** live in the AWS trust policy (`sub` claim filtered on `run:<tier>:*:apply`), not only in Stackd — otherwise bypassing the API bypasses the control. Both layers express the same rule; the `tier` **name** feeds the token's `sub` (§10.2). Tier names are charset-constrained (`^[a-z0-9][a-z0-9-]*$`, no `:`/whitespace) precisely so a crafted name can't alias another tier's `sub` segment.

Per-env option `require_second_pair_of_eyes`: the triggerer cannot confirm (forces 4-eyes on a tier that does not itself require it).

### 2.5 Table `refresh_tokens`

Rotation with reuse detection (§2.1, step 7) requires persisting refresh tokens by **family**:

```sql
refresh_tokens (
  id uuid PK,                          -- = jti of the refresh JWT
  user_id FK,
  family_id uuid,                      -- one family per login; revoked as a block on reuse
  parent_id uuid nullable,             -- token this one is derived from (rotation chain)
  token_hash text,                     -- SHA-256 of the token (never the token in clear)
  used_at timestamptz nullable,        -- set on rotation; a 2nd use = reuse detected
  revoked_at timestamptz nullable,
  expires_at, created_at,
  UNIQUE (token_hash)
)
```

On each `/auth/refresh`: the presented token must exist, not be revoked, not be expired, `used_at IS NULL`. We mark it `used_at`, issue a new token (same `family_id`, `parent_id = id`). If a token already `used_at` is presented again → **reuse**: revocation of the whole family + `audit auth.refresh_reuse_detected`. Purge of expired families as a periodic task (§7.5).

---

## 3. Data model

### 3.0 `spaces` — the root container

First level of the hierarchy (breadcrumb `space / stack / env / run`, DESIGN §4) and parent of `stacks`, `variable_sets`, `worker_pools`, `secret_sources`. The bootstrap creates a `default` space (the dev seed also creates `demo`, DEV §7); a create request without `space_id` lands in `default`. **Per-space RBAC** shipped in Phase F (§2.4): `space_memberships`, `GET|POST /api/v1/spaces` (create = instance admin) and member management. Space rename/delete and Google group mapping are not exposed; per-stack grants and OPA stay Phase 7. All `space_id` FKs point to a space from Phase 1.

```sql
spaces (
  id uuid PK,
  name text unique,                 -- 'default' at MVP
  description text,
  created_at, updated_at
)
```

### 3.1 `stacks` — the template (repo + code)

```
id uuid PK, space_id FK, name text unique(space), description,
repo_url, repo_auth_kind enum(none|token|deploy_key), repo_secret_encrypted,
webhook_secret_encrypted bytea nullable,   -- §5/§9.6: HMAC secret of the incoming webhook
project_root text default '.',
tool enum(opentofu|terraform), tool_version text,
created_at, updated_at
```

> Neither branch, nor state, nor autodeploy: everything goes down into the environment.

> **Incoming webhook & shared repo**: one repo can serve several stacks (distinct `project_root`). Since the GitHub/GitLab webhook is configured **per repo** with a unique secret, `webhook_secret_encrypted` is shared across these stacks (same value). On receipt (`POST /api/v1/webhooks/github`, §12), the API resolves the stacks whose `repo_url` matches the payload, verifies the HMAC against their common secret, then filters environments by branch and `project_root` (§9.6).

### 3.2 `environments` — the executable instance

```
id uuid PK, stack_id FK, name text unique(stack),       -- dev, staging, prod
tier text references tiers.name,   -- §2.4: carries the apply/destroy permissions (catalog, not enum)
branch text,                       -- branch tracked by THIS env
autodeploy bool,                   -- forced false if protected
protected bool,                    -- §2.4: forces confirmation + 4-eyes (NOT the access control → tier)
require_second_pair_of_eyes bool,
managed_state bool,
allow_mock_apply bool default false,  -- §9.3: allow the apply of a run having consumed mocks
head_sha text nullable,               -- §9.6: known head of the tracked branch
head_updated_at timestamptz nullable,
commits_ahead int nullable,           -- nb of commits between last apply and head
affects_project_root bool nullable,   -- do the commits ahead touch this project_root?
locked bool, labels jsonb, position int,
created_at, updated_at
```

Why not Terraform workspaces: weak isolation (shared code/backend/credentials, state suffix), easy targeting mistakes. A Stackd environment = physically separate state, variables, protections and its own worker pool.

**Deletion.** Deleting an environment — or a stack, which takes its environments with it — is refused with **409 `Active run`** (problem+json carrying `run_id`) while one of them has a run in an active state (§3.5): the cascade would remove that run outside `transition()` (§4.2) while a worker executes it. Queued runs go with their environment. Everything that targets what is deleted by `target_kind`/`target_id` (no FK) is removed in the same transaction, each removal audited with a `reason` (`stack_deleted` | `environment_deleted`): variable-set attachments (§3.4, `variable_set.detached`), platform hooks (§8.1, `hook.deleted`) and notification targets (`notification_target.deleted`). The deletion holds the stack/env rows `FOR UPDATE`; attaching one of these to a stack or an env locks that row `FOR KEY SHARE` until it commits, so an attach either waits and then finds no target (404) or commits first and is removed by the deletion — never an orphan.

### 3.3 `variables` — stack and environment level

```
id uuid PK,
stack_id FK nullable,              -- set for stack/env vars
environment_id FK nullable,        -- NULL = common to the stack; set = env override
variable_set_id FK nullable,       -- set for the vars of a set (§3.4)
kind enum(terraform|environment),  -- terraform → TF_VAR_/tfvars; environment → env var
name text, value text nullable, value_encrypted bytea nullable,
sensitive bool, hcl bool
-- CHECK: exactly one parent (variable_set_id XOR stack_id)
-- CHECK: environment_id IS NULL if variable_set_id is set
--         (a set variable is never scoped to an env: it is
--          the ATTACHMENT of the set that carries the targeting, §3.4)
-- Uniqueness: "parent" is not a column (stack_id OR variable_set_id) →
--   two partial unique indexes, not a single UNIQUE constraint:
--     UNIQUE (stack_id, COALESCE(environment_id,'00..0'::uuid), kind, name)
--       WHERE stack_id IS NOT NULL
--     UNIQUE (variable_set_id, kind, name)
--       WHERE variable_set_id IS NOT NULL
--   (COALESCE because NULL ≠ NULL in SQL: without it, two stack vars of the same name
--    with environment_id NULL would not trigger the intended conflict)
```

### 3.4 `variable_sets` — factored configuration

```sql
variable_sets (
  id uuid PK, space_id FK,
  name text unique(space),          -- e.g. common-aws, datadog, prod-credentials
  description text,
  auto_attach bool default false,   -- true = attached to all stacks of the space
  selector jsonb,                   -- {label: value} rule; attaches to any env whose effective
                                    --   labels (stack.labels + env.labels) match all entries.
                                    --   AND-equality only (NOT a policy engine — OPA is Phase 7)
  created_at, updated_at
)

variable_set_attachments (
  id uuid PK,
  variable_set_id FK,
  target_kind enum('stack','environment','tier'),
  target_id uuid,                   -- stack → all its envs; tier → all envs of that tier in the
                                    --   set's space (target_id = tiers.id); environment → this env only
                                    -- polymorphic (stack OR env) → no DB FK; integrity is app-enforced
  priority int default 0,           -- orders the sets among themselves at resolution
  UNIQUE (variable_set_id, target_kind, target_id)
)
```

**Final resolution of a variable at claim time** (from weakest to strongest):

```
1. variable sets that apply by rule      (auto_attach, or selector matching the env's
   effective labels) — weakest layer, ordered by name
2. variable sets attached to the tier    (all envs of the env's tier, by increasing priority)
3. variable sets attached to the stack   (by increasing priority)
4. variable sets attached to the env     (by increasing priority)
5. stack variables (environment_id NULL)
6. environment variables                 ← always wins
```

A set's **selector** (`{label: value}`) auto-attaches it to every environment whose effective labels (`stack.labels` + `env.labels`, env wins on conflict) contain all of its key=value pairs — equality AND-match, no operators (deliberately not the deferred OPA engine, PLAN §7). It sits at the same weakest layer as `auto_attach`; explicit attachments remain stronger.

**Attachments stay in the set's space.** Attaching to a `stack` or `environment` requires the target to belong to the variable set's space **and** the caller to be a `writer` there; otherwise it is refused with `400 Cross-space attachment` (`403` if the caller lacks write access on the target). A tier is a global catalog row (no space), so a `tier` attachment applies only to environments of that tier **inside the set's own space**. Resolution also ignores any explicit attachment whose set belongs to another space (defense in depth).

**No orphan attachments.** Since there is no FK, deleting a stack or an environment removes the attachments targeting it — for a stack, also those targeting its environments — in the deletion's transaction, each audited `variable_set.detached` (context `reason`: `stack_deleted` | `environment_deleted`).

At equal name and kind, the upper layer overrides. Two sources outside resolution are added at claim time: upstream outputs (`dependency:`) and mocks (`mock`) — see §9.

**Writing a resolved value to disk (Phase D).** Resolution order above is unchanged — this is purely *how* the winning value reaches terraform. A terraform var with the **`hcl`** flag and a value is written **verbatim** (`name = <raw value>`) to a generated `zzz_stackd.auto.tfvars` (HCL), so real HCL syntax (`{ a = "b" }`, `["a","b"]`, function calls, expressions) parses with its native type; it is **excluded** from `stackd.auto.tfvars.json` so it is never defined twice. Every other terraform var (plain strings, and the dependency outputs / mocks added at claim — already typed JSONB) goes to `stackd.auto.tfvars.json`. Both files auto-load. The claim payload carries the split (`tfvars_json` + `hcl_tfvars`); sensitive hcl values remain in the masking table (§5.1). The snapshot of **provenances** (`{"TF_VAR_region": "set:common-aws", "TF_VAR_cidr": "env", "TF_VAR_vpc_id": "dependency:network/prod", "TF_VAR_nlb_dns": "mock"}`) is frozen in `runs.variable_provenance` for audit and the UI badge (DESIGN.md §5.2: "inherited from…", "overridden here", `MOCK`). Deleting an attached set → 409 with the list of attachments (explicit detachment required).

### 3.5 `runs`

```
id uuid PK, environment_id FK,
type enum(tracked|proposed|destroy|command), state enum (§4),
commit_sha,                          -- pinned by the plan job's report (§7.3) when the trigger gave
                                     -- none or a ref; the apply clones exactly this commit
commit_message, commit_author,
command jsonb nullable,              -- command runs only: {name, args} (§4.3)
triggered_by enum(manual|webhook|dependency|api|schedule), trigger_user_id nullable,
                                     -- 'api' reserved: no application tokens/PAT at MVP (auth = human
                                     -- Google + worker), set when programmatic triggering arrives
                                     -- (Phase 7); 'schedule' = drift runs (§19)
confirmed_by_user_id nullable,       -- who approved the apply (core of the audit)
parent_run_id nullable, run_group_id nullable, worker_id nullable,
plan_summary jsonb,                  -- {add, change, destroy, resources}
check_results jsonb,                 -- results of the after_plan hooks (§8)
resolved_inputs jsonb,               -- injected upstream outputs (non-sensitive)
used_mocks bool default false,       -- §9.3: at least one mock consumed
variable_provenance jsonb,           -- §3.4: provenance of each resolved variable
plan_artifact_key text nullable,     -- S3 key of the encrypted saved plan (§7.3/§11.3) the apply job
                                     -- applies; set by the plan job's upload, cleared by transition()
                                     -- on any terminal state (migration 0024)
claimed_at, confirmed_at, finished_at, error nullable, created_at
```

The drift (§19), VCS (§18) and external-secret (§15.5) columns are listed in their own sections.

Concurrency — 1 active run per environment:

```sql
CREATE UNIQUE INDEX one_active_run_per_env ON runs (environment_id)
WHERE state IN ('preparing','planning','checking','unconfirmed','confirmed','applying','running');
```

### 3.6 `run_events`

```
id, run_id FK, from_state, to_state,
actor enum(system|user|worker), actor_id nullable, payload jsonb, created_at
```

### 3.7 `workers` and `worker_pools`

```
worker_pools: id, space_id, name, labels jsonb, token_hash, created_at
workers: id, pool_id, name, status(idle|busy|offline), labels jsonb,
         version, last_heartbeat_at, registered_at
```

`offline` if heartbeat > 60 s; run whose job is executing on an offline worker (`preparing`, `planning`, `checking`, `applying`, `running`) → `failed (worker_lost)` after 120 s — **except a job holding apply credentials** (an `applying` run, or a mutating command run §4.3), reclaimed only after the apply window = apply budget + grace (`apply_reclaim_window_seconds` = `stackd_apply_timeout_seconds` + `stackd_apply_lost_grace_seconds`, default 15 min + 5 min) so a healthy long apply is never failed mid-flight and two workers can't write the same env's state concurrently (§4.2). Its state token and apply OIDC token are minted for that same window (§10.2/§11.2), so by the time it is reclaimed they have expired. `unconfirmed` and `confirmed` runs are **never** reclaimed: their plan job is over and they wait on a human or on the apply claim. The apply needs nothing from the plan worker: any worker takes it past the affinity window (§7.2), clones the run's pinned commit and applies the **saved plan** it downloads from the API (§7.3) — never a re-plan. Workers are disposable, losing the plan worker must not fail them. Targeting by labels (dedicated prod pool recommended).

### 3.8 Dependencies, outputs and mocks

```sql
env_dependencies (
  id uuid PK,
  upstream_env_id FK, downstream_env_id FK,
  trigger_policy enum('on_output_change','always','never'),
  UNIQUE (upstream_env_id, downstream_env_id),
  CHECK (upstream_env_id <> downstream_env_id)
)

output_references (
  id uuid PK, dependency_id FK,
  output_name text,                 -- upstream Terraform output
  input_name text,                  -- downstream variable (without TF_VAR_)
  mock_value jsonb nullable,        -- §9.3: mock value for bootstrap
  UNIQUE (dependency_id, input_name)
)

env_outputs (
  id uuid PK, environment_id FK, run_id FK,
  name text, value jsonb,           -- NULL if sensitive
  value_hash text, sensitive bool,
  UNIQUE (environment_id, name)
)
```

Anti-cycle: DFS on creation → 422. Helper `POST /stacks/{id}/dependencies/link-by-name`: bulk edges between environments of the same name across two stacks.

### 3.9 Managed state and hooks

```
state_versions: id, environment_id FK, serial, lineage, size_bytes,
                s3_key, created_by_run_id nullable, created_at
state_locks:    environment_id PK, lock_id, info jsonb, locked_at

hooks (platform, §8): id, target_kind enum('stack','environment'), target_id,
       stage enum(before_init|after_init|before_plan|after_plan|before_apply|after_apply),
       name text, command text, on_failure enum('fail','warn'),
       position int, created_at, updated_at

run_logs (hot tier, §5.2): run_id FK, phase text, section text nullable,
       seq int, lines jsonb, created_at — PK (run_id, phase, seq)
```

### 3.10 `cloud_integrations` (OIDC workload, §10)

```
id uuid PK, environment_id FK unique,
provider enum('aws'),                -- gcp/azure in Phase 7
plan_role_arn text, apply_role_arn text,
region text nullable, session_duration int default 3600,
created_at, updated_at
```

### 3.11 `oidc_signing_keys` (issuer workload, §10)

JWKS rotation with overlap (§10.1) requires persisting keys with their `kid`: the old one stays published as long as in-flight tokens use it.

```sql
oidc_signing_keys (
  id uuid PK,
  kid text unique,                   -- exposed in the JWKS and the JWT header
  algorithm text default 'RS256',
  public_jwk jsonb,                  -- published on /oidc/jwks
  private_key_encrypted bytea,       -- AES-256-GCM (§1) OR KMS reference (key never in clear at rest)
  status enum('active','retiring','retired'),  -- only 1 'active' at a time (signs); 'retiring' still in the JWKS
  created_at, retired_at nullable
)
```

Rotation: a new `active` key, the old one moves to `retiring` (still in the JWKS, no longer signs), then `retired` (out of the JWKS) once the max TTL of a token has elapsed. KMS option: `private_key_encrypted` becomes an ARN reference, signing goes through `kms:Sign` (the private key never leaves KMS) — recommended in prod (see risk §13 / PLAN §5).

---

## 4. Run state machine

### 4.1 Diagram

```
                  ┌──────────┐
   trigger ──────▶│  queued  │── env locked / active run: waits
                  └────┬─────┘
                  ┌────▼─────┐
                  │preparing │ claim, clone, setup tool, hooks before/after_init, init
                  └────┬─────┘
                  ┌────▼─────┐
                  │ planning │ commit pinned, hooks before_plan, plan -out + show -json,
                  └────┬─────┘ saved plan uploaded (tracked/destroy with changes, §7.3)
                  ┌────▼─────┐
                  │ checking │ hooks after_plan (tfsec, infracost, scripts)
                  └────┬─────┘   fail → failed; warn → forced confirmation
         ┌────────────┼────────────────────┐
  empty  │            │ changes             │ type=proposed
  plan   │            ▼                     ▼
         │     ┌─────────────┐       ┌──────────┐
         │     │ unconfirmed │       │ finished │ (plan-only)
         │     └────┬───┬────┘       └──────────┘
         │  confirm │   │ discard ──▶ discarded
         │ (can_app-│
         │  ly: tier│
         │  + role) │
         │     ┌────▼─────┐
         │     │confirmed │ (the plan's worker is preferred for 60 s, then any worker)
         │     └────┬─────┘
         │     ┌────▼─────┐
         │     │ applying │ clone pinned commit, init, download saved plan, hooks before_apply,
         │     └────┬─────┘ apply plan.tfplan, output -json, hooks after_apply
         ▼          ▼
      ┌──────────────────┐
      │     finished     │──▶ scheduler hooks: capture outputs, cascade, audit
      └──────────────────┘
```

Terminal: `finished`, `failed`, `discarded`, `canceled`. `canceled`: user on `queued`/`unconfirmed`, or the system when a PR closes (its in-flight proposed runs, §18.3). Cancelling a run a worker is executing (signal via heartbeat → SIGINT) is not implemented yet: the API answers 409. Any terminal state drops the run's saved plan (§11.3).

Command runs (§4.3) take a shorter path: `queued → preparing → running → finished | failed`.

### 4.2 Transition rules

| Transition | Actor | Conditions |
|---|---|---|
| `queued → preparing` | worker (claim) | no active run on the env, env not locked, compatible labels |
| `planning → checking` | worker | plan OK **and ≥ 1 after_plan hook**. Without an after_plan hook, `checking` is skipped: the transitions `planning → unconfirmed / confirmed / finished` exist, with exactly the same conditions as their equivalents from `checking` |
| `checking → unconfirmed` | worker | checks OK or warn; non-empty diff. A warn **forces** confirmation even if autodeploy |
| `checking → confirmed` | system | all checks OK, non-empty diff, `autodeploy=true`, env not protected, `used_mocks=false` |
| `planning/checking → finished` | worker | empty diff (outputs captured after refresh) |
| `unconfirmed → confirmed` | user | `can_apply(user, env)` = role∈{approver,admin} AND `env.tier ∈ allowed_tiers` (§2.4); ≠ triggerer if the tier requires four-eyes or 4-eyes; for a `destroy` run: `can_destroy` required; **blocked if `used_mocks` and `allow_mock_apply=false`** |
| `confirmed → applying` | worker (claim) | the plan's worker within the affinity window, then any compatible worker (§7.2); re-checks `allow_mock_apply` and the secret-fallback gate against the apply-time resolution (§9.3/§15.5). The apply job clones the pinned commit and applies the run's **saved plan** (§7.3); a missing or unreadable plan fails the run — it never re-plans |
| `applying → finished` | worker | apply exit 0 + outputs uploaded |
| `* → failed` | worker/system | exit ≠ 0, hook `fail`, timeouts — the worker hard-kills the process past its budget: the apply (and a mutating command run, §4.3) past `stackd_apply_timeout_seconds` (default 15 min, sent as `apply_timeout_seconds`), any other subprocess past the agent's 2 h cap (separate prepare/plan budgets are not enforced yet); `worker_lost` (§3.7); `commit_mismatch` (§7.3); an unresolvable dependency or secret at claim (§9.3, §15.2) |

Single function `transition(run, to_state, actor, payload)`: legality, atomic update guarded on `from_state`, `run_event`, audit event if the action is human or terminal, WS publication, scheduler hooks. The guarded UPDATE uses `RETURNING` (PG18: old + new tuple) to produce the `from→to` `run_event` without a re-read, in the same transaction.

### 4.3 Command runs

A `RunType.command` run executes **one allowlisted tofu/terraform subcommand** (`import`, `state list/show/rm/mv`, `taint`, `untaint`, `output`, `show`, `validate`, `providers`, `refresh`) instead of the plan→apply flow — for state surgery and adoption that `plan`/`apply` can't express. It is **not** arbitrary shell: the worker runs `<tool> <command> <args>` with the command taken verbatim from the allowlist. Lifecycle: `queued → preparing → running → finished | failed`. The subcommand + args live in `runs.command` (`{name, args}`). Endpoint: `POST /api/v1/environments/{env_id}/commands`. Permissions: read-only commands need `writer`; **mutating** commands (import, state rm/mv, taint, untaint, refresh) require `can_apply(user, env)` — the same gate as an apply. The worker receives the **apply** OIDC role for mutating commands and the (read-only) **plan** role otherwise. A mutating command, like an applying run, **holds apply credentials**: its state token TTL and OIDC cap are the apply window (`apply_reclaim_window_seconds`, §3.7), the worker hard-kills its subcommand past the apply budget (`apply_timeout_seconds` of the claim payload), and worker-lost detection only reclaims it after the apply window (§7.5). The trigger is audited (`run.command_triggered`) and so is completion (`run.command_executed`). `force-unlock` is **not** a command — it has its own endpoint (`DELETE …/state/lock`, §11.2).

---

## 5. Job logging system

### 5.1 Ingestion (worker → API)

```
POST /worker/v1/jobs/{id}/logs
  { "phase": "planning", "section": "hook:infracost" | null,
    "seq": 42, "lines": [ {"t": "...", "msg": "..."} ] }
```

- `seq` strictly increasing per phase → idempotency of retries.
- `section` distinguishes hooks from the terraform stream (dedicated sections in the viewer).
- Agent buffer: 1 s / 32 KB. **Masking before sending of ALL the run's sensitive values** — `sensitive_env` *and* the `tfvars` marked `sensitive` (§3.3) — replaced with `***`. The agent builds the masking table from the claim payload, not only from `sensitive_env`.
- **`plan.json` and hooks**: `tofu show -json` lists the run's variable values in clear, sensitive ones included. **Repo** hooks only ever read a **masked copy** (§8.3): every value tofu marks sensitive and every value of the masking table above replaced by `***`; the uploaded `plan.json` artifact is that copy too. **Platform** hooks (trusted, §8.1) read the raw JSON: one that dumps it can re-print a sensitive value to its stdout, where the value-based masking above still applies. Residual limit: a short or transformed secret (base64, substring) escapes value-based masking — in hook stdout, and in the repo hooks' copy wherever tofu did not mark the value sensitive itself — a **documented** limit, do not put exploitable secrets in non-sensitive `tfvars`. The check tools (tfsec/checkov/infracost) do not print values by default.
- **Cleartext tripwire (Phase C)**: after a phase, the agent scans the plan (`show -json`) and apply (`output -json`) for any value Stackd treats as sensitive appearing verbatim in a **non-sensitive** output — terraform only redacts outputs the *repo* marked `sensitive = true`, so a sensitive Stackd variable echoed by an unmarked output leaks. On plan it flags the run with a `secret_leak_suspected` **warn** check (config `STACKD_LEAK_TRIPWIRE=fail` aborts the run before any apply); on apply it is warn-only (a masked log line) since the infra has already changed. The tripwire **narrows, does not eliminate**, the leak surface — a transformed secret still escapes value-based detection. The human-readable `show` already relies on terraform's own `(sensitive value)` redaction; only the masked JSON artifact is uploaded.
- ANSI sequences preserved (color rendering on the front side).

### 5.2 Two-tier storage

| Tier | Where | When | Usage |
|---|---|---|---|
| Hot | table `run_logs` | during the run + 7 d | live + recent |
| Cold | S3 `logs/{run_id}/{phase}.log.gz` | async archiving at end of run | history (1 year then lifecycle) |

`GET /runs/{id}/logs?phase=&after_seq=` serves one or the other transparently. `GET /runs/{id}/logs/download?phase=all`.

> **Status**: only the hot tier exists today — logs stay in `run_logs` (no archiving, no purge,
> §7.5) and `/logs/download` is not served yet.

### 5.3 Live distribution

Single multiplexed WebSocket: `{"sub": "run:<id>"}` → `log_chunk {phase, section, seq, lines}` + `run_event`. Reconnection → REST GET `after_seq` to fill the gap, then the stream.

**Multi-replica fan-out (without a broker).** A WS client is connected to **one** API replica; the event (transition or log chunk) may be produced on **another** replica. The bridge is **Postgres `LISTEN/NOTIFY`**:

- `transition()` (§4.2) and log ingestion (§5.1) emit a `NOTIFY` on a per-entity channel (`run_<id>`, `env_<id>`) **in the same transaction** as the write.
- The `NOTIFY` payload carries only a **lightweight signal** — `{kind, run_id, phase, max_seq}`, never the content (8 KB `NOTIFY` cap, and log lines can be large).
- On receipt, each replica re-reads the source (`run_logs` after `after_seq`, or the run state) and pushes to the locally subscribed WS clients. The front sees the same sequence as on REST reconnection — a single read path, idempotent by `seq`.

In the single-replica MVP, `LISTEN/NOTIFY` remains the mechanism (no "in-process" branch to maintain); it scales as-is up to several replicas before requiring a real bus (abstract `EventBus` interface, like `JobQueue`).

### 5.4 Viewer (front)

Specified in **DESIGN.md §5.3**: virtualization, collapsible sections per phase and per hook, follow-tail, search, `#L1234` anchors, ANSI rendering, timestamps toggle, download.

---

## 6. Audit — "who applied what"

### 6.1 Table `audit_events` (append-only)

```
id uuid PK (v7), actor_kind enum(user|worker|system|webhook), actor_id nullable,
actor_email text nullable,          -- denormalized: readable even if user deleted
action text,                        -- taxonomy §6.2
target_kind text, target_id uuid,
context jsonb,                      -- stack_name, env_name, run_id, commit, plan_summary...
ip, user_agent nullable, created_at
```

**Real immutability, not just "no API"**: the absence of an update/delete endpoint does not protect against a bug or an application compromise. The application DB role only has `INSERT` and `SELECT` on `audit_events` (`REVOKE UPDATE, DELETE`), a `BEFORE UPDATE OR DELETE` trigger raises an exception, and retention purging goes through a distinct role, separate from the application role and itself audited. 2-year retention, explicit admin purge (audited). Indexes: `(created_at)`, `(actor_id, created_at)`, `(target_kind, target_id, created_at)`, `(action, created_at)`.

### 6.2 Taxonomy

The exhaustive list of actions the API emits (to check it: `grep -rn 'action="' api/app`, plus the
`audit_action=` of each `transition()` call and the per-field map in `users/router.py`):

```
auth.login / auth.logout / auth.domain_denied / auth.refresh_reuse_detected
space.created / space.member_set / space.member_removed            # Phase F memberships (§2.4)
stack.created / stack.updated / stack.deleted
environment.created / environment.updated / environment.deleted
environment.head_refreshed        # manual head refresh (§9.6) — context: branch, old_sha, new_sha
environment.inputs_discovered     # discover-inputs created placeholder env variables (§20.3)
environment.drift_detected / environment.drift_cleared               # §19.4
variable.created / variable.updated / variable.deleted   # stack, env and set variables — context:
                                                          #   name, sensitive — NEVER the value
variable_set.created / variable_set.updated / variable_set.deleted
variable_set.attached / variable_set.detached   # detached: manual, or with its target — context
                                                #   reason = stack_deleted | environment_deleted (§3.2)
hook.created / hook.updated / hook.deleted      # deleted: manual, or with its stack/env (reason, §3.2)
notification_target.created / notification_target.updated / notification_target.deleted
                                                # deleted: manual, or with its stack/env (reason, §3.2)
run.triggered / run.destroy_triggered / run.command_triggered / run.promoted   # §4.3, §9.7
run.confirmed ⭐                  # a human confirm, or the system on autodeploy (context autodeploy)
run.discarded / run.canceled      # canceled: a user, or the system when a PR closes (reason pr_closed)
run.plan_finished                 # plan-only terminal: empty diff, or a proposed/drift run
run.applied ⭐ / run.command_executed
run.apply_failed                  # ANY failed run, whatever its phase — context phase, or reason:
                                  #   worker_lost | commit_mismatch | dependency | secret (§7.2/§7.3)
run.check_failed / run.check_warned            # results of the after_plan hooks
state.force_unlocked / state.import_session_created   # §11.2, §11.4
dependency.created / dependency.deleted / dependency.mock_consumed   # created: also via link-by-name
cloud_integration.created / cloud_integration.updated / cloud_integration.deleted
worker_pool.created / worker_pool.deleted
worker.diagnostics_requested                   # read-only debug bundle (cf. §observability)
user.role_changed / user.apply_tier_changed / user.destroy_permission_changed / user.disabled
tier.created / tier.updated / tier.deleted     # configurable tier catalog (§2.4)
secret_source.created / secret_source.updated / secret_source.token_rotated / secret_source.deleted
secret.fallback_used / secret.fallback_overridden                    # §15
```

Not audited by a dedicated action: a run failed because an external secret could not resolve is
`run.apply_failed` with `reason: secret` (§15.6); a pool token is not rotatable through the API
(delete and recreate the pool).

`run.confirmed` + `run.applied` = the answer to "who applied what": Google identity of the confirmer, env, commit, plan summary, assumed IAM role (if OIDC), link to the logs.

### 6.3 Assumed double write

`run_events` = the fine-grained mechanics of the state machine. `audit_events` = the denormalized business journal. The audit event is written **in the same DB transaction** as the action — no bus, no loss.

### 6.4 API and UI

```
GET /api/v1/audit?actor=&action=&target_kind=&target_id=&from_=&to=&limit=   (admin; limit ≤ 1000, default 200)
GET /api/v1/audit/export?actor=&action=&target_kind=&target_id=&from_=&to=   (admin, CSV)
```

Filtering by stack or environment goes through `target_kind`/`target_id`; there is no `stack=` /
`environment=` filter.

/audit page (filterable global journal), Activity tab per env (recent applies: who/when/commit/summary/logs), per-user view.

---

## 7. Worker protocol

### 7.1 Registration and heartbeat

```
POST /worker/v1/register   (Bearer pool_token) → worker_id + worker_token
POST /worker/v1/heartbeat  (20 s) { "in_flight": N, "capacity": M } → { "commands": [{"id": ..., "type": "diagnostics"}] }
```

Heartbeat = downstream command channel. No incoming connection. Today it carries `diagnostics` only; `cancel_job` (cancelling an executing run, §4.1) is not implemented yet. **In-flight count (Phase E)**: the worker reports the number of jobs it is currently executing; the API sets `status = busy` when it is > 0, `idle` when 0 (the worker is authoritative, which also flips a finished worker back to idle). `capacity` (the worker's `STACKD_MAX_CONCURRENT_JOBS`) is advertised for the scheduler to reason about; not persisted this phase (no schema change). A heartbeat with no body is just a liveness ping.

**Worker concurrency (Phase E).** `STACKD_MAX_CONCURRENT_JOBS` (default `1` = today's serial behaviour) bounds the in-flight jobs a single worker runs at once, each on its own thread (the heartbeat thread is already independent). The claim stays one-run-at-a-time on the wire; the API's `SELECT … FOR UPDATE SKIP LOCKED` (§7.2) and the one-active-run-per-env partial unique index (§3.5) keep concurrent claims safe — **concurrency is across different environments only**. Same-env races are still guarded by the state-backend lock (§11.2).

### 7.2 Claim (long-poll)

```
POST /worker/v1/jobs/claim?wait=25
→ 204 if nothing
→ 200 {
    "job_id": "...",                       # = the run id
    "phase": "plan" | "apply" | "command", # JOB type (what the agent executes) — do not confuse
                                           # with the fine-grained phases of the run (§4) or of the
                                           # logs (§5): a "plan" job covers preparing+planning+
                                           # checking, an "apply" job applying, a "command" job
                                           # (§4.3) preparing+running
    "cred_phase": "plan" | "apply",        # CREDENTIAL phase of the job: "apply" iff it holds
                                           # apply credentials — an apply job or a MUTATING command
                                           # (§4.3); a plan job or a read-only command → "plan".
                                           # Picks the OIDC role (§10.2), the state-token TTL and
                                           # scope (§11.2), and the agent's time budget for a
                                           # command (absent/unknown → the agent assumes "apply")
    "apply_timeout_seconds": 900,          # hard budget (§4.2, `stackd_apply_timeout_seconds`):
                                           # the agent kills `apply`, and a cred_phase=apply command,
                                           # past it
    "refresh_only": false,                 # true for a drift run (§19): plan with -refresh-only
    "destroy": false,                      # true for a destroy run: plan with -destroy (the saved
                                           # plan carries it to the apply)
    "save_plan": true,                     # plan job of a tracked/destroy run: upload plan.tfplan
                                           # when the plan has changes (§7.3); false for a proposed
                                           # run (never applied), an apply or a command job
    "command": {"name": "state rm", "args": ["aws_s3_bucket.old"]},   # command jobs only, else null
    "environment": { "id", "name": "prod", "stack_name": "core-network",
      "repo_url", "commit_sha", "branch", "project_root",
      "tool": "opentofu" | "terraform", "tool_version" },
                                           # commit_sha null (plan job only): the agent clones the
                                           # head of `branch`, reports the SHA (§7.3) and the API
                                           # pins the run to it; an apply job always gets the pinned
                                           # full SHA and clones exactly it (no SHA → refused)
    "repo_credentials": { "kind": "none" | "token" | "deploy_key",
                          "token": "<decrypted, memory TTL>" },   # token only if kind≠none and set
    "env": { ... },                        # environment-kind variables, resolution
                                           # sets→stack→env (§3.4)
    "sensitive_env": { ... },              # sensitive environment-kind variables — never logged,
                                           # never given to repo hooks (§8.3)
    "tfvars_json": { ... },                # terraform variables (JSON), with the dependency
                                           # inputs / mocks (§9.3) merged in
    "hcl_tfvars": { "tags": "{ team = \"core\" }" },  # hcl-typed variables, written verbatim (§3.4)
    "mask_values": [ "..." ],              # literal secrets the agent masks in ALL logs (§5.1):
                                           # sensitive values, state/OIDC/repo tokens
    "hooks": {                             # §8: PLATFORM hooks by stage — the agent appends the
                                           # repo's .stackd.yml hooks after them
      "after_plan": [ {"name": "infracost", "command": "...", "on_failure": "warn",
                       "source": "platform"} ]
    },
    "backend": { "type": "http", "address": ".../state/v1/<env_id>",   # managed state (§11),
                 "lock_address", "unlock_address",                     # else null; no lock_* keys
                 "lock_method": "LOCK", "unlock_method": "UNLOCK",     # for a `ro` token (§11.2)
                 "username": "env", "password": "<state_token scoped+TTL>" },
                                           # → handed to tofu ONLY, as TF_HTTP_ADDRESS/…/
                                           # TF_HTTP_PASSWORD env vars, never -backend-config
                                           # (tofu persists those in .terraform/ and in the saved
                                           # plan); hooks never get them; inherited TF_HTTP_* dropped
    "backend_config_file": null,           # unmanaged state only (§11.3): -backend-config=<file>
    "backend_config": null,                # unmanaged state only (§11.3): -backend-config=k=v
    "cloud_credentials": {                 # §10, if cloud_integration configured, else null
      "provider": "aws",
      "oidc_token": "<signed workload JWT>",
      "role_arn": "arn:aws:iam::123:role/stackd-prod-plan",   # role of the cred_phase
      "region": "eu-west-1"
    },
    "resolved_inputs": { "TF_VAR_vpc_id": "vpc-0abc..." },          # informational: already
    "mock_inputs": { "TF_VAR_nlb_dns": "mock.example.internal" }    # in tfvars_json (§9.3)
  }
```

Three distinct notions, never to be conflated (CLAUDE.md §7): the **job phase** (`phase`, the execution type), the **credential phase** (`cred_phase`, which credentials the job holds — the single definition is `holds_apply_credentials(run)`, shared with the worker-lost reclaim cutoff §7.5), and the **run state** (§4, the fine-grained `preparing`/`planning`/… the run moves through, which the logs also use as their phase). A plan or apply job's credential phase equals its job phase; a command job's depends on the command. The agent never re-derives the credential phase from the command name: it reads `cred_phase`.

Atomic claim:

```sql
WITH next AS (
  SELECT r.id FROM runs r
  JOIN environments e ON e.id = r.environment_id
  WHERE r.state IN ('queued','confirmed')
    AND e.labels <@ :worker_labels
    AND NOT EXISTS (...)            -- no other active run on the env
  ORDER BY (r.state = 'confirmed' AND r.worker_id = :wid) DESC,   -- affinity:
           r.created_at                                            -- the apply prefers
  FOR UPDATE SKIP LOCKED LIMIT 1                                   -- the worker of the plan
)
UPDATE runs SET state=..., worker_id=:wid, claimed_at=now()
FROM next WHERE runs.id = next.id RETURNING runs.*;
```

**The real concurrency guard is the unique index `one_active_run_per_env` (§3.5), not `SKIP LOCKED`.** Two workers claiming two **distinct** `queued` runs of the same env select different rows: `SKIP LOCKED` does not serialize them against each other, and the `NOT EXISTS (active run)` is true for both (none active yet). Both `UPDATE → preparing` go ahead; the second violates the partial unique index. Two protections, **both** to be applied:

1. **Serialize per env**: the `SELECT ... FOR UPDATE` also locks the corresponding `environments` row (`JOIN environments e ... FOR UPDATE OF e SKIP LOCKED`), so that only one claim per env progresses at a time; the others skip the locked env and take another run.
2. **Safety net**: the violation of `one_active_run_per_env` is **caught** (SQLSTATE `23505`) and treated as "nothing to claim" → `204`, the worker re-polls. This is the correctness guarantee; the lock above is only an optimization to avoid discarded work.

Apply affinity: a `confirmed` run is reserved for its originating worker for **60 s** (`AND (r.worker_id = :wid OR r.confirmed_at < now() - interval '60 seconds')` on confirmed runs). Past that delay (dead or saturated worker), any compatible worker takes it: the apply needs nothing from the plan worker's workspace — it downloads the run's **saved plan** from the API (§7.3) and applies exactly it. There is **no re-plan** on the apply path: no saved plan → the apply fails.

### 7.3 Events and artifacts

```
POST /worker/v1/jobs/{id}/events
  { "event": "phase_started"|"phase_finished"|"job_failed",
    "phase": "...", "exit_code": 0,
    "result": { "has_changes": true, "summary": {...},
                "checks": [{"name":"infracost","status":"warn","detail":"..."}] } }
  phase_started(planning) carries "result": {"commit_sha": "<full SHA checked out>"}
  job_failed(after_apply) carries "result": {"checks": [...], "outputs": {...}}   # §8.3: the
      # apply went through, an after_apply hook failed → outputs recorded (§9.1), no cascade

PUT /worker/v1/jobs/{id}/artifacts/plan.tfplan | plan.json | outputs.json
GET /worker/v1/jobs/{id}/artifacts/plan.tfplan          # the apply job's saved plan
```

**Saved plan.** What a human confirms is the plan file the plan job made, and the apply applies exactly that file (`tofu apply plan.tfplan`) — never a fresh plan:

- **Upload** (`PUT …/plan.tfplan`, worker-authenticated, the run's worker only): accepted only for a `tracked`/`destroy` run (`409` for a proposed or command run) and only during its plan job (`preparing`/`planning`/`checking`, checked under the run's row lock — `409` afterwards: once planned, the reviewed plan is frozen, for every worker). Capped at 64 MiB (`413`, also while streaming), empty → `400`. The API encrypts it (AES-256-GCM, app key, AAD bound to its S3 key — a plan file holds the run's variable values in clear, sensitive ones included), stores it under `artifacts/{run_id}/plan.tfplan` (§11.3) and records the key on the run (`runs.plan_artifact_key`). The agent makes the plan file outside the checkout, reads it right after `plan`/`show`, deletes it and uploads those bytes **before any `after_plan` hook** (a repo hook can neither swap what gets applied nor read it, §8.3).
- **Download** (`GET …/plan.tfplan`): only by the worker holding the run, only while the run is `applying` (`409` otherwise — not even the worker that planned it before the apply claim). No saved plan / object missing → `404`; integrity failure (wrong key, tampered or moved blob) → `500`. Never anything else in its place.
- **Apply job**: clone the pinned commit, `init`, download the saved plan — on any refusal or error the job **fails closed** (`saved plan unavailable, not applied: …`), before the `before_apply` hooks — then `before_apply` hooks (a failing `fail` check fails the job here: nothing is applied, §8.3), write the plan (`0600`, replacing whatever the checkout has at that path, never through a symlink), `apply -input=false -json plan.tfplan` with the run's tfvars present for that command only, delete the file (the `after_apply` repo hooks never see it). tofu itself refuses a saved plan whose state moved since (`Saved plan is stale`) or whose inputs changed (`Mismatch between input and plan variable value`): the run fails with tofu's detail and "trigger a new plan".
- **Retention**: deleted once the run is terminal (§11.3).

**Commit pinning.** The plan job reports the full SHA it checked out on `phase_started(planning)`. A run triggered without a commit (manual, cascade) is pinned to it; a run whose `commit_sha` is a ref (branch/tag) is re-pinned to the SHA it resolved to; a run already pinned to a full SHA must have checked out exactly that commit — otherwise the API fails it (`commit_mismatch`, audited `run.apply_failed`) and answers `409`. A malformed SHA gets `400`. The apply job is handed the pinned SHA and clones exactly it (shallow `fetch` of that SHA from the authed URL — `origin` itself points at the token-free URL, §8.3 — checkout of `FETCH_HEAD`, verified against `HEAD`), so the apply, its hooks and `output -json` run on the reviewed commit and staleness (§9.6) knows what was applied.

### 7.4 Course of a job on the agent side (pseudo-code)

```python
job = claim()
ws = Workspace(job.job_id)
# depth 1; a pinned SHA/ref is fetched exactly (FETCH_HEAD, verified), else the head of `branch`.
# An apply job without a pinned commit is refused before cloning (§7.3).
ws.git_clone(job.environment.repo_url, job.environment.commit_sha, branch=job.environment.branch,
             token=job.repo_credentials.token)  # token in the clone URL, then `origin` reset to the
                                       # token-free URL; a pinned commit is fetched from the authed URL
                                       # itself — no token left under .git/ (§8.3)
# Managed state (§11): the backend settings and state token reach tofu ONLY, as TF_HTTP_* (tofu_env
# below); the repo gets a bare `backend "http" {}` block.
tf = ensure_tool(job.environment.tool, job.environment.tool_version)  # verifies the checksum
                                       # SHA-256 (and the cosign/GPG signature if available) of the
                                       # downloaded binary against a pinned list — refuse otherwise (supply-chain)

cloud_env = {} if not job.cloud_credentials else {       # §10 OIDC workload
    "AWS_WEB_IDENTITY_TOKEN_FILE": ws.secret_path("oidc_token"),   # outside the checkout
    "AWS_ROLE_ARN": job.cloud_credentials.role_arn,
    "AWS_ROLE_SESSION_NAME": f"stackd-{job.job_id}" }
run_env = {**job.env, **job.sensitive_env, **cloud_env}
tofu_env = {**worker_env_minus("STACKD_*"), **run_env, **tf_http(job.backend)}   # runner contract
platform_env = {**worker_env, **run_env}                 # platform hooks (trusted, §8.3)
repo_env = {**allowlisted(worker_env), **job.env}        # repo hooks (§8.3)

# What a tofu command reads from disk that no repo hook may find (§8.3) exists only while it runs:
# the OIDC token file, and — for a command that reads variables — the run's tfvars (`tfvars_json`,
# dependency inputs/mocks merged in, §9.3, and `hcl_tfvars`, §3.4). Written fresh (never through a
# symlink), deleted right after. The token file is written around each platform hook the same way.
with_files = lambda tfvars=True: tofu_files(ws, job, tfvars)

hooks = merge_hooks(job.hooks, ws.load_stackd_yml())   # platform first, §8
# stage(name): run its hooks, each under the hook timeout (§8.3). A failing `fail` hook → report
# job_failed(phase=name, checks) and stop, before the next tofu step; a `warn` one → into `checks`.

if job.phase == "plan":
    write_backend_override(ws)               # `terraform { backend "http" {} }`, managed state only
    report(phase_started, phase="planning", commit_sha=ws.head_sha())   # the API pins the run
    stage("before_init")
    with with_files(): run(tf, "init", "-input=false")
    stage("after_init"); stage("before_plan")
    # plan/apply run with -json: the agent streams each event's human @message to the log (masked)
    # AND collects the structured events — the `change_summary` event gives the authoritative
    # add/change/destroy counts and `diagnostic` events surface the real error as the run's `error`.
    plan_file = ws.private_path("plan.tfplan")             # outside the checkout (§8.3)
    with with_files():
        code, events = run_json(tf, "plan", "-json", f"-out={plan_file}", "-detailed-exitcode",
                                *(["-destroy"] if job.destroy else []))
    # 0 = no changes, 2 = changes; anything else (1 = error, 124 = killed past its time budget, a
    # signal) → job_failed with the reason — never "no changes". apply/commands: any non-zero fails.
    with with_files(tfvars=False):
        run(tf, "show", "-no-color", plan_file)              # the human-readable diff, to the log
        plan_json = run(tf, "show", "-json", plan_file)
    masked_json = mask_plan_json(plan_json)                  # §8.3
    upload_artifact("plan.json", masked_json)
    saved_plan = read(plan_file) if code == 2 and job.save_plan else None
    delete(plan_file)                                        # before any hook runs
    if saved_plan:
        upload_artifact("plan.tfplan", saved_plan)           # the saved plan (§7.3)
    stage("after_plan")   # checking phase: platform hooks read plan.json = plan_json, then the repo
                          # hooks plan.json = masked_json (§8.3)
    report(phase_finished, has_changes=(code == 2), summary=..., checks=checks)

elif job.phase == "apply":                   # a fresh clone of the pinned commit (§7.3)
    write_backend_override(ws)
    with with_files(): run(tf, "init", "-input=false")
    plan = download_artifact("plan.tfplan")  # missing/unreadable → fail closed, NEVER re-plan
    stage("before_apply")                    # a failing check → nothing applied
    write_plan(plan)                         # 0600; removed right after the apply
    with with_files():                       # the tfvars too: tofu refuses changed inputs (§7.3)
        run(tf, "apply", "-input=false", "-json", "plan.tfplan",   # stale → tofu refuses → failed
            timeout=job.apply_timeout_seconds)
    with with_files(tfvars=False): outputs = run(tf, "output", "-json")
    upload_artifacts(outputs, "outputs.json")
    stage("after_apply")                     # a failing check → job_failed(after_apply) carrying
                                             # `outputs`: recorded, no cascade (§8.3)
    report(phase_finished, outputs=outputs, checks=checks)

else:                                        # command job (§4.3): no hooks, no plan/apply phases
    write_backend_override(ws); write_tfvars(ws, job.tfvars_json, job.hcl_tfvars)
    report(phase_started, phase="running"); run(tf, "init", "-input=false")
    run(tf, *job.command.name.split(), *job.command.args,
        timeout=default_cap if job.cred_phase == "plan" else job.apply_timeout_seconds)

ws.cleanup()
```

The tofu subcommands that reach the state (`init`, `plan`, `apply`, `output`, a command) get
`tf_env`; `show` of the local plan file does not need it. Hooks get the platform env (platform
hooks) or the stripped repo env (§8.3), never the `TF_HTTP_*` settings. Subprocesses without an
explicit budget are capped at 2 h by the agent's watchdog.

`docker` runner: each command (terraform AND hooks) runs in `stackd/runner:<tool>-<version>` (image including the common check tools: tfsec, checkov, infracost, jq).

**Runner trust contract (Phase C).** The `docker` runner (prod) is the trust boundary: one **ephemeral** container per job; the image carries **no long-lived cloud creds**; the OIDC token is written to a `0600` file outside the checkout (`Workspace.secret_file`) only for the duration of each tofu command and each platform hook — never while a repo hook runs (§8.3) — exposed to them via `AWS_WEB_IDENTITY_TOKEN_FILE`, and the whole workspace is removed in the `finally` `ws.cleanup()`; an optional egress allowlist is enforced via the run's network. Repo code also runs inside tofu (`data "external"`, provisioners): tofu gets the worker's environment **minus the agent's own settings** — the whole `STACKD_*` namespace (the pool token, the API URL, …, and any platform setting a worker host carries) — then the run's `env` and `sensitive_env`, the OIDC workload env (§10.4) and, for managed state, `TF_HTTP_*` (§11.2). Everything else a provider may need stays: `PATH` and locale, proxies and CA bundle, `TF_*` (plugin cache, …), provider settings. Repo hooks run untrusted: `sh -c <repo command>` with an allowlisted `repo_env` (§8.3). The `local` runner (dev) with a mounted `~/.aws` is **trusted-dev only** — never prod (`STACKD_ENV=production` is documented to require the `docker` runner).

### 7.5 Periodic tasks (internal scheduler, multi-replica)

The scheduler module (PLAN §2.1) carries background tasks that must run **exactly once** even with several API replicas:

The loop ticks every 10 s.

| Task | Frequency | Effect |
|---|---|---|
| `worker_lost` detection | every tick | heartbeat > 60 s → `offline`; run executing on a worker offline for > 120 s → `failed (worker_lost)` (never `unconfirmed`/`confirmed`, §3.7); a job holding apply credentials (an `applying` run or a mutating command run, §4.3) only after the apply window = budget + grace (§3.7/§4.2) |
| Notification outbox | every tick | drain `notification_outbox` (§17 intro, CONCEPTS §14): at-least-once, ≤ 5 attempts, no DB transaction held across the HTTP POST |
| VCS post-back outbox | every tick | drain `vcs_outbox` (§18.4) |
| Drift detection | every tick, per-env gate | enqueue the due `-refresh-only` runs (§19.2) |
| Git staleness polling | 15 min (`stackd_head_poll_interval`) | `git ls-remote` per (repo, branch, credentials) → update `head_sha` (§9.6); its own task, so slow git calls never delay the others |
| Cold log archiving | end of run | `run_logs` → S3 gz, purge hot > 7 d (§5.2) — **not implemented yet**: logs stay in `run_logs` |
| Refresh token / audit purge | daily | expired families (§2.5), audit > 2 years (§6.1, audited purge) — **not implemented yet** |

**Single-execution guarantee**: each task takes a dedicated **PG advisory lock** (`pg_try_advisory_lock(<task_key>)`) before running; a replica that does not obtain the lock skips its tick. The lock is session-level, so it is taken and released on a **dedicated connection** held for the task's whole run, separate from the task's own DB session — the tasks commit mid-way, which returns the session's connection to the pool, and an unlock through it could land on another connection and leak the lock. No permanently elected leader (no external dependency), no double execution. The tasks are **idempotent** anyway (re-running a staleness computation or an archive breaks nothing) — the advisory lock mainly avoids redundant work and races on the `worker_lost` transitions.

---

## 8. Hooks & checks (custom flows)

### 8.1 Two sources, one merge

| Source | Declaration | Modifiable by | Usage |
|---|---|---|---|
| **Platform** | UI/API, stack or environment level (table `hooks`) | writer+ (audited) | imposed governance: **not bypassable by a PR** |
| **Repo** | `.stackd.yml` file at the root of the `project_root`, versioned | anyone who pushes code | project-specific logic (file generation, terragrunt, etc.) |

Execution order at each stage: platform stack hooks → platform env hooks → repo hooks. Critical security checks go on the platform side.

### 8.2 `.stackd.yml` format

```yaml
version: 1
hooks:
  before_plan:
    - name: generate-locals
      command: ./scripts/gen-locals.sh
  after_plan:
    - name: infracost
      command: infracost breakdown --path plan.json --format table
      on_failure: warn          # fail | warn (default: fail)
    - name: no-destroy-prod
      command: jq -e '[.resource_changes[] | select(.change.actions | index("delete"))] | length == 0' plan.json
      on_failure: fail
```

### 8.3 Execution semantics

- Each hook: one shell command in the workspace (cwd = project_root), the run's env vars injected (except `sensitive_env` for **repo** hooks — opt-in per env, same logic as proposed runs).
- **Cloud credentials (§10) not exported to repo hooks.** The `AWS_WEB_IDENTITY_TOKEN_FILE` / `AWS_ROLE_ARN` variables are injected only into the environment of **terraform** invocations, never into that of **repo** hooks by default (same reasons as `sensitive_env`: a `.stackd.yml` pushed via PR must not be able to assume the prod apply role and exfiltrate). Opt-in per env if a hook legitimately needs the cloud. **Platform** hooks (non-bypassable) have access to it. The token file `AWS_WEB_IDENTITY_TOKEN_FILE` names (`<workspace>/oidc_token`, outside the checkout) is on disk only while a tofu command or a platform hook runs — written fresh for each, never through a planted symlink, and removed right after — so a repo hook can't read it either (as `../oidc_token`).
- **Repo-hook env is built from an allowlist.** Of the worker process environment, the agent's `repo_env` keeps only what any shell command needs — `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `TMPDIR`, `TERM`, `LANG`, `LANGUAGE`, `LC_*`, `TZ`, the proxy settings (`HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, `ALL_PROXY`, either case) and the CA bundle location (`SSL_CERT_FILE`, `SSL_CERT_DIR`) — then layers the run's non-sensitive `env`. Nothing else of the worker's own environment reaches a repo hook: a worker host can carry any secret there (the pool token, a database URL, a cloud-credential family such as `AWS_*` — exported, on the **trusted-dev** `local` runner, by a mounted `~/.aws`; the `docker` runner (prod) bakes no long-lived creds into the image), and the state backend's `TF_HTTP_*` (§11.2) go to tofu only. Anything a repo hook needs is set as a run variable. Platform hooks keep the worker's environment. A regression test asserts a repo hook sees neither cloud nor sensitive env, nor any other worker variable.
- **Repo hooks at the `*_apply` stages on tier=prod**: forbidden by default. On a `tier=prod` env, only **platform** hooks execute at `before_apply`/`after_apply`; a repo hook at these stages is ignored with a visible warning (it would run with the prod write role). The `*_init`/`*_plan` repo stages remain allowed (plan role = ReadOnly).
- `plan.json` available for reading at the `after_plan` stage — the plan job's last; the apply job is a fresh clone (§7.3) and has none. `tofu show -json` lists the run's variable values in clear, sensitive ones included: **platform** hooks read it as is; before the first **repo** hook runs it is replaced by a **masked copy** — every value tofu marks sensitive (input variables declared `sensitive`, resource attributes, outputs) and every value the agent masks in logs (§5.1) replaced by `***`, the rest kept as valid JSON. The uploaded `plan.json` artifact is that masked copy. The plan job writes its plan file (`plan.tfplan`) outside the checkout and deletes it, once shown and uploaded (§7.3), before any `after_plan` hook runs: no repo hook finds the saved plan, nor raw plan JSON, in the workspace. Files the agent writes into the checkout replace whatever is there — a repo-planted symlink is never written through.
- **The run's tfvars are in the checkout only while tofu reads them.** `stackd.auto.tfvars.json` and `zzz_stackd.auto.tfvars` (§3.4) are written right before each tofu command that reads variables — `init`, `plan`, `apply` (so tofu still refuses a saved plan whose inputs changed, §7.3) and a command run's subcommand — and deleted right after it: no hook, at any stage, finds them. tofu auto-loads them as before (same precedence among the repo's own `*.auto.tfvars`, and every subcommand reads them), which `-var-file` would change.
- **The repo token is not in the checkout.** The agent clones with the token in the URL (`repo_credentials`, §7.2), then points `origin` back at the token-free `repo_url` before anything else runs; the pinned-commit fetch (§7.3) is handed the authed URL itself. git keeps no other copy (`FETCH_HEAD` and the reflogs record the URL without its credentials): no hook finds the token under `.git/`.
- Timeout per hook: 10 min, configurable per worker (`STACKD_HOOK_TIMEOUT_SECONDS`), for platform and repo hooks alike. Past it the hook is killed — its whole process group, so a child it started can't outlive it — and fails by its own `on_failure` (detail `timed out after <n>s`). Logs in a dedicated section of the viewer.
- **`on_failure: fail`** → run `failed`, audit `run.check_failed`. At every stage and from either source: the job fails at that stage (`job_failed`, `phase` = the stage, the failed check in `checks`) before the next tofu step — a failing `before_apply` hook means nothing is applied. A failing `after_apply` hook fails the run although the apply has already changed the infrastructure: the agent reports the outputs it read right after the apply with the failure (`job_failed(after_apply)`, `result.outputs`), so the API records them like a finished apply's (§9.1 — `env_outputs` reflects the infrastructure), but nothing cascades from the failed run (§9.2).
- **`on_failure: warn`** → the run continues but mandatorily goes through `unconfirmed` (even with autodeploy), warning badge + detail on the run page, audit `run.check_warned`. A human takes responsibility. At the `*_apply` stages the run is already confirmed: the apply goes on and the warning is reported in the apply job's `checks` (`phase_finished`); the API appends those checks to the plan's in `runs.check_results` and audits `run.check_warned` the same way.
- Results aggregated in `runs.check_results` and displayed in the checks status bar.

---

## 9. Dependencies, output propagation and mocks

### 9.1 Capture

At `applying → finished`, parsing of `outputs.json`:
- non-sensitive → upsert `env_outputs` + `value_hash = sha256(canonical_json)`
- sensitive → `value=NULL, sensitive=true`. Never stored nor propagated; an `output_reference` that points to it → visible config error, no silent null.

### 9.2 Cascade — hook `on_finished(run)`

```
1. outgoing edges of run.environment
2. policy: never → skip; always → trigger;
   on_output_change → trigger if value_hash ≠ resolved_inputs of the last
   finished run of the downstream env
3. run created: type=tracked, triggered_by=dependency, parent_run_id, run_group_id
4. multi-parents: downstream triggered when ALL its parents of the run group are finished
5. parent failed → branch stopped, run group = partial_failure
6. downstream env protected → stops at unconfirmed (never bypassed)
```

Resolution of inputs at claim time (fresh values), snapshot frozen in `resolved_inputs`.

### 9.3 Mock outputs (bootstrap, inspired by Terragrunt)

**Problem**: how to plan `app/dev` if `network/dev` has never been applied? Without a mechanism, the cascade has a chicken-and-egg problem at creation time.

**Mechanism**:

1. Each `output_reference` can define a `mock_value` (JSON: string, number, list, map).
2. When resolving inputs at claim time, for each reference:
   - the upstream output **exists** in `env_outputs` → real value (the mock is ignored, even on a PR)
   - the output **does not exist** AND `mock_value` is defined → mock injected, the reference is listed in `mock_inputs` of the payload
   - the output does not exist AND no mock → run `failed` immediately with an explicit error (`missing_upstream_output`)
3. The run is marked `used_mocks=true` + audit `dependency.mock_consumed` (which references).
4. **Guardrails**:
   - highly visible "MOCKED" badge on the run page + list of mocked values
   - `unconfirmed → confirmed` **refused** if `used_mocks` and `environment.allow_mock_apply=false` (default): a mocked plan serves to validate the config, not to be applied
   - never autodeploy a mocked run
   - proposed runs (PR) use mocks freely (plan-only by nature)

**Good mock values**: plausible for the type expected by the provider (`vpc-mock00000000`, `subnet-mock...`) — documented, with examples per common AWS resource type.

### 9.4 Run groups

`POST /environments/{id}/runs` with `{"with_downstream": true}` in the body: subgraph (BFS), topological sort (Kahn), run group, root launched, subsequent levels via cascade. UI: graph colored by state (see DESIGN.md §5.4).

### 9.5 Multi-region patterns (reference recipes)

The model has no native "region" dimension: the region is configuration, the environment is the unit. Two recipes cover the real cases:

**A. Identical deployment in N regions**

```
stack core-network (single code, main branch)
├── env prod-eu-west-1   ← variable sets: [tier-prod, region-eu-west-1]
├── env prod-us-east-1   ← variable sets: [tier-prod, region-us-east-1]
└── env dev-eu-west-1    ← variable sets: [tier-dev,  region-eu-west-1]
```

- Everything that differs between regions lives in the `region-*` sets (provider region, AZs, CIDRs, AMIs). Everything that differs between tiers lives in the `tier-*` sets.
- A push on the tracked branch triggers one run per env (multi-region iso-prod).
- Since the `cloud_integration` is per env, each region can assume a different IAM role (or even a different AWS account).

**B. Primary region A → secondary "similar but not identical" in region B, with outputs from A**

Choice of form, in order of preference:
1. **Same stack, difference via variables**: a `TF_VAR_is_primary` flag (+ `count`/`for_each` in the code) if the difference is conditional. Maximizes reuse.
2. **Separate stack** (other `project_root` of the same repo or another repo) if the difference is structural. Avoids invasive conditionals.

Output flow: an ordinary `env_dependencies` edge —

```
network/prod-eu-primary ──▶ network/prod-us-secondary
  output_references:
    global_cluster_arn → TF_VAR_global_cluster_arn   (mock: "arn:aws:rds::mock")
    kms_replica_key_id → TF_VAR_kms_key_id           (mock: "mrk-mock0000")
  trigger_policy: on_output_change
```

- **A dependency between two environments of the same stack is valid**: the constraint only forbids self-reference (`upstream <> downstream`). The intra-stack primary/secondary pattern is therefore native.
- Bootstrap: mocks allow planning region B before the first apply of A (§9.3); the apply of B remains blocked as long as it consumes mocks.
- Env B `protected` → the cascade stops at `unconfirmed`: promoting a change from primary to the secondary region goes through a human.
- The `link-by-name` helper does not apply (different env names): the edges are created explicitly — a cross-region relationship must be a choice, not an automatism.

### 9.6 Git staleness — "applied ≠ branch head"

> To be distinguished from **infrastructure drift** (state vs cloud reality, Phase 7): here we compare the **last applied commit** to the **HEAD of the tracked branch**. Typical case: apply Monday 9:00 on `abc1234`, PR merged at 9:15 → the environment is one commit behind.

**Computation**:

```
last_applied_sha = commit_sha of the last finished run (type tracked, with apply or no-change)
env.head_sha     = known head of env.branch
stale            = head_sha present AND last_applied_sha present AND head_sha ≠ last_applied_sha
```

No applied run yet (or a last run with no pinned commit) → `stale = false`: the applied commit is unknown. Computed for every env the API returns, in one query per request.

**Updating `head_sha`** (from most precise to fallback):
1. **Push webhook** (Phase 5): immediate update of all envs tracking the branch — even if the webhook does not trigger a run (`project_root` filtering), it always updates `head_sha` — on the envs still tracking that branch when it is written (guarded update: an env re-pointed meanwhile gets neither the head nor a run). A push that **deletes** the branch (`deleted: true`, all-zero `after`) triggers no run and never records the zero SHA: it clears `head_sha`, `commits_ahead` and `affects_project_root` (so the env is not `stale`), stamping `head_updated_at` with the receipt time.
2. **Fallback polling**: `git ls-remote` per (repo, branch) every 15 min (`stackd_head_poll_interval`; periodic task, deduplicated by repo across the stacks sharing it — and by their repo credentials, so no stack's head is read with another stack's token) — covers absent or lost webhooks. Only envs whose head was not observed within the interval (by a webhook, a manual refresh or a previous poll, on any replica) are polled; a head recorded by a webhook while the poll runs is never overwritten. No DB transaction is held across the git calls, which run concurrently (at most 8 at a time); a failing repo is logged (never its token) and retried at the next poll, without blocking the others. The poll runs as a task of its own (still under its advisory lock, §7.5), so git calls outlasting a scheduler tick never delay the other periodic tasks; a poll still running when the next one is due skips that one.
3. **Manual**: `POST /api/v1/environments/{id}/refresh-head`. Like the poll, it holds no DB transaction across its git call, and does not record the head if the env was re-pointed to another branch meanwhile. The recorded refresh is audited `environment.head_refreshed` (branch, old and new SHA) in the same transaction.

Changing an environment's `branch` (`PATCH`) clears `head_sha`, `head_updated_at`, `commits_ahead` and `affects_project_root`: the recorded head was the old branch's, and the env is then due at the next poll.

**Two levels of precision**:
- *Level 1 — branch ahead*: SHA comparison, always available. `commits_ahead` via the Git provider's compare API if available, otherwise binary "behind" display.
- *Level 2 — concerns you*: `affects_project_root` = at least one commit ahead modifies files under `project_root` (GitHub/GitLab compare API). If unavailable: NULL, the UI stays at level 1.

**Effects**:
- `↑N` chip on the env cell (DESIGN.md §5.1) and the env header; attenuated variant if `affects_project_root = false` ("the branch moved ahead, but not this folder").
- **Obsolete `unconfirmed` run**: if `head_sha` advances while a plan awaits confirmation, banner on the run page — "plan computed on `abc1234`, the branch advanced by N commits" — with a *Re-plan* action (discard + new run on the head). Confirmation remains **possible** (applying a specific commit is legitimate) but the obsolescence is impossible to miss.
- No automatic action: staleness is information, never a trigger (that is the role of webhooks).
- `GET /api/v1/environments/{id}` exposes `head_sha`, `commits_ahead`, `affects_project_root`, `stale`.

### 9.7 Environment promotion (trunk-based)

`POST /api/v1/environments/{target_id}/promote` `{from_environment_id}` re-deploys the **exact commit
currently applied** on a sibling environment (same stack) to the target: it takes the source's last
`finished` run `commit_sha` and creates a **tracked** run on the target pinned to that commit. This
is the trunk-based promotion primitive (dev → staging → prod of the *same* stack, as opposed to the
cross-stack §9.2 cascade). Triggering needs `writer`; the apply is gated as usual at confirm
(`can_apply` + 4-eyes). 400 on a cross-stack pair, 409 if the source has no applied commit yet.
Audited as `run.promoted` (context: from env/run, target env, commit).

---

## 10. Dynamic cloud credentials — OIDC workload identity

### 10.1 The platform as OIDC issuer

```
GET /.well-known/openid-configuration     → issuer, jwks_uri, alg RS256
GET /oidc/jwks                            → public keys (kid, rotation)
```

- RS256 key pair, stored encrypted (or KMS), rotation with overlap (the old key stays in the JWKS for the lifetime of in-flight tokens).
- The issuer must be reachable over HTTPS by AWS STS (public URL or via the provided Terraform module that creates the Identity Provider with the thumbprint).

### 10.2 Workload token (signed at claim, per phase)

```json
{
  "iss": "https://stackd.example.com",
  "sub": "run:prod:core-network:apply",       // tier:stack:phase — basis of the trust policies
  "aud": "sts.amazonaws.com",
  "environment": "prod", "tier": "prod", "stack": "core-network",
  "environment_id": "...", "run_id": "...", "phase": "apply",
  "triggered_by": "dependency",
  "exp": <now + min(session_duration, max duration of the phase)>
}
```

For the `apply` phase — an apply, or a mutating command run (§4.3) — the phase cap is the apply window (`apply_reclaim_window_seconds`, §3.7), so the cloud credentials never outlive the job's worker-lost reclaim cutoff.

> The tier segment in the `sub` (`run:<tier>:...`) is what materializes the **double lock** of §2.4: Stackd refuses the confirmation on the API side *and* the AWS trust policy refuses the AssumeRole if the tier does not match. A prod write role is only assumable by a prod-tier token.

### 10.3 AWS side (provided: example Terraform module)

```hcl
# Trust policy of the prod apply role — refuses everything else
condition {
  test     = "StringLike"
  variable = "stackd.example.com:sub"
  values   = ["run:prod:*:apply"]
}
```

Recommended pattern: `plan_role_arn` = ReadOnly role (+ S3 module access), `apply_role_arn` = scoped write role. A PR plan **physically** cannot modify the infra.

**The wildcard only covers the `stack` segment** (`run:prod:*:apply` = "any stack, but prod tier AND apply phase"). `tier` and `phase` are always fixed in the condition: they are what materializes the double lock (§2.4). A role whose trust policy left the tier or the phase as a wildcard (`run:*:*:*`) would cancel the guard — to be banned (see §13).

### 10.4 Worker side

The worker exchanges nothing itself in the simple case: it writes the token to a file and exports `AWS_WEB_IDENTITY_TOKEN_FILE` + `AWS_ROLE_ARN` + `AWS_ROLE_SESSION_NAME=stackd-{run_id}` — the AWS SDK in the providers does the AssumeRoleWithWebIdentity natively. The session name = run_id → **CloudTrail traces every AWS action back to the run** (and therefore back to the human who confirmed: complete audit loop).

Fallback: no `cloud_integration` → classic static variables (variable set `aws-credentials`), current behavior.

### 10.5 Priority and coexistence

If a `cloud_integration` exists AND static `AWS_*` variables are resolved → the OIDC variables win, config warning displayed (a classic source of confusion).

---

## 11. Managed state — S3 behind the HTTP backend

### 11.1 Architecture

```
terraform ──(backend "http")──▶ API Stackd ──(boto3, SSE-KMS)──▶ S3
                                    └─▶ Postgres: state_versions, state_locks, audit
```

S3 for the bytes, HTTP for the interface: per-job scoped tokens (§11.2) without distributed AWS credentials, visible locking, audit, refusal of a regressive serial.

### 11.2 Endpoints

| Method | Endpoint | Behavior |
|---|---|---|
| `GET` | `/state/v1/{env_id}` | any scope; 200 + latest state, 404 otherwise |
| `POST` | `/state/v1/{env_id}?ID=<lock_id>` | **`rw` scope only**; while a lock is held, a missing `ID` or one that is not the held lock's → **423** + holder (no lock held: an ID-less write is accepted — `tofu -lock=false`), refuses regressive serial (409), **403** once the token's run is no longer active, upload S3, `state_version` linked to the run (`NULL` for an import session) |
| `LOCK` | `/state/v1/{env_id}/lock` | `plan` or `rw` scope; the body (tofu's lock info) must carry an `ID` (400 otherwise); 200 or **423** + holder; **403** once the token's run is no longer active — and, for a `plan` token, once its run has left the plan phase |
| `UNLOCK` | `/state/v1/{env_id}/lock` | `plan` or `rw` scope (a `ro` token never locks); releases the lock only for its holder: the body's `ID` must be the held lock's — missing or different → **423** + holder, like a `LOCK` conflict (tofu always sends the lock info it locked with; `tofu force-unlock <id>` sends that id); 200 if nothing is held; still allowed after the run ended |

These four are the whole Terraform-protocol surface: there is no state deletion route. Breaking a lock whose ID one does not hold is the human **force-unlock**, `DELETE /api/v1/environments/{env_id}/state/lock` (admin, audited `state.force_unlocked`, §12) — never a state token.

Auth: Basic, password = JWT `{env_id, run_id, scope, typ: "state", iat, exp}`, minted at the claim (§7.2) for **one job**, not for the whole run. Its `scope` is fixed at mint time from the job, so a run's plan token and its apply token are different tokens with different rights:

| Scope | Minted for | May call |
|---|---|---|
| `ro` | a **proposed** run's plan job (PR plan, drift §19) and a **read-only command** job (`output`, `show`, `state list/show`, `validate`, `providers`, §4.3) | `GET` only. Its `backend` block carries **no** `lock_address`/`unlock_address`, so tofu's http backend never locks (a `LOCK` would get 403); these commands never lock anyway |
| `plan` | the **plan job** of a `tracked`/`destroy` run (`cred_phase = plan`) | `GET`, `LOCK` (only while the run is `preparing`/`planning`/`checking`), `UNLOCK` — **never `POST`**: `tofu plan`, `-refresh-only` included, takes the lock but never persists state |
| `rw` | a job **holding apply credentials** (`cred_phase = apply`): an **apply** job or a **mutating command** job (§4.3); and the run-less import session (§11.4) | `GET`, `LOCK`, `UNLOCK`, `POST` |

Only an `rw` token writes state: no plan, not even with a leaked or stale plan token, can write state, including during that same run's apply. A plan token cannot take the lock from under that apply either. `exp`: the apply window (`apply_reclaim_window_seconds`, §3.7) for an `rw` job token, 6 h for `ro`/`plan`; 30 min for an import session.

A **run-scoped** token writes state (`POST`) or takes the lock (`LOCK`) only while its run is active: once the run is terminal (e.g. failed as `worker_lost` and its env handed to the next run), a partitioned worker's still-unexpired token gets **403** (defense in depth on top of the TTL). `UNLOCK` stays allowed, so a reclaimed run can still release the lock it holds. Run-less import-session tokens (§11.4) are unaffected — bounded by their own short TTL.

### 11.3 S3 layout

```
s3://stackd-<org>/
  states/{environment_id}/{version_uuid}.tfstate    # SSE-KMS, bucket versioning ON
  logs/{run_id}/{phase}.log.gz
  artifacts/{run_id}/plan.tfplan | plan.json | outputs.json
```

`artifacts/{run_id}/plan.tfplan` is the run's **saved plan** (§7.3): it holds the run's variable values in clear, sensitive ones included, so the API encrypts it **AES-256-GCM with the application key** before it reaches S3 (layout `nonce(12) || ciphertext || tag`, AAD = `stackd:artifact:<s3 key>` so a blob moved under another run's key does not decrypt) — on top of the bucket's SSE. Its key is recorded on the run (`runs.plan_artifact_key`). **Retention**: it lives only while the run can still be applied. On any transition to a terminal state (`finished`, `failed`, `discarded`, `canceled` — whatever the path: worker event, user, scheduler), `transition()` clears `plan_artifact_key` in the same transaction and the object is deleted once that transaction commits (best effort: a failed delete is logged; the blob stays encrypted and the API serves it to nobody, since only an `applying` run's worker can download it). `plan.json`/`outputs.json` are accepted but not archived yet.

`managed_state: false` → no HTTP backend injected, the repo keeps its own `backend` block. For a **partial** backend, set either/both (per-env, applied at init in this order — later `-backend-config` wins): `environments.backend_config_file` (repo-relative `.config`/`.tfbackend` path → `-backend-config=<file>`) and `environments.backend_config` (a `{key: value}` map → one `-backend-config=key=value` each, e.g. `bucket`/`key`/`region`/`dynamodb_table`/`use_lockfile`). The inline map is the way to onboard a new tier on shared code without a per-env `.config` file: set its own `key` so it doesn't collide with another env's state. Mutually exclusive with the managed HTTP backend.

### 11.4 Importing an existing stack

To adopt a stack that already has remote state into Stackd-managed state (`managed_state: true`):
`POST /api/v1/environments/{env_id}/state/import-session` (admin) mints a short-lived (30 min),
run-less `rw` state token and returns a ready-to-use `http` backend config. The operator migrates
their current state with one standard command — `tofu init -migrate-state -backend-config=...` —
which `LOCK`s, uploads the state via §11.2 (stored as a `state_version` with
`created_by_run_id = NULL`), then `UNLOCK`s. Audited as `state.import_session_created`; the address
uses the public URL (the operator runs locally). Once migrated, the committed code must declare **no**
`backend` block: on a managed-state env the agent adds its own `backend "http" {}` (§7.4), and a
second backend block fails `init`. For stacks that should keep their own backend, use
`managed_state: false` (§11.3) instead — no migration needed.

---

## 12. REST API (main surface)

The routes the API serves (checked against the routers: `grep -rn '@.*router\.' api/app`). `{vid}`,
`{hid}`, `{tid}`… are the child resource's id.

```
# Auth
GET  /api/v1/auth/google/start | /callback ; POST /api/v1/auth/refresh | /logout
GET  /api/v1/auth/me ; POST /api/v1/auth/me/onboarded   # marks the walkthrough seen (server-side)
GET  /api/v1/auth/dev/personas ; POST /api/v1/auth/dev/login   # dev only (STACKD_DEV_AUTH), never
                                                               # mounted when STACKD_ENV=production

# OIDC issuer (workload)
GET  /.well-known/openid-configuration ; GET /oidc/jwks

# Spaces & memberships (§2.4, Phase F)
GET|POST /api/v1/spaces                                  # create = instance admin
GET|PUT  /api/v1/spaces/{id}/members ; DELETE /api/v1/spaces/{id}/members/{user_id}

# External secret sources (§15) — writes admin
GET|POST /api/v1/spaces/{id}/secret-sources ; PATCH|DELETE /api/v1/spaces/{id}/secret-sources/{sid}

# Stacks & environments
GET|POST /api/v1/stacks ; GET|PATCH|DELETE /api/v1/stacks/{id}          # DELETE: 409 while a run
POST /api/v1/stacks/{id}/check-repo                                    #   is active (§3.2)
GET|POST /api/v1/stacks/{id}/environments ; GET|PATCH|DELETE /api/v1/environments/{id}
POST /api/v1/environments/{id}/refresh-head        # Git staleness, §9.6
POST /api/v1/environments/{id}/discover-inputs     # shallow clone + HCL parse → placeholder
                                                   #   env variables for required inputs (§20.3)
GET|POST /api/v1/stacks/{id}/variables ; PATCH|DELETE /api/v1/stacks/{id}/variables/{vid}
GET|POST /api/v1/environments/{id}/variables ; PATCH|DELETE /api/v1/environments/{id}/variables/{vid}

# Variable sets
GET|POST /api/v1/variable-sets ; GET|PATCH|DELETE /api/v1/variable-sets/{id}
GET|POST /api/v1/variable-sets/{id}/variables ; PATCH|DELETE /api/v1/variable-sets/{id}/variables/{vid}
GET|POST /api/v1/variable-sets/{id}/attachments ; DELETE /api/v1/variable-sets/{id}/attachments/{aid}
GET /api/v1/environments/{id}/resolved-variables    # merged view + provenance

# Hooks (platform)
GET|POST /api/v1/stacks/{id}/hooks ; PATCH|DELETE /api/v1/stacks/{id}/hooks/{hid}
GET|POST /api/v1/environments/{id}/hooks ; PATCH|DELETE /api/v1/environments/{id}/hooks/{hid}

# Notification targets (outbound Slack/webhook)
GET|POST /api/v1/stacks/{id}/notifications ; PATCH|DELETE /api/v1/stacks/{id}/notifications/{tid}
POST /api/v1/stacks/{id}/notifications/{tid}/test
GET|POST /api/v1/environments/{id}/notifications ; PATCH|DELETE /api/v1/environments/{id}/notifications/{tid}
POST /api/v1/environments/{id}/notifications/{tid}/test

# In-app notifications (§17) — the caller's own feed
GET /api/v1/notifications ; POST /api/v1/notifications/read
DELETE /api/v1/notifications/{nid} ; DELETE /api/v1/notifications     # the latter: clear read ones

# Cloud integrations (OIDC)
GET|PUT|DELETE /api/v1/environments/{id}/cloud-integration
POST /api/v1/environments/{id}/cloud-integration/test    # verification AssumeRole

# Runs
POST /api/v1/environments/{id}/runs      { type?, with_downstream?, commit_sha?, secret_overrides? }
POST /api/v1/environments/{id}/commands  { command, args, commit_sha? }     # §4.3
POST /api/v1/environments/{id}/promote   { from_environment_id }           # §9.7
GET  /api/v1/environments/{id}/runs ; GET /api/v1/runs/{id}
POST /api/v1/runs/{id}/confirm | /discard | /cancel
POST /api/v1/runs/{id}/vcs/resync                                          # §18.5
GET  /api/v1/runs/{id}/logs ?phase=&after_seq=
GET  /api/v1/runs/{id}/plan ; GET /api/v1/runs/{id}/checks
WS   /api/v1/ws                     sub: run:{id}, environment:{id}, user:{own id}

# Dependencies
GET|POST /api/v1/environments/{id}/dependencies          # references with mock_value
POST /api/v1/stacks/{id}/dependencies/link-by-name       { upstream_stack_id, trigger_policy? }
DELETE /api/v1/dependencies/{id}
GET /api/v1/environments/{id}/outputs ; GET /api/v1/graph

# Plan-review comments (§16)
GET|POST /api/v1/runs/{id}/comments ; PATCH|DELETE /api/v1/runs/{id}/comments/{cid}

# Audit (admin)
GET /api/v1/audit ; GET /api/v1/audit/export

# Users & permissions
GET   /api/v1/users              # admin
PATCH /api/v1/users/{id}         # admin: role, allowed_tiers, can_destroy, disabled (audited)
GET   /api/v1/users/mentionable  # minimal directory (id/email/display_name) for @mention, any auth

# Tiers (catalog, §2.4) — listing open; create/update/delete admin
GET /api/v1/tiers ; POST /api/v1/tiers ; PATCH|DELETE /api/v1/tiers/{id}

# Workers & execution queue
GET|POST /api/v1/worker-pools ; DELETE /api/v1/worker-pools/{id}       # admin
GET /api/v1/workers
GET /api/v1/queue          # runs in progress + waiting, with computed blocking reason
                           # (active_run|env_locked|no_compatible_worker|apply_affinity_hold)
POST|GET /api/v1/workers/{id}/diagnostics   # admin: read-only debug bundle (via heartbeat)

# Observability
GET /healthz               # unauthenticated liveness probe
GET /metrics               # unauthenticated Prometheus scrape (§20.1)
GET /api/v1/health         # DB, workers (online + heartbeat), active/waiting runs, recent errors
GET /api/v1/logs           # admin: structured JSON buffer, filters level/event/worker_id/run_id/q

# State
GET /api/v1/environments/{id}/state/versions
POST /api/v1/environments/{id}/state/import-session      # admin, audited (§11.4)
DELETE /api/v1/environments/{id}/state/lock              # force-unlock (admin, audited)

# Webhooks
POST /api/v1/webhooks/github                             # HMAC

# Worker API (agents — detail §7)
POST /worker/v1/register | /heartbeat | /jobs/claim
POST /worker/v1/jobs/{id}/events | /jobs/{id}/logs
PUT  /worker/v1/jobs/{id}/artifacts/{name}               # plan.tfplan (saved plan) | plan.json | outputs.json
GET  /worker/v1/jobs/{id}/artifacts/plan.tfplan          # the apply job's saved plan (§7.3)
POST /worker/v1/commands/{id}/result        # result of a downstream command (diagnostics…)

# State backend HTTP (Terraform — detail §11)
GET|POST /state/v1/{env_id} ; LOCK|UNLOCK /state/v1/{env_id}/lock
```

Specified but **not served yet**: `GET /api/v1/runs/{id}/logs/download` (§5.2) and
`GET /api/v1/run-groups/{id}` (§9.4).

---

## 13. Security — summary

| Surface | Measure |
|---|---|
| Human auth | Google OIDC + PKCE, JWKS/nonce, `hd` restriction, rotating refresh |
| Cloud credentials | **OIDC workload by default**: tokens signed per run/phase, trust policies on claims, session name = run_id (CloudTrail ↔ Stackd audit). Static = fallback |
| OIDC signing key | KMS or encrypted volume (`oidc_signing_keys` §3.11), rotation with overlap, short token TTL. `sub` wildcard allowed **only** on the stack segment; never on `tier` nor `phase` (§10.3) |
| Static secrets | AES-256-GCM, write-only, decrypted at claim, masked in the logs |
| Hooks | platform hooks not bypassable by PR; repo hooks without `sensitive_env` **nor cloud credentials** by default; repo hooks forbidden at the `*_apply` stages on tier=prod; masking of sensitive values in their stdout; timeout; execution in the run's container (§8.3) |
| Mocks | apply forbidden by default (`allow_mock_apply=false`), badge, audit `mock_consumed` |
| External secrets | provider bootstrap credential AES-256-GCM write-only (`secret_sources.bootstrap_secret_encrypted` §15.1); live values fetched at claim, never persisted, masked; **fallback value (static or break-glass override) forbids apply by default** (`allow_fallback_apply=false`), badge, audit `secret.fallback_used` (§15) |
| Workers | revocable tokens, no incoming port, labels (isolated prod pool); tool binaries verified by checksum/signature (§7.4, supply-chain) |
| State | S3 SSE-KMS via API only; per-job state tokens scoped `ro` (proposed runs, read-only commands) / `plan` (lock, never write) / `rw` (apply, mutating command, import session), refused once their run is terminal; handed to tofu only, as `TF_HTTP_*` env (§11.2); audited force-unlock |
| Saved plans | the reviewed `plan.tfplan` is AES-256-GCM-encrypted by the API (AAD bound to its S3 key), served only to the run's apply job, applied as-is (never re-planned), deleted on any terminal state (§7.3, §11.3) |
| Outbound notifications | https only; the host must resolve to public addresses only; the POST dials the vetted IP (Host/SNI/certificate stay on the hostname), ignores env proxies (`HTTPS_PROXY`…) by design, never follows redirects |
| Apply permissions | tier per env × `allowed_tiers` set per user (§2.4, configurable catalog, non-linear); distinct `can_destroy`; double lock with the OIDC trust policy on the tier name |
| Protected envs | autodeploy forbidden, 4-eyes (automatic on a tier flagged `requires_four_eyes`, e.g. `prod`), never bypassed (cascade included); the *right* to apply comes from the tier, not from `protected` |
| Proposed runs | plan-only, state RO, secrets not injected by default, mocks allowed |
| Webhooks | HMAC SHA-256 (secret per repo, `stacks.webhook_secret_encrypted` §3.1), anti-replay 5 min |
| Sessions | access JWT 15 min in Bearer header; refresh httpOnly `SameSite=Strict` + CSRF double-submit, rotation with reuse detection → family revocation (§2.5) |
| Encryption at rest | AES-256-GCM, random 96-bit nonce per value, never reused (§1) |
| Rate limiting | Google callback + `/auth/refresh`, `/webhooks/github`, `discover-inputs`: per-IP token buckets, per replica (§20.3). Worker register/claim are not throttled yet; hardening Phase 7 |
| Audit | append-only **at the DB level** (INSERT-only role + anti-update/delete trigger §6.1), denormalized, transactional, 2 years |

---

## 14. Tests — minimal strategy

- **API unit tests**: transitions (`can_apply` per tier including apply-everywhere-except-prod, `can_destroy`, auto 4-eyes on a `requires_four_eyes` tier **and its human-triggerer-only scope**, mock-apply blocking, warn → forced confirmation), 6-layer variable resolution + provenance (space-scoped attachments), hook merging (platform→repo order, exclusion of `*_apply` repo hooks on prod), cycle detection, multi-parent cascade, mock resolution (real > mock > error), signature/claims of workload tokens (tier segment in the `sub`, wildcard limited to the stack segment), Google id_token validation, **refresh rotation + reuse detection → family revocation**.
- **Integration**: Postgres testcontainers; **concurrent claim → only one wins, the loser catches `23505` and re-polls**; HTTP backend with real `tofu` + Garage; log idempotency; **masking of sensitive values (`tfvars` + env) in a hook's stdout**; `after_plan` hook that reads plan.json; **WS fan-out via `LISTEN/NOTIFY`**; **single execution of a periodic task under advisory lock with 2 replicas**; AssumeRoleWithWebIdentity against a mock STS (moto).
- **E2E**: the live compose stack + fixture repos (built-in `terraform_data`, no provider) → on a throwaway copy of the demo graph, a 2-stack cascade **with mocks** (mocked plan → upstream apply → real cascade), the role/mock gates, and the `run.confirmed` audit actor (DEV §7).
- **Agent**: exit codes, secret masking, saved-plan upload and fail-closed apply, commit pinning, the `TF_HTTP_*` backend env (tofu only), the command time budget from `cred_phase`, token encoding in clone URLs, writing the OIDC token and exporting the env vars.

---

## 15. External secret sources (post-MVP)

> Status: **post-MVP**, additive. This section extends the variable model (§3.3), the claim
> build (§7.2) and the security model (§13). It reuses the mock apply-gate pattern (§9.3)
> verbatim — read it first; everything here is the same shape applied to externally-sourced
> secrets instead of mocked outputs.

### 15.1 Goal and model

A sensitive variable can carry its value **by reference** to an external secrets manager instead
of storing the value in Stackd. At claim build the platform resolves the reference to a live value,
injects it into the job exactly like any other sensitive variable, and **never persists it**. This
is a security upgrade over a stored `value_encrypted`: no secret at rest in our DB, automatic
rotation, single revocable bootstrap credential per source.

The provider is abstracted — Proton Pass is the first concrete implementation, others (HashiCorp
Vault, AWS Secrets Manager, GCP Secret Manager, 1Password Service Accounts, Doppler, Infisical)
plug into the same interface.

**New table `secret_sources`** (scoped per space, like `cloud_integrations` §3.10):

```sql
CREATE TABLE secret_sources (
    id                        uuid PRIMARY KEY DEFAULT uuidv7(),
    space_id                  uuid NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    name                      text NOT NULL,
    provider                  secret_provider NOT NULL,   -- enum: proton_pass | vault | aws_secrets_manager | ...
    config                    jsonb NOT NULL DEFAULT '{}',-- non-sensitive: e.g. proton {server,vault_scope}; vault {address,mount,auth}
    bootstrap_secret_encrypted bytea NOT NULL,            -- AES-256-GCM, write-only: Proton PAT / AI Access Token, Vault token, ...
    created_by_user_id        uuid REFERENCES users(id),
    created_at                timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now(),
    UNIQUE (space_id, name)
);
```

`bootstrap_secret_encrypted` follows the §1 encryption-at-rest rule and is **never returned** by
the API (write-only, like `stacks.webhook_secret_encrypted`).

**Variable extension** (`variables`, §3.3) — a third value source alongside `value` /
`value_encrypted`:

```sql
ALTER TABLE variables ADD COLUMN secret_source_id          uuid REFERENCES secret_sources(id) ON DELETE RESTRICT;
ALTER TABLE variables ADD COLUMN secret_ref                text;   -- provider locator, e.g. Proton "pass://vault/item/field"
ALTER TABLE variables ADD COLUMN secret_fallback_mode      secret_fallback NOT NULL DEFAULT 'error';  -- error | static | break_glass
ALTER TABLE variables ADD COLUMN secret_fallback_encrypted bytea;  -- AES-256-GCM, only for mode=static
-- exactly one value source; a referenced variable is implicitly sensitive
ALTER TABLE variables ADD CONSTRAINT variables_one_value_source CHECK (
    (value IS NOT NULL)::int
  + (value_encrypted IS NOT NULL)::int
  + (secret_source_id IS NOT NULL)::int = 1
);
-- ON DELETE RESTRICT: a source in use can't be dropped out from under live variables.
```

A variable with `secret_source_id` is treated as `sensitive=true` regardless of the flag.

### 15.2 Resolution order (real > fallback > error)

At claim build (`build_job_payload`, §7.2), reference variables resolve **after** the existing
layered resolution (§3.4), by calling the provider:

1. **Provider reachable, secret found** → use the live value. `variable_provenance[name] =
   "secret:{source_name}"`. No audit event on success (would be noisy — successes are implicit in
   the run; only failures and fallbacks are audited).
2. **Provider unavailable / timeout / not-found** → apply `secret_fallback_mode`:
   - `error` (**default**) → the claim resolution fails: the run transitions to `failed` with
     reason `secret_unavailable:{var}`; the claim returns no job and the worker re-polls. Fail-closed.
   - `static` → use `secret_fallback_encrypted` (the operator-chosen value). Sets
     `run.used_secret_fallback = true`, provenance `secret_fallback:{source_name}`, audit
     `secret.fallback_used`.
   - `break_glass` → no stored value: the run can only proceed if the trigger carried an inline
     override (see §15.4). Without one it behaves like `error`.

This is the same `real > mock > error` precedence as §9.3, with "fallback" in the middle slot.

A provider fetch has a short timeout (default 10 s); a slow source counts as unavailable. Resolved
values are **never cached to disk or DB** — caching would re-introduce a secret at rest; an
optional in-process memoisation lives only for the duration of one claim build.

### 15.3 Where resolution runs

**v1: API-side.** The platform resolves references during claim build and injects the live value
into the existing payload fields — `sensitive_env` / `tfvars_json` — and the literal into
`mask_values` (§5.1). **No worker or claim-payload schema change**: a resolved reference is
indistinguishable downstream from a stored sensitive variable. The bootstrap credential stays on
the API host and is never shipped to a worker. For Proton Pass this means the API host runs
`pass-cli` (headless, authenticated by the source's PAT / AI Access Token); for Vault/ASM it is a
plain HTTPS call.

**Future: worker-side** (mirrors OIDC→STS, §10.4) — ship `{provider, config, scoped_token,
secret_ref}` in the claim and let the worker fetch locally, so the value never transits the API.
Deferred: it widens bootstrap-credential exposure to every worker and complicates the fallback path,
for a marginal gain over the masked API-side flow. Noted here so the payload contract can grow
compatibly.

### 15.4 Break-glass override (operator-supplied value)

When a source is down and the operator must ship anyway, the run **trigger** accepts an inline
override (it is never stored, used for that run only):

```
POST /api/v1/environments/{id}/runs
  { ..., "secret_overrides": { "<variable_name>": "<value>" } }
```

- **Permission**: supplying an override is an apply-affecting bypass → requires
  `can_apply(user, env)` (§2.4). It is rejected unless the targeted variable's
  `secret_fallback_mode = break_glass` (a source must opt in to being overridable).
- The override value is injected like a sensitive variable, added to `mask_values`, and sets
  `run.used_secret_fallback = true`, provenance `secret_override:{source_name}`.
- Audit `secret.fallback_overridden`, `context = {variable, source, run_id}` — **the value itself
  is never written to the audit context** (§6.1).

No new run state is introduced: break-glass reuses the trigger path, so `transition()` legality
(invariant) is untouched.

### 15.5 Apply gate (mirrors §9.3)

A value produced by any fallback is **not the real secret** (it may be stale or operator-chosen), so
it must not reach prod silently:

```sql
ALTER TABLE runs         ADD COLUMN used_secret_fallback bool NOT NULL DEFAULT false;
ALTER TABLE environments ADD COLUMN allow_fallback_apply bool NOT NULL DEFAULT false;
```

`confirm_run()` enforces, in the same place as the mock gate:

```python
if run.used_secret_fallback and not env.allow_fallback_apply:
    raise ProblemException(409, "Apply disabled",
        "This run resolved a secret via fallback (allow_fallback_apply is off).")
```

The UI shows a provenance badge on any value sourced from a fallback (DESIGN — same treatment as the
mock badge). Plan of a `used_secret_fallback` run is always allowed; only apply is gated.

### 15.6 Audit taxonomy additions (extends §6.2)

```
secret_source.created / secret_source.updated / secret_source.deleted / secret_source.token_rotated
secret.fallback_used          -- static fallback consumed (provider was down)
secret.fallback_overridden    -- operator supplied a break-glass value
```

A run failed because a reference could not resolve and no fallback applied is audited as
`run.apply_failed` with context `reason: secret` (§6.2), not by a dedicated `secret.*` action.

Successful live resolutions are intentionally **not** audited (volume); the run's
`variable_provenance` already records `secret:{source}` per variable.

### 15.7 Proton Pass binding

- `provider = proton_pass`; `config = {server?, vault_scope?}`; bootstrap credential = a Proton
  **Personal Access Token** or **AI Access Token**, scoped to the relevant vault/items (no account
  password). `secret_ref` uses Proton's URI form `pass://vault/item/field`.
- Integration runs `pass-cli` on the resolver host (API for v1); the binary must be present in the
  API image and is verified by checksum (supply-chain, §7.4).
- The Proton end-to-end model is preserved: decryption happens client-side via the token, Stackd
  only ever holds the (encrypted-at-rest) token and the reference, never the user's master key.

### 15.8 Invariants preserved

1. Secrets never logged nor returned in clear (invariant §13.3): live value, static fallback and
   break-glass override all enter `mask_values`; `bootstrap_secret_encrypted` and
   `secret_fallback_encrypted` are write-only.
2. Every mutating action audited in the same transaction (§6.3): source CRUD and fallback events.
3. A non-real value cannot be applied silently (§9.3 parity): `used_secret_fallback` ×
   `allow_fallback_apply`.
4. Run state only changes through `transition()` (invariant §4.2): no new state; failure path uses
   `→ failed`, break-glass uses the normal trigger path.

---

## 16. Plan review — comments & highlights (post-MVP)

> Status: **post-MVP**, additive. Turns the approval gate into a collaborative review: an open
> discussion thread on a run, with comments optionally **anchored** to a part of the plan. Built on
> existing substrate — immutable run logs (the anchor target), `LISTEN/NOTIFY` (live updates, §5.3),
> the approval gate (§4). No run-state change; `transition()` is untouched.

### 16.1 Model

```sql
CREATE TABLE run_comments (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),
    run_id              uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    parent_id           uuid REFERENCES run_comments(id) ON DELETE CASCADE,  -- thread reply
    author_user_id      uuid REFERENCES users(id),
    author_email        text,            -- denormalized, readable if the user is later removed
    body                text NOT NULL,
    anchor              jsonb,           -- NULL = general comment; else a polymorphic anchor (§16.2)
    resolved_at         timestamptz,
    resolved_by_user_id uuid REFERENCES users(id),
    created_at          timestamptz NOT NULL DEFAULT now(),
    edited_at           timestamptz
);
```

Comments are **per run** — each plan is a distinct artifact, so a new run starts a fresh discussion.

### 16.2 Anchors (polymorphic)

An anchor pins a comment to a part of the plan. Two kinds (a comment with no anchor is a general
thread):

```jsonc
// v1 — a range of plan log lines (the rendered, already-masked plan output)
{ "kind": "plan_line", "phase": "planning", "seq": 3, "line_start": 12, "line_end": 14,
  "snippet": "~ aws_s3_bucket.logs" }
// v2 — a Terraform resource address (from the structured plan.json)
{ "kind": "resource", "address": "aws_s3_bucket.logs", "action": "update" }
```

The `plan_line` anchor targets `run_logs` (`(run_id, phase, seq)` is immutable and idempotent, §5.2),
so the anchor is stable for the life of the run. The `snippet` is copied from the **masked** log line
(`run_logs.lines[].msg`), so an anchor can never carry a secret (invariant §13.3). `resource` anchors
(v2) require serving the structured plan; the column is polymorphic so v2 needs no migration.

**Rendering**: `plan_line` threads render **inline** in the log viewer, directly under their line
(progressive, line-level review — like PR-review comments), with a hover affordance per line to open
an inline composer. General (unanchored) and `resource` threads live in a separate Discussion panel.

### 16.3 API

`/api/v1/runs/{run_id}/comments` — listing & posting are open to any authenticated user
(collaboration, reader+); editing a body is author-only; resolving a thread needs the author or
`role ∈ {approver, admin}`; deleting needs the author or admin.

| Method | Path | Who | Effect |
|---|---|---|---|
| GET | `/comments` | any auth | list (oldest first; replies carry `parent_id`) |
| POST | `/comments` | any auth | create `{body, anchor?, parent_id?}` |
| PATCH | `/comments/{id}` | author / approver | edit `body` (author) and/or `resolved` (author or approver+) |
| DELETE | `/comments/{id}` | author / admin | delete (cascades replies) |

Every create/edit/delete/resolve emits a light `pg_notify('run_<id>', …)` (§5.3) so the run page's
existing WS subscription invalidates `["run-comments", run_id]` and the thread updates live.

### 16.4 Interaction with the gate

**Advisory** at v1: the run page shows the count of open (unresolved) threads next to the Confirm
button; it does **not** block. (`can_apply` and the §9.3/§15.5 gates are untouched.) A future opt-in
env flag could make confirm require all threads resolved — deliberately deferred.

### 16.5 Invariants preserved

1. No run-state change — comments are a side record; `transition()` (invariant §4.2) is untouched.
2. Snippets come from already-masked log lines, so no secret leaks (invariant §13.3).
3. Live updates reuse `LISTEN/NOTIFY` (§5.3) — no new transport.

---

## 17. In-app notification center (post-MVP)

> Status: **post-MVP**, additive. The **inbound** counterpart to the outbound Slack/webhook outbox
> (§5/notifications): a per-user inbox surfaced as a bell in the UI. Side records only — no run-state
> change (`transition()` untouched), live over the existing `LISTEN/NOTIFY` transport (§5.3).

### 17.1 Model & events

`user_notifications` (per user): `user_id`, `kind`, `run_id?`, `comment_id?`, `context jsonb`,
`read_at?`, `created_at`. `kind ∈ approval_request | run_finished | run_failed | comment_reply |
mention`. Each row is created **in the same transaction** as its trigger:

- **`transition()`** (§4.2): `→ unconfirmed` fans out `approval_request` to every eligible approver
  — the users who pass `can_apply` on the env (§2.4), evaluated with their **effective** space
  membership (members of the env's space, instance admins included); a `destroy` run additionally
  requires `can_destroy`; excluding the triggerer. A terminal state notifies the run's human
  triggerer (`run_finished` / `run_failed`).
- **comment create** (§16): a reply notifies the root author; `@mention` tokens (matched against a
  user's email local-part or full email) notify the mentioned users. Never the author. The composer
  offers `@` autocomplete from `GET /users/mentionable` (a minimal directory readable by any auth),
  inserting `@<email-local-part>` so it resolves against the parser.

### 17.2 Live delivery — private per-user channel

`notify()` emits `pg_notify('user_<uid>', …)` alongside the row. The WS hub maps a `user:<id>`
subscription to `user_<id>` **only for the connecting user's own id** (`channel_for(sub, user_id)`,
§5.3) — the feed is private; you cannot listen on someone else's signals. The bell subscribes to its
own channel and invalidates the `["notifications"]` query on any signal (no payload trusted).

### 17.3 API

`GET /api/v1/notifications` (current user, newest first, capped) ; `POST /api/v1/notifications/read`
(`{ids?}` — omit to mark all of the user's unread as read). Opening the bell marks all read.

### 17.4 Invariants preserved

1. No run-state change — notifications are a side record; `transition()` (invariant §4.2) untouched.
2. Same-txn write (invariant §6.3 spirit): a rolled-back trigger emits no notification (NOTIFY is
   transactional).
3. The per-user channel is access-scoped to self; the WS payload is a content-free signal (§5.3).

## 18. VCS feedback loop — PR comment + commit status (post-MVP)

> Status: **shipped** (Phase A). Closes the review loop: a PR shows the plan result *in GitHub* — a
> commit **status** + one PR comment edited in place. **Auth: PAT** (the stack's `repo_secret`), not
> a GitHub App. A VCS post-back is a side-effect of `transition()`, never a source of truth: a VCS
> outage leaves the run correct. GitHub App (bot identity + Checks API) is deferred to Phase I.

### 18.1 Model

`runs` (add): `pr_number int?` (the PR that spawned a `proposed` run), `vcs_provider text?`
(`'github'` — enum-by-string, gitlab/bitbucket later), `vcs_comment_id bigint?` (the posted PR
comment, for idempotent edit), `vcs_head_sha text?` (PR head commit the status reports against).
`vcs_outbox` (transactional outbox, same shape as the notification outbox §17/§5): `id`, `run_id`,
`to_state`, `attempts`, `created_at`, `sent_at?`, partial index `WHERE sent_at IS NULL`.

### 18.2 Auth — PAT

Reuse the stack's `repo_secret` (already used to clone). For post-back it must additionally carry
**`pull_requests:write` + commit `statuses:write`** (classic `repo`, or a fine-grained PAT with
those + `contents:read`). No GitHub App, no extra instance config — posts appear as the token's
user. A `repo_secret` without write scope → post-back fails soft (run unaffected, logged).

### 18.3 Webhook ingestion (`webhooks/router.py`)

On `pull_request` (`opened`/`synchronize`/`reopened`): create the `proposed` run as today **and**
persist `pr_number`, `vcs_provider='github'`, `vcs_head_sha = pr.head.sha`. On `closed`: best-effort
`transition()` of the still-in-flight proposed runs for that PR to `canceled` (reason `pr_closed`).

### 18.4 Post-back (`app/vcs/` — transactional outbox)

Enqueued on the run `transition()` in the SAME txn (**no network I/O there** — a rolled-back
transition never posts); drained by the scheduler dispatcher (`dispatch_vcs`, advisory-locked,
at-least-once with `attempts` cap). Only runs with `vcs_provider` set post back. A `proposed` run is
**plan-only and terminal at `finished`** (never `unconfirmed`). Enqueued on `planning`, `finished`,
`failed`, `canceled`, `discarded` (intermediate states elided to avoid status churn).

- **Commit status** (Status API — PAT-compatible) on `vcs_head_sha`: `planning → pending`;
  `finished → success`; `failed → failure`; `canceled|discarded → error`.
  `POST /repos/{o}/{r}/statuses/{sha}` with `{state, target_url=<ui>/runs/{id}, context="stackd/plan",
  description="+a ~c -d"}`. (The richer **Checks API** is App-only → deferred.)
- **PR comment**: one comment per run, **edited in place** (`vcs_comment_id`): the `+a ~c -d`
  summary, a mocks-block-apply note, deep link to `/runs/{id}`. Created on first post
  (`POST .../issues/{pr}/comments`), updated on later transitions (`PATCH .../issues/comments/{id}`);
  the returned id is persisted back on the run for idempotent edits.
- A VCS failure **never** fails the run (logged, retried up to the attempts cap).

### 18.5 API

- `POST /api/v1/webhooks/github` — unchanged contract, now also persists PR metadata.
- `POST /api/v1/runs/{id}/vcs/resync` (writer) — re-enqueue the post-back for the run's current
  state (manual recovery).

### 18.6 Invariants preserved

1. Post-back is a side-effect of `transition()`, never a source of truth; a VCS outage leaves the
   run correct (mirrors §17.4).
2. Same-txn enqueue (invariant §6.3 spirit): a rolled-back transition enqueues nothing.
3. Sensitive plan values stay masked in the comment (reuse the artifact masking, §13).

## 19. Drift detection (post-MVP)

> Status: **shipped** (Phase B). Detects when real infrastructure diverged from the last applied
> state, via a periodic **read-only** `-refresh-only` plan. **Never auto-remediated** — drift is
> surfaced (env badge + inbox), the operator decides. Reuses the `proposed` run machinery; no new
> job type.

### 19.1 Model

`environments` (add): `drift_status text default 'unknown'` (`unknown | in_sync | drifted | error`),
`last_drift_checked_at timestamptz?`, `drift_run_id uuid?` (the proposed run that last detected
drift), `drift_check_enabled bool default true`. `runs` (add): `is_drift bool default false`.
Config: `STACKD_DRIFT_INTERVAL_SECONDS = 21600` (6h) — per-env minimum spacing. New trigger source
`TriggeredBy.schedule`; new inbox kind `drift_detected`.

### 19.2 Scheduler task (`detect_drift`, advisory-locked like the others, §7.5)

On the 10s loop, gated on `last_drift_checked_at + interval`: for each `drift_check_enabled`,
unlocked env with **no active run**, bump `last_drift_checked_at` (so a no-op env isn't retried
every tick) and — if the env has an applied commit — enqueue a read-only `proposed` run pinned to
that commit (`is_drift=true`, `triggered_by=schedule`). An env with nothing applied stays `unknown`.

### 19.3 Worker & claim

No new job type: the `proposed` plan runs with **`-refresh-only`** (true drift = state vs reality,
not state vs code), signalled by `refresh_only` in the claim payload. To keep drift behind user
work, the claim order-by (§7.2) sorts `is_drift` ascending — a queued user run is always claimed
before a background drift run.

### 19.4 Outcome (applied in the run's `transition()` txn)

On the drift run completing: changes → `drifted` (set `drift_run_id`, audit `environment.drift_detected`
+ fan out `drift_detected` to the same eligible approvers as `approval_request` (§17.1: `can_apply`
with effective space membership), **once** per edge into drift — debounced while it
stays drifted); no changes → `in_sync`; the plan itself failing → `error`. A successful **apply**
on the env reconciles state with reality → back to `in_sync`, `drift_run_id` cleared
(`environment.drift_cleared`).

### 19.5 API / front

`drift_check_enabled` is settable on env create/update; the env out carries `drift_status` +
`last_drift_checked_at` + `drift_run_id`. Front: a neutral drift chip (icon + label, never colour
alone — DESIGN §7) on the env header, and a "drifted only" filter on the env list.

### 19.6 Invariants preserved

1. Drift runs are **read-only** and **never auto-applied**.
2. One active run per env unchanged (§3.5) — a drift run is skipped when a run is already active,
   and yields to user runs at claim time.
3. The env status change rides the run `transition()` txn — a rolled-back completion changes
   nothing; the notification is same-txn (§17.4).

---

## 20. Observability & API guardrails (post-MVP, Phase H)

### 20.1 Metrics — `GET /metrics` (Prometheus)
Unauthenticated scrape endpoint (restrict at the network layer — it exposes only bounded counts,
**never** secrets or tfvars). `prometheus-client`. Cardinality is bounded: labels are state/phase/
result only, never a run id.
- `stackd_runs_total{state}`, `stackd_queue_depth`, `stackd_workers_online` — gauges refreshed from
  the DB at scrape time (cheap aggregate queries).
- `stackd_claim_latency_seconds` — histogram, observed in `claim_one` (queue wait = now − created_at).
- `stackd_run_duration_seconds{phase}` — histogram, observed at the terminal `transition()` (claim →
  terminal); `phase` = `apply` if the run was confirmed, else `plan`.
- `stackd_webhook_total{result}` — counter (`accepted`/`rejected`/`no_match`) in the webhook handler.

### 20.2 Tracing (OpenTelemetry, OTLP)
`setup_tracing(app)` wires FastAPI + SQLAlchemy instrumentation and a `BatchSpanProcessor` →
`OTLPSpanExporter` **only when `STACKD_OTLP_ENDPOINT` is set**; a complete no-op otherwise (the OTel
stack is imported lazily). Spans link an HTTP request → run `transition` → claim → worker events.

### 20.3 Guardrails
- **Rate limiting** (`app/ratelimit.py`): an in-process token bucket per (limiter, client IP),
  per-replica (no Redis in the MVP). The buckets live in an LRU hard-capped at 10 000 keys under a
  lock (the least recently used bucket is evicted at the cap), so memory stays bounded. Applied to `auth/google/callback` + `auth/refresh` (30/min,
  burst 15), `webhooks/github` (120/min, burst 40), and `discover-inputs` (10/min, burst 5). 429
  problem+json when empty. `/me` is intentionally unthrottled (the UI polls it). Dev login is not
  throttled (dev-only, dropped in prod).
- **Discovery clone caps**: the input-discovery clone is already `--depth 1 --single-branch` +
  30 s-bounded; Phase H adds a post-clone **size budget** (`STACKD_DISCOVERY_MAX_REPO_MB`, default
  200 → 413) and a **`.tf` file cap** (`STACKD_DISCOVERY_MAX_TF_FILES`, default 500 → 413).

### Invariants
`/metrics` and traces never include secret values or tfvars; metrics are cardinality-bounded (label
by state/phase/result, never by run id).
