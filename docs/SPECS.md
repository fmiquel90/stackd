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
8. Front  → POST /api/v1/auth/refresh (the callback only sets the cookies and redirects to the
            SPA: the access token comes from this first refresh), then GET /api/v1/auth/me
```

**CSRF**: the access token travels in the `Authorization: Bearer` header (no CSRF risk on API calls). The refresh relies on an httpOnly cookie → `/auth/refresh` and `/auth/logout` require `SameSite=Strict` **and** a double-submit CSRF token (the only pair of cookie-bound endpoints). Refresh cookie `stackd_refresh`: httpOnly, `Path=/api/v1/auth`; CSRF cookie `stackd_csrf`: readable by JS (`Path=/`, so the SPA can echo it in `X-CSRF-Token` on boot); both `SameSite=Strict`, `Secure` when `STACKD_ENV=production`.

Bootstrap: the first user of an allowed domain = `admin`, subsequent ones = `reader`. No local auth; internal `AuthProvider` interface for other IdPs later. Login/logout/denial → `audit_events`.

### 2.2 Table `users`

```
id uuid PK, google_sub text unique, email text, display_name, avatar_url,
role enum(admin|approver|writer|reader),    -- global capabilities (§2.3)
allowed_tiers text[] not null default '{}',  -- set of tiers where the user can confirm an apply (§2.4)
can_destroy bool default false,             -- right to trigger/confirm a destroy run (§2.4)
disabled bool, last_login_at,
onboarded_at timestamptz nullable,          -- first-login walkthrough seen (POST /auth/me/onboarded)
created_at
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

> **Per-space RBAC (Phase F).** `users.role`/`allowed_tiers`/`can_destroy` are **instance defaults**. The effective permission is the caller's `space_memberships` row for the resource's space when present (`UNIQUE (space_id, user_id)`; columns `role`, `allowed_tiers text[]`, `can_destroy`). Every list/get/mutate resolves the space from the resource (stack→space, env→stack→space, run→env→…) and requires membership; an **instance admin** reaches every space. List endpoints are filtered to the caller's accessible spaces. `can_apply(user, env, membership)` uses the *effective* role/tiers/can_destroy. A migration backfills one membership per existing user × existing space (no behaviour change); a newly-provisioned user is enrolled into every existing space with their instance defaults. Space listing/creation + membership management live under `/api/v1/spaces` (create = instance admin; member management — `GET|PUT /spaces/{id}/members`, `DELETE /spaces/{id}/members/{user_id}` — = space admin or instance admin). Per-stack grants and the OPA engine stay Phase 7.
> A create request naming an explicit `space_id` that does not exist gets **404** (after the membership gate, so a non-member still gets 403).
> **Residual (Phase F)**: `GET /api/v1/audit` (and its CSV export) stays instance-wide and is
> **admin-only** — audit rows carry no `space_id` (proper scoping needs a denormalization
> migration). The WS `run:`/`environment:` subscriptions are membership-gated like the REST routes
> (§5.3, §17.2); everything that returns config/secret/output data is membership-gated.

> **Set-based, not linear** (evolved from the original MVP design): tiers are a **configurable catalog** (`tiers` table — `name` unique, `requires_four_eyes`, `repo_hooks_at_apply` (§8.3), `position`), not a fixed `dev<staging<prod` enum, and a user carries a **set** of allowed tiers, not a single ceiling. This expresses custom tiers (`qa`, `preprod`, `sandbox`) and **non-contiguous** grants (e.g. `dev`+`prod` but not `staging`) that the old linear ceiling could not.

- `environments.tier` text — references `tiers.name` (validated against the catalog on env create/update). No ordering.
- `users.allowed_tiers` text[] — the set of tiers where the user can **confirm an apply**. Empty = none (can read and plan, never apply). Each entry is validated against the catalog; deleting a tier — refused (409 `Tier in use`) while an environment still references it — strips it from every user's set and every space membership's (no FK on the array, so this is enforced in app code).
- Apply rule: `confirm` allowed iff `role ∈ {approver, admin}` **AND** `env.tier ∈ allowed_tiers` (set membership). Example: an approver with `{dev, staging}` confirms in dev/staging, denied in prod (the dev persona Bob holds that set but is a `writer`: he confirms nowhere, DEV §3); Alice `{dev, staging, prod}` confirms everywhere; a user `{dev, prod}` skips staging.
- `users.can_destroy` — a run `type=destroy` (trigger AND confirm) requires `can_destroy=true` **in addition** to the tier rule. A destruction is more dangerous than an apply: a distinct, explicit right.

**Relationship with `protected`**: we **decouple** sensitivity and access right. `environments.protected` carries only its own effects — forcing confirmation (no autodeploy) and enabling 4-eyes; *who* can confirm comes from the tier set. An env can be `tier=prod` without being `protected` and vice versa. "Forcing confirmation" means a **named human decides** to apply: an approver typing a confirm on the run, or — for an environment rollout (§21.5) — an approver who approved that rollout with this environment in it, under four-eyes against its creator. A rollout's approval only ever confirms a plan that creates resources in an environment never applied before; it is never `autodeploy`.

**4-eyes / tier consistency**: a tier with `requires_four_eyes=true` (seeded on `prod`; was hardcoded to the prod tier, now a per-tier flag) forces self-confirmation off by default (the triggerer ≠ the confirmer), as do a `protected` env and the per-env `require_second_pair_of_eyes`. The check **fails closed**: if an env points at a tier that no longer exists in the catalog, four-eyes is required rather than silently dropped.

**Scope of 4-eyes**: the "triggerer ≠ confirmer" rule only bites on runs with a **human** triggerer (`triggered_by=manual`, or `triggered_by=rollout` whose `trigger_user_id` is the rollout's creator, §21.3; `trigger_user_id` set). For a rollout, the rule also holds between its **creator and its approver** — at the approval, and again at each auto-confirmation (§21.5, §21.6). A run with no human at its origin (`webhook`, `dependency`) has no `trigger_user_id`: any confirmer authorized at the tier can confirm it (there is no one to oppose). This is not a bypass — the access guard remains the tier (`can_apply`); 4-eyes only prevents *the same person* from triggering **and** confirming.

**Double-lock boundary**: when workload OIDC is active (§10), the apply restriction must **also** live in the AWS trust policy (`sub` claim filtered on `run:<tier>:*:apply`), not only in Stackd — otherwise bypassing the API bypasses the control. Both layers express the same rule; the `tier` **name** feeds the token's `sub` (§10.2). Tier names are charset-constrained (`^[a-z0-9][a-z0-9-]*$`, no `:`/whitespace) precisely so a crafted name can't alias another tier's `sub` segment.

Per-env option `require_second_pair_of_eyes`: the triggerer cannot confirm (forces 4-eyes on a tier that does not itself require it, without `protected`'s ban on autodeploy).

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

First level of the hierarchy (breadcrumb `space / stack / env / run`, DESIGN §4) and parent of `stacks`, `variable_sets`, `worker_pools`, `secret_sources`. The bootstrap seed (`app.seed`, with the default tier catalog) creates a `default` space — and a `demo` space, used by the dev demo data (DEV §7); a create request without `space_id` lands in `default`. **Per-space RBAC** shipped in Phase F (§2.4): `space_memberships`, `GET|POST /api/v1/spaces` (create = instance admin, who becomes the new space's first `admin` member) and member management. Space rename/delete and Google group mapping are not exposed; per-stack grants and OPA stay Phase 7. All `space_id` FKs point to a space from Phase 1.

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
tool enum(opentofu|terraform) default opentofu, tool_version text,
labels jsonb nullable,                     -- org/selection tags, matched by a variable set's selector (§3.4)
created_at, updated_at
```

> Neither branch, nor state, nor autodeploy: everything goes down into the environment.

> **Incoming webhook & shared repo**: one repo can serve several stacks (distinct `project_root`). Since the GitHub/GitLab webhook is configured **per repo** with a unique secret, `webhook_secret_encrypted` is shared across these stacks (same value). On receipt (`POST /api/v1/webhooks/github`, §12), the API resolves the stacks whose `repo_url` matches the payload, verifies the HMAC against their common secret, then filters environments by branch and `project_root` (§9.6).

### 3.2 `environments` — the executable instance

```
id uuid PK, stack_id FK, name text unique(stack),       -- dev, staging, prod
tier text references tiers.name ON DELETE RESTRICT,  -- §2.4: carries the apply/destroy
                                   --   permissions (catalog, not enum)
branch text,                       -- branch tracked by THIS env
autodeploy bool,                   -- forced false if protected (a rollout's approval is not
                                   --   autodeploy: §2.4, §21.5)
protected bool,                    -- §2.4: forces confirmation + 4-eyes (NOT the access control → tier)
require_second_pair_of_eyes bool,
managed_state bool default true,
allow_mock_apply bool default false,  -- §9.3: allow the apply of a run having consumed mocks
head_sha text nullable,               -- §9.6: known head of the tracked branch
head_updated_at timestamptz nullable,
commits_ahead int nullable,           -- nb of commits between last apply and head
affects_project_root bool nullable,   -- do the commits ahead touch this project_root?
locked bool, labels jsonb, position int,
created_at, updated_at
```

The external-secret (`allow_fallback_apply`, §15.5), unmanaged-backend (`backend_config_file`, `backend_config`, §11.3) and drift (§19) columns are listed in their own sections.

Why not Terraform workspaces: weak isolation (shared code/backend/credentials, state suffix), easy targeting mistakes. A Stackd environment = physically separate state, variables, protections and its own worker pool.

**Deletion.** Deleting an environment — or a stack, which takes its environments with it — is refused with **409 `Active run`** (problem+json carrying `run_id`) while one of them has a run in an active state (§3.5): the cascade would remove that run outside `transition()` (§4.2) while a worker executes it — and with **409 `Active rollout`** (carrying `rollout_id`) while one is a member of a rollout in progress (§21.3). Queued runs go with their environment. Everything that targets what is deleted by `target_kind`/`target_id` (no FK) is removed in the same transaction, each removal audited with a `reason` (`stack_deleted` | `environment_deleted`): variable-set attachments (§3.4, `variable_set.detached`), platform hooks (§8.1, `hook.deleted`) and notification targets (`notification_target.deleted`). The deletion holds the stack/env rows `FOR UPDATE`; attaching one of these to a stack or an env locks that row `FOR KEY SHARE` until it commits, so an attach either waits and then finds no target (404) or commits first and is removed by the deletion — never an orphan.

### 3.3 `variables` — stack and environment level

```
id uuid PK,
stack_id FK nullable,              -- set for stack/env vars
environment_id FK nullable,        -- NULL = common to the stack; set = env override
variable_set_id FK nullable,       -- set for the vars of a set (§3.4)
kind enum(terraform|environment),  -- terraform → TF_VAR_/tfvars; environment → env var
name text, value text nullable, value_encrypted bytea nullable,
sensitive bool, hcl bool
-- + the external-secret reference columns (secret_source_id, secret_ref, fallback — §15.1)
-- CHECK: exactly one value source (value, value_encrypted or secret_source_id — §15.1)
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
triggered_by enum(manual|webhook|dependency|api|schedule|rollout), trigger_user_id nullable,
                                     -- 'api' reserved: no application tokens/PAT at MVP (auth = human
                                     -- Google + worker), set when programmatic triggering arrives
                                     -- (Phase 7); 'schedule' = drift runs (§19); 'rollout' = a
                                     -- member run of an environment rollout (§21, trigger_user_id
                                     -- = the rollout's creator)
confirmed_by_user_id nullable,       -- who approved the apply (core of the audit)
parent_run_id nullable, run_group_id nullable, worker_id nullable,
rollout_id nullable,                 -- §21: the environment rollout this run belongs to
plan_summary jsonb,                  -- {add, change, destroy, import, forget, …}: tofu's
                                     --   `change_summary` counters (§7.3) — `remove` reported as
                                     --   `destroy`; `import`; `forget` (OpenTofu's counter, or
                                     --   counted from the plan JSON: Terraform has none); any other
                                     --   counter a newer tool reports (Terraform's
                                     --   `action_invocation`, `action_fail`) kept under its name
check_results jsonb,                 -- hook check results, plan and apply jobs (§8.3):
                                     --   {results: every check (ok|warn|fail), each with its
                                     --    `stage`; checks: the warn/fail ones only (the field's
                                     --    meaning before `results` existed)}
resolved_inputs jsonb,               -- injected upstream outputs (non-sensitive)
used_mocks bool default false,       -- §9.3: at least one mock consumed
variable_provenance jsonb,           -- §3.4: provenance of each resolved variable
plan_artifact_key text nullable,     -- S3 key of the encrypted saved plan (§7.3/§11.3) the apply job
                                     -- applies; set by the plan job's upload, cleared by transition()
                                     -- on any terminal state (migration 0024)
claimed_at, confirmed_at, finished_at,
cancel_requested_at timestamptz nullable,  -- §4.1: a user asked the worker to stop the executing run
                                     --   (migration 0032); the state itself only changes on `job_canceled`
logs_archived_at timestamptz nullable,     -- §5.2: the run's logs are in S3 (migration 0031)
error nullable, created_at
```

The drift (§19), VCS (§18) and external-secret (§15.5) columns are listed in their own sections.

Concurrency — 1 active run per environment:

```sql
CREATE UNIQUE INDEX one_active_run_per_env ON runs (environment_id)
WHERE state IN ('preparing','planning','checking','unconfirmed','confirmed','applying','running');
```

Run-group members (§9.4) are looked up by group — on every cascade, group status and run page:

```sql
CREATE INDEX ix_runs_run_group_id ON runs (run_group_id) WHERE run_group_id IS NOT NULL;
```

Terminal runs whose logs still wait for the cold tier (§5.2) are found by:

```sql
CREATE INDEX ix_runs_logs_to_archive ON runs (finished_at)
WHERE finished_at IS NOT NULL AND logs_archived_at IS NULL;
```

### 3.6 `run_events`

```
id, run_id FK, from_state, to_state,
actor enum(system|user|worker), actor_id nullable, payload jsonb, created_at
```

### 3.7 `workers` and `worker_pools`

```
worker_pools: id, space_id, name unique, labels jsonb, token_hash, created_at
workers: id, pool_id, name, status(idle|busy|offline), labels jsonb,
         version, runner text nullable (local|docker, §7.4), tools jsonb nullable (§7.4),
         last_heartbeat_at, registered_at
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
                s3_key, created_by_run_id nullable, deleted_at nullable (soft delete: reads skip such
                rows; no endpoint sets it yet),
                created_at
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
  private_key_encrypted bytea null,  -- local key: AES-256-GCM (§1)
  kms_key_arn text null,             -- KMS key: only its ARN; the private key never leaves KMS
  status enum('active','retiring','retired'),  -- only 1 'active' at a time (partial unique index)
  created_at, retiring_at nullable, retired_at nullable
  -- CHECK num_nonnulls(private_key_encrypted, kms_key_arn) = 1
)
```

**Signer.** `STACKD_OIDC_SIGNER=local` (default) keeps an RSA-2048 key AES-GCM encrypted in the row and signs in process; `STACKD_OIDC_SIGNER=kms` + `STACKD_OIDC_KMS_KEY_ARN` signs every workload token through `kms:Sign` (`RSASSA_PKCS1_V1_5_SHA_256` = RS256; the API builds the JWS itself), the private key never entering the API — required in production unless `STACKD_ALLOW_LOCAL_SIGNER=true` acknowledges a local key (the API refuses to start otherwise; see risk §13 / PLAN §5). A KMS key is registered from `kms:GetPublicKey`: it must be `SIGN_VERIFY`, RSA ≥ 2048, support RS256 (else 422); its `kid` is derived from its public key like a local key's. The API needs `kms:Sign` + `kms:GetPublicKey` on it, nothing else.

**Rotation.** A new `active` key, the old one moves to `retiring` (`retiring_at` set; still in the JWKS, no longer signs), then `retired` (out of the JWKS) once the longest token it can have signed has expired: max(plan cap 30 min, apply budget + grace) + 5 min clock skew — a scheduler task (§7.5, advisory-locked) does it. Triggers: `POST /api/v1/oidc/keys/rotate` (admin; `{"kms_key_arn"}` for a KMS key, empty for a new local key — refused 422 on a KMS issuer), and, automatically, an issuer configured for KMS whose active key is still local (or absent) rotates to the configured key on its next signature — switching the setting is the whole migration. `GET /api/v1/oidc/keys` (admin) lists them (public metadata only). Rotating to a key the issuer
already knows (switching back to a KMS key used before — `retiring` or `retired`) reactivates it —
same kid — rather than refusing, signing from then on through the ARN just configured or requested
(the same key material can sit behind another ARN, e.g. a multi-region replica); only a key that
already signs is refused (409). Every rotation is serialized by a transaction-level advisory lock.
The key is provisioned on first use in a database session of its own: key management never commits
or rolls back the caller's transaction (a job claim). Provisioning is idempotent under concurrency:
under the lock it re-checks the active key, and a concurrent provisioning that won anyway (unique
violation, or "already active" from a replica that does not take the lock) is used as is — concurrent
claims and `GET /oidc/jwks` never fail on it.

**Signing failures at claim.** A token that cannot be signed is classified:
- **transient** — KMS throttling, KMS or the network unreachable or timing out, a KMS 5xx, the
  API's own credentials mid-refresh, no database connection for the provisioning session (pool
  exhausted — it needs a second connection while the claim holds one — or a connection lost): the
  claim is rolled back, the run keeps its state (`queued`, or `confirmed` — an approved apply keeps
  its approval), and the worker gets **503** `code=<marker>`, `retryable: true`; it polls again;
- **permanent** — access denied, key disabled / pending deletion / not found, an unusable key (not
  RSA ≥ 2048 `SIGN_VERIFY` RS256), an invalid ARN, no AWS credentials, anything unrecognized: the
  job's run fails closed at claim, `run.apply_failed` with `reason: oidc`, the worker getting nothing
  (204).

`run.error` and the audit `detail` carry a generic marker only — `oidc_signing:<code>`, e.g.
`kms_access_denied`, `kms_key_disabled`, `kms_key_not_found`, `kms_key_unusable`, `kms_invalid_arn`,
`kms_no_credentials`, `kms_rejected`, `signing_failed` (transient: `kms_throttled`, `kms_unavailable`,
`kms_unreachable`, `db_unavailable`) — never the provider's message (role/key ARNs, account id),
which is logged server-side (`oidc.signing_failed`, admin log view). The payload build runs in a
savepoint: a failed claim discards what it staged on the run (`used_mocks`, inputs, provenance,
`used_secret_fallback`) and its audit events (`dependency.mock_consumed`, `secret.fallback_used`) —
they only stand for a job handed out. Audited: `oidc_key.rotated` (actor the admin, or `system`;
context kid, backend, ARN, previous kid, `reactivated`, and for a reactivation `previous_status` and
`replaced_kms_key_arn` when the ARN changed), `oidc_key.retired` (`system`).

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

Terminal: `finished`, `failed`, `discarded`, `canceled`. `canceled`: user on `queued`/`confirmed`/`unconfirmed` (not claimed yet, or waiting on a human), the system when a PR closes (its in-flight proposed runs, §18.3), or a rollout's cancel on its member runs that are `queued`, `unconfirmed` or `confirmed` and not claimed yet (§21.4 step 8 — `confirmed → canceled` is that edge). **Cancelling a run a worker is executing** is a request, not a transition: `POST /runs/{id}/cancel` records `runs.cancel_requested_at`, audits `run.cancel_requested` and queues a `cancel_job` command for the worker's next heartbeat (§7.1) — one transaction. The worker interrupts its tool (SIGINT, a graceful stop: tofu finishes the operation in flight, releases the state lock, exits; SIGKILL only after `STACKD_CANCEL_GRACE_SECONDS`, default 120) and reports `job_canceled`; only then does `transition()` end the run. A job that holds **no apply credentials** (preparing, planning, checking, a read-only command) ends `canceled`; one that does (`applying`, a mutating command, §4.3) ends **`failed` with `error=canceled_during_apply`** — `applying` is never `canceled`, since the infrastructure may be partly changed — and nothing cascades. Stopping a job that holds apply credentials needs `can_apply` (the gate of starting it); any other needs `writer`. Idempotent: asking again re-queues the command (the first may have been lost) without a second audit. If the plan finishes before the stop reaches it, the run ends `canceled` instead of going to `unconfirmed`/`confirmed` — a stopped plan is never auto-confirmed. Once an apply has succeeded the worker ignores a later stop (what is left — recording the outputs — must finish). A worker that is offline never reports: `worker_lost` (§3.7) ends the run. Any terminal state drops the run's saved plan (§11.3).

Command runs (§4.3) take a shorter path: `queued → preparing → running → finished | failed` (or, on a stop request, `canceled` — a read-only command — or `failed` — a mutating one, as above).

### 4.2 Transition rules

| Transition | Actor | Conditions |
|---|---|---|
| `queued → preparing` | worker (claim) | no active run on the env, env not locked, compatible labels |
| `planning → checking` | worker | plan OK **and ≥ 1 after_plan hook**. Without an after_plan hook, `checking` is skipped: the transitions `planning → unconfirmed / confirmed / finished` exist, with exactly the same conditions as their equivalents from `checking` |
| `checking → unconfirmed` | worker | checks OK or warn; non-empty diff. A warn **forces** confirmation even if autodeploy |
| `checking → confirmed` | system | all checks OK, non-empty diff, `autodeploy=true`, env not protected, `used_mocks=false`, the run belongs to no rollout — **or** the run belongs to a started rollout whose gate is open and passes its gate (§21.5) |
| `planning/checking → finished` | worker | empty diff, or a `proposed` run whatever its diff (plan-only). No outputs are captured here: only `applying → finished` records them (§9.1) |
| `unconfirmed → discarded` | user (`writer`) | `POST /runs/{id}/discard` — the plan is dropped, never applied. `queued → discarded` is also a legal edge of `transition()` and the endpoint does not restrict the state |
| `queued / confirmed / unconfirmed → canceled` | user, or system | a user's cancel (a `confirmed` run is not claimed yet); the system when a PR closes (its proposed runs, §18.3) or a rollout is canceled (its member runs not claimed yet, §21.4 step 8) |
| `preparing / planning / checking / running → canceled` | worker | `job_canceled` after a user's stop request (§4.1), the job holding no apply credentials |
| `applying → failed` (`canceled_during_apply`) | worker | `job_canceled` after a stop request on a job holding apply credentials; a run already `applying` is never `canceled` |
| `unconfirmed → confirmed` | user (or system: a rollout re-approval, §21.6) | `can_apply(user, env)` = role∈{approver,admin} AND `env.tier ∈ allowed_tiers` (§2.4); ≠ triggerer if the tier requires four-eyes, the env is `protected` or sets `require_second_pair_of_eyes`; for a `destroy` run: `can_destroy` required; **blocked if `used_mocks` and `allow_mock_apply=false`** (likewise `used_secret_fallback` and `allow_fallback_apply=false`, §15.5) |
| `confirmed → applying` | worker (claim) | the plan's worker within the affinity window, then any compatible worker (§7.2); re-checks `allow_mock_apply` and the secret-fallback gate against the apply-time resolution (§9.3/§15.5). The apply job clones the pinned commit and applies the run's **saved plan** (§7.3); a missing or unreadable plan fails the run — it never re-plans |
| `applying → finished` | worker | apply exit 0 + outputs uploaded + no failing `after_apply` check (one that fails → `failed`, the outputs still recorded, nothing cascades, §8.3) |
| `* → failed` | worker/system | a tofu command failing (a plan fails on any exit but 0 or 2, an apply or a command on any non-zero exit), the plan JSON unavailable (§8.3), a hook `fail` at any stage (§8.3), timeouts — the worker hard-kills the process group past its budget: the apply (and a mutating command run, §4.3) past `stackd_apply_timeout_seconds` (default 15 min, sent as `apply_timeout_seconds`), a hook past the hook timeout (10 min, `STACKD_HOOK_TIMEOUT_SECONDS`, then it fails by its `on_failure`), any other subprocess past the agent's 2 h cap (separate prepare/plan budgets are not enforced yet); `worker_lost` (§3.7); `commit_mismatch` (§7.3); an unresolvable dependency, secret or workload token at claim — plan or apply claim, hence also `confirmed → failed` (§9.3, §15.2, §3.11) |

Single function `transition(run, to_state, actor, payload)` (`app/runs/transition.py`): legality (its `ALLOWED` edge table), atomic update guarded on `from_state` — the row re-read `FOR UPDATE` where `state = from_state` (409 `Concurrent modification` if it moved), a rollout member's rollout locked first (§21.3) — `run_event`, audit event when the caller passes one (human or terminal actions), the notification / VCS / in-app outboxes and the rollout's progress (§17, §18, §21), WS publication (`NOTIFY`, §5.3). A terminal state also stamps `finished_at` and drops the saved plan (§11.3). Everything in the caller's transaction: `transition()` never commits. The cascade (§9.2) is run by the caller, after the transition to `finished`.

### 4.3 Command runs

A `RunType.command` run executes **one allowlisted tofu/terraform subcommand** (`import`, `state list/show/rm/mv`, `taint`, `untaint`, `output`, `show`, `validate`, `providers`, `refresh`) instead of the plan→apply flow — for state surgery and adoption that `plan`/`apply` can't express. It is **not** arbitrary shell: the worker runs `<tool> <command> <args>` with the command taken verbatim from the allowlist; an arg starting with `-` (`-chdir`, `-state`, …) is refused (400). Lifecycle: `queued → preparing → running → finished | failed`. The subcommand + args live in `runs.command` (`{name, args}`). Endpoint: `POST /api/v1/environments/{env_id}/commands`. Permissions: read-only commands need `writer`; **mutating** commands (import, state rm/mv, taint, untaint, refresh) require `can_apply(user, env)` — the same gate as an apply. The worker receives the **apply** OIDC role for mutating commands and the (read-only) **plan** role otherwise. A mutating command, like an applying run, **holds apply credentials**: its state token TTL and OIDC cap are the apply window (`apply_reclaim_window_seconds`, §3.7), the worker hard-kills its subcommand past the apply budget (`apply_timeout_seconds` of the claim payload), and worker-lost detection only reclaims it after the apply window (§7.5). The trigger is audited (`run.command_triggered`) and so is completion (`run.command_executed`). `force-unlock` is **not** a command — it has its own endpoint (`DELETE …/state/lock`, §11.2).

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

> **Status**: done. A periodic task (§7.5) archives each run's logs once it has been terminal for
> 60 s (the agent's last buffered chunks, §5.1; 20 runs per tick) — one gzip'd JSONL
> object per phase, one `{seq, section, lines}` chunk per line — and stamps `runs.logs_archived_at`;
> the hot rows are purged `log_hot_retention_days` (7, env `LOG_HOT_RETENTION_DAYS`) after that, by
> the daily retention task. `GET …/logs` serves the hot rows while the run has any, and falls back
> to the archive once they are gone (same chunks, same order, `phase`/`after_seq` honoured; a
> `phase` outside `[A-Za-z0-9_.-]{1,64}` — it ends up in an S3 key — reads nothing from the archive,
> and is refused with 400 by the download). `GET …/logs/download?phase=all|<phase>` returns plain text (ANSI kept, phases in start
> order, hooks prefixed `[hook:x]`), from either tier. If S3 is down the run stays unarchived and its
> hot rows untouched — retried next tick. The 1-year S3 expiry is a bucket lifecycle rule (ops), not
> application code. A chunk ingested after its run was archived is not re-archived.

### 5.3 Live distribution

Single multiplexed WebSocket `/api/v1/ws` (the access token goes in the first message, `{"type": "auth", "token": …}`, not in the URL): `{"sub": "run:<id>"}` → light signals `{"kind": "log", run_id, phase, max_seq}` and `{"kind": "run_event", run_id, to_state}` — never the content. On a `log` signal the front fetches the new chunks with REST `GET …/logs?phase=&after_seq=`; on a `run_event` it invalidates its queries. Reconnection → the same REST GET `after_seq` fills the gap, then the stream.

**Multi-replica fan-out (without a broker).** A WS client is connected to **one** API replica; the event (transition or log chunk) may be produced on **another** replica. The bridge is **Postgres `LISTEN/NOTIFY`**:

- `transition()` (§4.2) and log ingestion (§5.1) emit a `NOTIFY` on a per-entity channel (`run_<id>`, `env_<id>`) **in the same transaction** as the write.
- The `NOTIFY` payload carries only a **lightweight signal** — `{kind, run_id, phase, max_seq}` for a log chunk, `{kind, run_id, to_state}` for a transition — never the content (8 KB `NOTIFY` cap, and log lines can be large).
- On receipt, each replica forwards the signal as is to its locally subscribed WS clients, which re-read the source over REST (`run_logs` after `after_seq`, or the run). The front sees the same sequence as on REST reconnection — a single read path, idempotent by `seq`.

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

**Real immutability, not just "no API"**: the absence of an update/delete endpoint does not protect against a bug or an application compromise. The application DB role only has `INSERT` and `SELECT` on `audit_events` (`REVOKE UPDATE, DELETE`), a `BEFORE UPDATE OR DELETE` trigger raises an exception, and retention purging goes through a distinct role, separate from the application role and itself audited. 2-year retention (`audit_retention_days` = 730, env `AUDIT_RETENTION_DAYS`): a daily task (§7.5) purges older events on a connection of the `stackd_audit_purge` role (created `NOLOGIN` by migration 0031 — an operator gives it a login and sets `STACKD_AUDIT_PURGE_DATABASE_URL`; without that URL no purge runs). The role holds `SELECT, INSERT, DELETE` only, and the append-only trigger lets exactly that role delete; when it deleted anything, it writes an `audit.purged` event (actor `system`, target_kind `audit`, context `deleted`, `before`, `retention_days`) in the same transaction as the delete. Indexes: `(created_at)`, `(actor_id, created_at)`, `(target_kind, target_id, created_at)`, `(action, created_at)`.

### 6.2 Taxonomy

The exhaustive list of actions the API emits (to check it: `grep -rn 'action="' api/app`, plus the
`audit_action=` of each `transition()` call and the per-field map in `users/router.py`):

```
auth.login / auth.logout / auth.domain_denied / auth.refresh_reuse_detected
space.created / space.member_set / space.member_removed            # Phase F memberships (§2.4)
stack.created / stack.updated / stack.deleted
environment.created / environment.updated / environment.deleted   # created: also by a clone
                                  #   (context cloned_from, autodeploy, autodeploy_off_reason, §21.2)
audit.purged                      # retention purge of old audit events (§6.1) — actor system
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
                                  # run.triggered by the system: context via = cascade (§9.2) |
                                  #   rollout (§21.4: rollout_id, environment_id, commit)
run.confirmed ⭐                  # a human confirm, or the system: on autodeploy (context
                                  #   autodeploy), or under a rollout's approval (context via =
                                  #   rollout, rollout_id, approved_by, approved_by_email, §21.5)
run.cancel_requested              # a user asked the worker to stop an executing run (§4.1) — once per run
run.discarded / run.canceled      # canceled: also the worker, on `job_canceled` (context reason cancel_requested); a user, or the system when a PR closes (reason pr_closed);
                                  #   a rollout's cancel: the canceller, context via = rollout (§21.8)
run.plan_finished                 # plan-only terminal: empty diff, or a proposed/drift run
run.applied ⭐ / run.command_executed
run.apply_failed                  # any failed run, whatever its phase, unless a check failed it —
                                  #   context phase, or reason: cancel_requested (a stopped apply, §4.1) |
                                  #   worker_lost | commit_mismatch | dependency | secret | oidc
                                  #   (§7.2/§7.3, §3.11)
run.check_failed                  # a hook check with on_failure=fail failed the run, at any stage
                                  #   (§8.3) — context phase = the failing stage
run.check_warned                  # a job (plan or apply) ended — finished, failed or stopped — with warn
                                  #   checks; context: checks (their names), stages (where they ran)
state.force_unlocked / state.import_session_created   # §11.2, §11.4
dependency.created / dependency.deleted / dependency.mock_consumed   # created: also via link-by-name,
                                  #   or a clone (via = clone, upstream_kind, §21.2)
cloud_integration.created / cloud_integration.updated / cloud_integration.deleted
oidc_key.rotated / oidc_key.retired            # issuer signing key (§3.11)
worker_pool.created / worker_pool.deleted
worker.rejected                                # a non-docker runner refused in production (§7.4)
worker.diagnostics_requested                   # read-only debug bundle (cf. §observability)
user.role_changed / user.apply_tier_changed / user.destroy_permission_changed / user.disabled
tier.created / tier.updated / tier.deleted     # configurable tier catalog (§2.4)
secret_source.created / secret_source.updated / secret_source.token_rotated / secret_source.deleted
secret.fallback_used / secret.fallback_overridden                    # §15
environment.cloned                                                   # §21.2
rollout.created / rollout.approved / rollout.paused / rollout.resumed    # §21.8
rollout.retried / rollout.canceled / rollout.succeeded / rollout.failed  # §21.8
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

CSV columns: `created_at, actor_email, action, target_kind, target_id, context` — `context` is the
event's context as JSON (what the list endpoint returns to the same admins): the "who" of a system
action lives there, e.g. the approver of a rollout's auto-confirmation (§21.5).

Filtering by stack or environment goes through `target_kind`/`target_id`; there is no `stack=` /
`environment=` filter.

/audit page (filterable global journal), Activity tab per env (recent applies: who/when/commit/summary/logs), per-user view.

---

## 7. Worker protocol

### 7.1 Registration and heartbeat

```
POST /worker/v1/register   (Bearer pool_token) { "name", "labels"?, "version"?, "runner": "local"|"docker", "tools"? } → worker_id + worker_token
POST /worker/v1/heartbeat  (20 s) { "in_flight": N, "capacity": M } → { "commands": [{"id": ..., "type": "diagnostics"|"cancel_job", "payload": ...}] }
POST /worker/v1/commands/{id}/result   { "status": "done"|"failed", "result": {...} }
```

Heartbeat = downstream command channel. No incoming connection. It carries `diagnostics` and `cancel_job` (payload `{job_id}`, cancelling an executing run, §4.1): the agent keeps a registry of its running jobs, flips the job's cancel token — the job's own thread interrupts its subprocess with SIGINT (the `local` runner signals the process group; the `docker` runner signals the container, whose init forwards it), then SIGKILL if it is still running after `STACKD_CANCEL_GRACE_SECONDS` (default 120) — and answers the command with `{job_id, running_here}`. A cancel that beats the job's registration is remembered and applied when it registers. Once an apply has exited 0 the token is disarmed: a later stop no longer interrupts the job (outputs, `after_apply` hooks). The worker reports the end with the job event `job_canceled` (§7.3), refused with 409 unless a cancel was requested. **In-flight count (Phase E)**: the worker reports the number of jobs it is currently executing; the API sets `status = busy` when it is > 0, `idle` when 0 (the worker is authoritative, which also flips a finished worker back to idle). `capacity` (the worker's `STACKD_MAX_CONCURRENT_JOBS`) is advertised for the scheduler to reason about; not persisted this phase (no schema change). A heartbeat with no body is just a liveness ping.

**Worker concurrency (Phase E).** `STACKD_MAX_CONCURRENT_JOBS` (default `1` = today's serial behaviour) bounds the in-flight jobs a single worker runs at once, each on its own thread (the heartbeat thread is already independent). The claim stays one-run-at-a-time on the wire; the API's `SELECT … FOR UPDATE SKIP LOCKED` (§7.2) and the one-active-run-per-env partial unique index (§3.5) keep concurrent claims safe — **concurrency is across different environments only**. Same-env races are still guarded by the state-backend lock (§11.2).

### 7.2 Claim (long-poll)

```
POST /worker/v1/jobs/claim?wait=25
→ 204 if nothing (or the claimed run just failed closed at claim: §9.3, §10, §15.2)
→ 503 problem+json, retryable: true — a transient failure to build the job (workload token not
       signable right now, §3.11): the claim is undone, the run keeps its state; poll again
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
                                           # never given to repo hooks (§8.3); always empty for a
                                           # proposed run, which gets no secret at all (§13)
    "tfvars_json": { ... },                # terraform variables (JSON), with the dependency
                                           # inputs / mocks (§9.3) merged in — none sensitive for
                                           # a proposed run (§13)
    "hcl_tfvars": { "tags": "{ team = \"core\" }" },  # hcl-typed variables, written verbatim (§3.4)
    "mask_values": [ "..." ],              # literal secrets the agent masks in ALL logs (§5.1):
                                           # sensitive values, state/OIDC/repo tokens
    "hooks": {                             # §8: PLATFORM hooks by stage — the agent appends the
                                           # repo's .stackd.yml hooks after them
      "after_plan": [ {"name": "infracost", "command": "...", "on_failure": "warn",
                       "source": "platform"} ]
    },
    "repo_hooks_at_apply": false,          # the env tier's flag (§8.3): may repo hooks run at
                                           # before_apply/after_apply? absent → false on the agent
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
    AND NOT e.locked
    AND e.labels <@ :worker_labels
    AND ...                         -- the worker has the stack's tool version (§7.4)
    AND (r.state = 'queued' OR ...) -- apply affinity, below
    AND NOT EXISTS (...)            -- no other active run on the env
  ORDER BY (r.state = 'confirmed' AND r.worker_id = :wid) DESC,   -- affinity: the apply
           r.is_drift,                                             -- prefers the worker of
           r.created_at                                            -- the plan; drift last (§19)
  FOR UPDATE OF e SKIP LOCKED LIMIT 1
)
-- then transition(run, preparing | applying) with worker_id=:wid, claimed_at=now() (§4.2)
```

**The real concurrency guard is the unique index `one_active_run_per_env` (§3.5), not `SKIP LOCKED`.** Two workers claiming two **distinct** `queued` runs of the same env select different rows: `SKIP LOCKED` does not serialize them against each other, and the `NOT EXISTS (active run)` is true for both (none active yet). Both `UPDATE → preparing` go ahead; the second violates the partial unique index. Two protections, **both** to be applied:

1. **Serialize per env**: the `SELECT ... FOR UPDATE` also locks the corresponding `environments` row (`JOIN environments e ... FOR UPDATE OF e SKIP LOCKED`), so that only one claim per env progresses at a time; the others skip the locked env and take another run.
2. **Safety net**: the violation of `one_active_run_per_env` is **caught** (SQLSTATE `23505`) and treated as "nothing to claim" → `204`, the worker re-polls. This is the correctness guarantee; the lock above is only an optimization to avoid discarded work. A lock not available (`55P03`, e.g. a rollout member whose rollout row is busy — taken `NOWAIT`, §21.3), a deadlock (`40P01`) or a serialization failure (`40001`) gives way the same way.

Apply affinity: a `confirmed` run is reserved for its originating worker for **60 s** (`stackd_apply_affinity_seconds`; `AND (r.worker_id = :wid OR r.confirmed_at < now() - interval '60 seconds')` on confirmed runs). Past that delay (dead or saturated worker), any compatible worker takes it: the apply needs nothing from the plan worker's workspace — it downloads the run's **saved plan** from the API (§7.3) and applies exactly it. There is **no re-plan** on the apply path: no saved plan → the apply fails.

### 7.3 Events and artifacts

```
POST /worker/v1/jobs/{id}/events
  { "event": "phase_started"|"phase_finished"|"job_failed"|"job_canceled",
    "phase": "...", "exit_code": 0,
    "result": { "has_changes": true,
                "summary": {"add": 2, "change": 0, "destroy": 0, "import": 0, "forget": 0, …},
                "checks": [{"name":"infracost","status":"warn","detail":"...","stage":"after_plan"}] } }
  phase_started(planning) carries "result": {"commit_sha": "<full SHA checked out>"}
  summary (§3.5): every counter of tofu's `change_summary` event — `remove` as `destroy`,
      `import`, `forget`, and any other one under its own name (Terraform's `action_invocation`,
      `action_fail`) — plus `forget` counted from the plan JSON when the tool reports none
      (Terraform); each of add/change/destroy/import/forget always present (0 by default). A
      rollout's gate fails closed on a missing one (§21.5)
  job_failed carries "result": {"error": "<masked reason>", "checks": [...]} — the checks of the
      stages that ran (§8.3); "phase" = where it failed (a stage, init, plan, apply, command);
      no "error" for a failing check (the run's error is then the phase); a command job, which
      runs no hook, sends only "error"
  phase_finished of a command job carries "result": {"command": "<name>"}
  job_failed(after_apply) carries "result": {"checks": [...], "outputs": {...}}   # §8.3: the
      # apply went through, an after_apply hook failed → outputs recorded (§9.1), drift
      # cleared (§19), no cascade
  job_canceled carries "result": {"checks": [...]} — the job stopped on a user's request
      (§4.1); 409 unless `runs.cancel_requested_at` is set

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
version = pick(worker_tools, job.environment.tool, job.environment.tool_version)
                                       # before anything runs, clone included: the stack's version
                                       # (exact, or the highest release of a MAJOR.MINOR series)
                                       # must be one this worker has — else job_failed
                                       # "tool_version_unavailable: …" (see "Tool versions" below)
ws = Workspace(job.job_id)
# depth 1; a pinned SHA/ref is fetched exactly (FETCH_HEAD, verified), else the head of `branch`.
# An apply job without a pinned commit is refused before cloning (§7.3).
ws.git_clone(job.environment.repo_url, job.environment.commit_sha, branch=job.environment.branch,
             token=job.repo_credentials.token)  # token in the clone URL, then `origin` reset to the
                                       # token-free URL; a pinned commit is fetched from the authed URL
                                       # itself — no token left under .git/ (§8.3)
# Managed state (§11): the backend settings and state token reach tofu ONLY, as TF_HTTP_* (tofu_env
# below); the repo gets a bare `backend "http" {}` block. Unmanaged state: `init` gets the env's
# -backend-config file/pairs instead (§11.3).

cloud_env = {} if not job.cloud_credentials else {       # §10 OIDC workload
    "AWS_WEB_IDENTITY_TOKEN_FILE": ws.secret_path("oidc_token"),   # outside the checkout
    "AWS_ROLE_ARN": job.cloud_credentials.role_arn,
    "AWS_ROLE_SESSION_NAME": f"stackd-{job.job_id}",
    **({"AWS_REGION": job.cloud_credentials.region} if job.cloud_credentials.region else {}) }
run_env = {**job.env, **job.sensitive_env, **cloud_env}
platform_env = {**worker_env_minus("STACKD_*"), **run_env}   # platform hooks (trusted, §8.3)
tofu_env = {**platform_env, **tf_http(job.backend)}           # runner contract
repo_env = {**allowlisted(worker_env), **job.env}        # repo hooks (§8.3)

# What a tofu command reads from disk that no repo hook may find (§8.3) exists only while it runs:
# the OIDC token file, and — for a command that reads variables — the run's tfvars (`tfvars_json`,
# dependency inputs/mocks merged in, §9.3, and `hcl_tfvars`, §3.4). Written fresh (never through a
# symlink), deleted right after. The token file is written around each platform hook the same way.
with_files = lambda tfvars=True: tofu_files(ws, job, tfvars)

hooks = merge_hooks(job.hooks, ws.load_stackd_yml())   # platform first, §8
# stage(name): run its hooks, each under the hook timeout (§8.3); every result (ok/warn/fail) goes
# into `checks` with "stage": name. A failing `fail` hook → report job_failed(phase=name, checks) and
# stop, before the next tofu step. Every job_failed below carries the `checks` gathered so far.

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
        code, events = run_json(tf, "plan", "-input=false", "-json", "-detailed-exitcode",
                                f"-out={plan_file}", *(["-destroy"] if job.destroy else []),
                                *(["-refresh-only"] if job.refresh_only else []))   # drift, §19
    # 0 = no changes, 2 = changes; anything else (1 = error, 124 = killed past its time budget, a
    # signal) → job_failed with the reason — never "no changes". apply/commands: any non-zero fails.
    with with_files(tfvars=False):
        run(tf, "show", "-no-color", plan_file)              # the human-readable diff, to the log
        plan_json = run(tf, "show", "-json", plan_file)  # failed, or no JSON object → job_failed
                                                             # ("plan JSON unavailable"), never `{}`
    masked_json = mask_plan_json(plan_json)                  # §8.3
    upload_artifact("plan.json", masked_json)
    saved_plan = read(plan_file) if code == 2 and job.save_plan else None
    delete(plan_file)                                        # before any hook runs
    if saved_plan:
        upload_artifact("plan.tfplan", saved_plan)           # the saved plan (§7.3)
    if hooks.get("after_plan"):
        report(phase_started, phase="checking")
    stage("after_plan")   # checking phase: platform hooks read plan.json = plan_json, then the repo
                          # hooks plan.json = masked_json (§8.3)
    report(phase_finished, has_changes=(code == 2), summary=..., checks=checks)

elif job.phase == "apply":                   # a fresh clone of the pinned commit (§7.3)
    write_backend_override(ws)
    hooks = skip_repo_apply_hooks(hooks) if not job.repo_hooks_at_apply else hooks   # warn, §8.3
    with with_files(): run(tf, "init", "-input=false")
    plan = download_artifact("plan.tfplan")  # missing/unreadable → fail closed, NEVER re-plan
    stage("before_apply")                    # a failing check → nothing applied
    write_plan(plan)                         # 0600; removed right after the apply
    with with_files():                       # the tfvars too: tofu refuses changed inputs (§7.3)
        run(tf, "apply", "-input=false", "-json", "plan.tfplan",   # stale → tofu refuses → failed
            timeout=job.apply_timeout_seconds)
    cancel_token.disarm()                    # applied: a stop request no longer interrupts (§4.1)
    with with_files(tfvars=False): outputs = run(tf, "output", "-json")
    upload_artifacts(outputs, "outputs.json")
    stage("after_apply")                     # a failing check → job_failed(after_apply) carrying
                                             # `outputs`: recorded, drift cleared, no
                                             # cascade (§8.3)
    report(phase_finished, outputs=outputs, checks=checks)

else:                                        # command job (§4.3): no hooks, no plan/apply phases
    write_backend_override(ws)
    report(phase_started, phase="running")
    with with_files(): run(tf, "init", "-input=false")
    with with_files():
        run(tf, *job.command.name.split(), *job.command.args,
            timeout=default_cap if job.cred_phase == "plan" else job.apply_timeout_seconds)
    report(phase_finished, command=job.command.name)

ws.cleanup()                                 # in a `finally`, whatever ended the job
# A stop request (§7.1) interrupts the running subprocess (or the next one, before it starts):
# the job ends with report(job_canceled, checks=checks) instead of job_failed.
```

Every tofu subcommand gets `tofu_env` — those that reach the state (`init`, `plan`, `apply`,
`output`, a command) need it, and the two `show` of the local plan file run with it too, so none
of them sees the agent's own settings. Hooks get the platform env (platform
hooks) or the stripped repo env (§8.3), never the `TF_HTTP_*` settings. Subprocesses without an
explicit budget are capped at 2 h by the agent's watchdog.

**Tool versions.** A stack's `tool_version` is an exact version (`1.12.0`, `1.12.0-rc1`) or a minor series (`1.12`: the highest *release* of that series a worker has — never a pre-release); anything else (`latest`, `~> 1.12`) is refused at stack create/update (422). The agent declares the versions it can run in `POST /worker/v1/register` (`tools: {"opentofu": ["1.12.2"], "terraform": [...]}`, kept on `workers.tools`): on the `local` runner the `tofu`/`terraform` on its PATH (`<bin> version -json`), on the `docker` runner the keys of `STACKD_RUNNER_IMAGES`. The claim (§7.2) hands a worker only a run whose stack's version it has, and the queue reports a queued run no online worker can take (labels and tool version) as `no_compatible_worker` (§12). The agent re-checks before the job runs anything — clone included — and fails it with `tool_version_unavailable: <tool> <version> is not on this worker (it has: …)` otherwise. An agent that declares no tools (predating the field) is not filtered by the claim, only by its own check; a production API refuses it anyway (it declares no runner either).

**Runner images (supply-chain).** `deploy/images/` builds one image per tool version: the release's checksum file is verified by its signature (OpenTofu: keyless Sigstore, certificate identity = OpenTofu's release workflow for that minor series; Terraform: HashiCorp's GPG key, pinned by fingerprint) and the zip by that checksum file, then the image adds git, ssh, ca-certificates and `jq` (pinned, checksum-verified); non-root (`uid 10001`), no entrypoint. Check tools beyond `jq` (checkov, trivy, infracost) are the operator's to layer on top. `STACKD_RUNNER_IMAGES` maps `"<tool>:<version>"` to an image **pinned by digest** (`<repo>@sha256:<digest>`); the agent refuses to start on a tag, which can be re-pointed.

**`docker` runner.** Each command of a job — every tofu subcommand (`init`, `plan`, `show`, `apply`, `output`, a command run's) **and every hook** — runs in its **own fresh container** of the image `STACKD_RUNNER_IMAGES` names for the job's tool version (pulled by digest when the daemon lacks it), created, started and removed through the Docker Engine API on the local unix socket (`STACKD_DOCKER_HOST`, default `/var/run/docker.sock`; no docker CLI, no SDK). One container *per command* rather than per job: when the command exits, the container is force-removed with everything still running in it, so nothing a command starts — a `setsid` daemon included — reaches a later step (the tfvars and token file tofu reads, the raw plan JSON a platform hook reads). The git clone stays in the agent: the repo token never enters a container (§8.3).

- **Mounts.** tofu and platform hooks: the job's workspace (checkout, OIDC token file while it exists, the plan job's saved plan), at the same absolute path as on the agent's side, so every path the agent hands a command is valid as is. **Repo hooks: the checkout only** — the token file and the saved plan are not even in their filesystem. A containerised agent must therefore see `STACKD_WORKSPACE_ROOT` at the same path as the Docker host does (bind-mount it at an identical path).
- **Hardening, every container.** `CapDrop: ALL`, `no-new-privileges`, not privileged, read-only root filesystem with a `/tmp` tmpfs (`HOME=/tmp`, size `STACKD_DOCKER_TMP_SIZE`, default `1g`), an init process, `PidsLimit` (`STACKD_DOCKER_PIDS_LIMIT`, 1024), optional `Memory` (`STACKD_DOCKER_MEMORY`, no swap beyond it) and `NanoCpus` (`STACKD_DOCKER_CPUS`), optional runtime (`STACKD_DOCKER_RUNTIME`, e.g. `runsc`), network `STACKD_DOCKER_NETWORK` (default `bridge`; the place for an egress allowlist, and it must reach the API's internal URL for the state backend). Never the Docker socket. User: the agent's own uid:gid; a **root** agent hands the workspace to the images' unprivileged `uid 10001` (`chown` that never follows a symlink) and runs the container as it.
- **Environment.** Only what the agent builds for the command: the run's env (and, for tofu and platform hooks, its sensitive env, the OIDC env and, for tofu, `TF_HTTP_*`), plus — of the worker's own environment — the proxy settings and the locale, nothing else (no `PATH`, no host `HOME`, no host credential). It travels in the API request body, never on a command line.
- **Timeouts** as on the `local` runner (watchdog → the container is killed → exit 124). stdout and stderr are two streams: each keeps its order, not their interleaving.
- A worker refuses to start on `STACKD_RUNNER=docker` without `STACKD_RUNNER_IMAGES`.

**Runner trust contract (Phase C).** The `docker` runner (prod) is the trust boundary: one **ephemeral** container per command (above); the image carries **no long-lived cloud creds**; an optional egress allowlist is enforced via the containers' network. What holds on every runner: the OIDC token is written to a `0600` file outside the checkout (`Workspace.secret_file`) only for the duration of each tofu command and each platform hook — never while a repo hook runs (§8.3) — exposed to them via `AWS_WEB_IDENTITY_TOKEN_FILE`, and the whole workspace is removed in the `finally` `ws.cleanup()`. Repo code also runs inside tofu (`data "external"`, provisioners), and feeds the check tools platform hooks run (their config files in the checkout): on the `local` runner, tofu and platform hooks get the worker's environment **minus the agent's own settings** — the whole `STACKD_*` namespace (the pool token, the API URL, …, and any platform setting a worker host carries) — then the run's `env` and `sensitive_env` and the OIDC workload env (§10.4); tofu also gets, for managed state, `TF_HTTP_*` (§11.2). Everything else a provider may need stays: `PATH` and locale, proxies and CA bundle, `TF_*` (plugin cache, …), provider settings. Repo hooks run untrusted: `sh -c <repo command>` with an allowlisted `repo_env` (§8.3). The `local` runner (dev) with a mounted `~/.aws` is **trusted-dev only** — never prod (`STACKD_ENV=production` requires the `docker` runner, enforced by the API, see below).

**Production runner gate.** The agent declares its runner (`local` | `docker`) in `POST /worker/v1/register`, on every (re-)registration; an unknown `STACKD_RUNNER` value stops the agent at startup instead of falling back to `local`. A production API (`STACKD_ENV=production`) answers **403** `code=runner_not_allowed` to a registration whose runner is not `docker` — an absent runner (an agent predating the field) included — and audits it (`worker.rejected`, target the pool, context `{name, runner, reason}` — once per pool, worker
name and hour: the agent keeps retrying, slowly — every 5 min — after a 403, logging the refusal); a `/jobs/claim` from a worker whose recorded runner is not `docker` (registered before the API switched to production) gets the same 403. The runner is shown on the Workers page. It is *declared*: it guards against a misconfigured worker, not against a compromised one holding a pool token — the pool token's custody stays the boundary. A production API also refuses to **start** on dev-grade settings: a master key that does not decode to 32 bytes, a `STACKD_JWT_SECRET` under 32 characters, a non-https `STACKD_PUBLIC_URL`, a `local` OIDC signer without `STACKD_ALLOW_LOCAL_SIGNER=true` (§3.11) (next to the existing `STACKD_DEV_AUTH` guard).

### 7.5 Periodic tasks (internal scheduler, multi-replica)

The scheduler module (PLAN §2.1) carries background tasks that must run **exactly once** even with several API replicas:

The loop ticks every 10 s.

| Task | Frequency | Effect |
|---|---|---|
| `worker_lost` detection | every tick | heartbeat > 60 s (`stackd_worker_offline_seconds`) → `offline`; run executing on an offline worker and claimed > 120 s ago (`stackd_worker_lost_seconds`) → `failed (worker_lost)` (never `unconfirmed`/`confirmed`, §3.7); a job holding apply credentials (an `applying` run or a mutating command run, §4.3) only after the apply window = budget + grace (§3.7/§4.2) |
| Notification outbox | every tick | drain `notification_outbox` (§17 intro, CONCEPTS §14): at-least-once, ≤ 5 attempts, no DB transaction held across the HTTP POST |
| VCS post-back outbox | every tick | drain `vcs_outbox` (§18.4) |
| Drift detection | every tick, per-env gate | enqueue the due `-refresh-only` runs (§19.2) |
| Git staleness polling | 15 min (`stackd_head_poll_interval`) | `git ls-remote` per (repo, branch, credentials) → update `head_sha` (§9.6); its own task, so slow git calls never delay the others |
| Cold log archiving | every tick | runs terminal for ≥ 60 s (the agent's last buffered chunks, §5.1) with `logs_archived_at IS NULL` (20 per tick) → S3 `logs/{run_id}/{phase}.log.gz` (§5.2) |
| OIDC key retirement | every tick | `retiring` keys past the longest token TTL → `retired`, out of the JWKS (§3.11) |
| Rollout approval expiry | every tick | a started rollout whose approval has expired is advanced under its lock → `paused (approval_expired)` (§21.4), without waiting for its next event |
| Retention purge | daily | hot `run_logs` rows 7 d after archiving (§5.2); refresh-token families whose every token has expired (§2.5); audit events older than `audit_retention_days` (730) through the distinct `stackd_audit_purge` role (§6.1), only if `STACKD_AUDIT_PURGE_DATABASE_URL` is set |

**Single-execution guarantee**: each task takes a dedicated **PG advisory lock** (`pg_try_advisory_lock(<task_key>)`) before running; a replica that does not obtain the lock skips its tick. The lock is session-level, so it is taken and released on a **dedicated connection** held for the task's whole run, separate from the task's own DB session — the tasks commit mid-way, which returns the session's connection to the pool, and an unlock through it could land on another connection and leak the lock. No permanently elected leader (no external dependency), no double execution. The tasks are **idempotent** anyway (re-running a staleness computation or an archive breaks nothing) — the advisory lock mainly avoids redundant work and races on the `worker_lost` transitions.

**Advisory-lock keys** (one per lock, never shared — two holders of the same key would skip each other's work): 74001 `worker_lost`, 74002 notification outbox, 74003 VCS outbox, 74004 drift, 74005 head poll, 74006 OIDC key retirement, 74007 OIDC key management (the issuer's rotation lock, §3.11), 74008 rollout approval expiry, 74009 log archiving, 74010 retention purge. A new lock takes the next free key and is added here.

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

- Each hook: one shell command (`sh -c`) in the checkout (cwd = project_root), with the run's environment-kind variables (`env`, plus `sensitive_env` for **platform** hooks only). Terraform-kind variables reach tofu only, through the tfvars files (below): no hook gets them. The plan job runs the `before_init` … `after_plan` stages, the apply job (a fresh clone, §7.3) `before_apply` and `after_apply`; a command job (§4.3) runs no hook. **Decided: no opt-in.** A per-environment opt-in giving repo hooks `sensitive_env` was specified and is rejected: a repo hook is code any pusher controls, and a check that needs a secret belongs on the platform side (§8.1). A repo hook never gets it.
- **Cloud credentials (§10) not exported to repo hooks.** The `AWS_WEB_IDENTITY_TOKEN_FILE` / `AWS_ROLE_ARN` variables are injected only into the environment of **tofu** invocations and **platform** hooks (non-bypassable), never into that of **repo** hooks (same reasons as `sensitive_env`: a `.stackd.yml` pushed via PR must not be able to assume the prod apply role and exfiltrate). **Decided: no opt-in** (same reasons): a hook that needs the cloud is a platform hook. A repo hook never gets it. The token file `AWS_WEB_IDENTITY_TOKEN_FILE` names (`<workspace>/oidc_token`, outside the checkout) is on disk only while a tofu command or a platform hook runs — written fresh for each, never through a planted symlink, and removed right after — so a repo hook can't read it either (as `../oidc_token`).
- **Repo-hook env is built from an allowlist.** Of the worker process environment, the agent's `repo_env` keeps only what any shell command needs — `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `TMPDIR`, `TERM`, `LANG`, `LANGUAGE`, `LC_*`, `TZ`, the proxy settings (`HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, `ALL_PROXY`, either case) and the CA bundle location (`SSL_CERT_FILE`, `SSL_CERT_DIR`) — then layers the run's non-sensitive `env`. Nothing else of the worker's own environment reaches a repo hook: a worker host can carry any secret there (the pool token, a database URL, a cloud-credential family such as `AWS_*` — exported, on the **trusted-dev** `local` runner, by a mounted `~/.aws`; the `docker` runner (prod) bakes no long-lived creds into the image), and the state backend's `TF_HTTP_*` (§11.2) go to tofu only. Anything a repo hook needs is set as a run variable. Platform hooks get the worker's environment minus the agent's own settings (the `STACKD_*` namespace, like tofu, §7.4), then the run's `env`, `sensitive_env` and cloud env. A regression test asserts a repo hook sees neither cloud nor sensitive env, nor any other worker variable.
- **Repo hooks at the `*_apply` stages: a tier flag, off by default.** A repo hook at `before_apply`/`after_apply` runs in the job holding the apply credentials, so it runs only on a tier with `repo_hooks_at_apply = true` (§2.4) — a flag, not a tier name, since tiers are a configurable, non-ordered catalog (CLAUDE.md §4, §7). **Fail closed**: the column defaults to `false` for every tier, the default catalog and existing tiers included, and a missing tier row counts as `false`; an admin turns it on per tier (`PATCH /api/v1/tiers/{id}`, audited `tier.updated` with the new values; the UI asks to type the tier's name). The claim carries the env tier's value as `repo_hooks_at_apply`; on the worker, a job without it (an older API) counts as `false`. Where it is off, only **platform** hooks execute at `before_apply`/`after_apply`; each repo hook of those stages is skipped with a visible warning — a `warn` check result with its stage (detail `skipped: …`, so `run.check_warned` audits it) and a log line in its section. The `*_init`/`*_plan` repo stages remain allowed on every tier (plan role = ReadOnly).
- `plan.json` available for reading at the `after_plan` stage — the plan job's last; the apply job is a fresh clone (§7.3) and has none. `tofu show -json` lists the run's variable values in clear, sensitive ones included: **platform** hooks read it as is; before the first **repo** hook runs it is replaced by a **masked copy** — every value tofu marks sensitive (input variables declared `sensitive`, resource attributes, outputs) and every value the agent masks in logs (§5.1) replaced by `***`, the rest kept as valid JSON. The uploaded `plan.json` artifact is that masked copy. Without that document — `show -json` failed, or printed no JSON object — the plan job fails (`plan JSON unavailable, not checked: …`) before any upload or hook: the checks never run against a stand-in empty plan. The plan job writes its plan file (`plan.tfplan`) outside the checkout and deletes it, once shown and uploaded (§7.3), before any `after_plan` hook runs: no repo hook finds the saved plan, nor raw plan JSON, in the workspace. Files the agent writes into the checkout replace whatever is there — a repo-planted symlink is never written through.
- **The run's tfvars are in the checkout only while tofu reads them.** `stackd.auto.tfvars.json` and `zzz_stackd.auto.tfvars` (§3.4) are written right before each tofu command that reads variables — `init`, `plan`, `apply` (so tofu still refuses a saved plan whose inputs changed, §7.3) and a command run's subcommand — and deleted right after it: no hook, at any stage, finds them. tofu auto-loads them as before (same precedence among the repo's own `*.auto.tfvars`, and every subcommand reads them), which `-var-file` would change.
- **The repo token is not in the checkout.** The agent clones with the token in the URL (`repo_credentials`, §7.2), then points `origin` back at the token-free `repo_url` before anything else runs; the pinned-commit fetch (§7.3) is handed the authed URL itself. git keeps no other copy (`FETCH_HEAD` and the reflogs record the URL without its credentials): no hook finds the token under `.git/`.
- **Residual, on the `local` runner (trusted dev only; production refuses it, §7.4).** Hooks run as the agent's own OS user, in no container: a repo hook can reach whatever that user can — the workspace of another job running concurrently on the same worker (`STACKD_MAX_CONCURRENT_JOBS` > 1), the agent's own process environment under `/proc`, a pool-token file. A process that leaves its hook's process group (`setsid`, a new session) escapes the kill below and keeps running. The `docker` runner closes all of it (one container per command, repo hooks seeing the checkout only); on `local`, a worker running one job at a time (`STACKD_MAX_CONCURRENT_JOBS=1`, the default) at least keeps jobs out of each other's workspaces.
- **Nothing a hook starts outlives it.** Each hook runs in its own process group; once its command exits, whatever is left in that group is killed — a background process (`cmd &`), detached from the hook's output or not. Otherwise it would run on while tofu has the tfvars and the OIDC token file on disk, or a platform hook the raw `plan.json`, and it would keep the job's environment, readable under `/proc` by any later hook running as the same user. Platform hooks alike: the check tools they run read the repo's config files (plugins, custom rules), and what lingers would carry the platform env — `sensitive_env` and the cloud env — into the repo stages. tofu too (`data "external"` runs repo code): every command the agent streams is handled the same way.
- Timeout per hook: 10 min, configurable per worker (`STACKD_HOOK_TIMEOUT_SECONDS`), for platform and repo hooks alike. Past it the hook is killed — its whole process group, the processes it started included — and fails by its own `on_failure` (detail `timed out after <n>s`). Logs in a dedicated section of the viewer.
- **`on_failure: fail`** → run `failed`, audit `run.check_failed`. At every stage and from either source: the job fails at that stage (`job_failed`, `phase` = the stage, the failed check in `checks`) before the next tofu step — a failing `before_apply` hook means nothing is applied. A failing `after_apply` hook fails the run although the apply has already changed the infrastructure: the agent reports the outputs it read right after the apply with the failure (`job_failed(after_apply)`, `result.outputs`), so the API records them like a finished apply's (§9.1 — `env_outputs` reflects the infrastructure) and, the state matching reality again, clears the environment's drift as a finished apply does (§19, same transaction); but nothing cascades from the failed run (§9.2).
- **`on_failure: warn`** → the run continues but mandatorily goes through `unconfirmed` (even with autodeploy), warning badge + detail on the run page, audit `run.check_warned`. A human takes responsibility. At the `*_apply` stages the run is already confirmed: the apply goes on and the warning is reported in the apply job's `checks` (`phase_finished`); the API adds those checks to the plan's in `runs.check_results` and audits `run.check_warned` the same way.
- Results aggregated in `runs.check_results` and displayed in the checks status bar (the run page lists every check result — `ok`, `warn` and `fail` — with its stage). A job reports **every** check result — `ok`, `warn` and `fail`, each with the hook `stage` it ran at (`before_init` … `after_apply`; the plan tripwire's `secret_leak_suspected` has `stage: "plan"`) — on its `phase_finished`, or on its `job_failed` whatever ends it: a failing check, a tofu error, a timeout, the plan JSON, an error of the agent's own — the results of the stages that ran. The API keeps them all, plan job then apply job, in `check_results.results`; `check_results.checks` keeps the `warn`/`fail` ones, what needs attention (its meaning before `results` existed). The `run.check_warned` audit context names the warned checks and their `stages`; `run.check_failed`'s `phase` is the failing stage. A job that fails on anything but a check still has its checks recorded, and a `warn` among them audited `run.check_warned`, in the failure's transaction (the failure itself is audited `run.apply_failed`).

  ```json
  "check_results": {
    "results": [
      {"name": "fmt",       "status": "ok",   "detail": null,     "stage": "before_plan"},
      {"name": "infracost", "status": "warn", "detail": "exit 1", "stage": "after_plan"},
      {"name": "smoke",     "status": "warn", "detail": "timed out after 600s", "stage": "after_apply"}
    ],
    "checks": [
      {"name": "infracost", "status": "warn", "detail": "exit 1", "stage": "after_plan"},
      {"name": "smoke",     "status": "warn", "detail": "timed out after 600s", "stage": "after_apply"}
    ]
  }
  ```

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
   on_output_change → trigger if a referenced output's current value ≠ the one in
   resolved_inputs of the last finished run of the downstream env (no such run → trigger)
3. run created: type=tracked, triggered_by=dependency, parent_run_id, run_group_id
4. multi-parents: downstream triggered when ALL its parents of the run group are finished
5. parent failed → branch stopped, run group = partial_failure
6. downstream env protected → stops at unconfirmed (never bypassed: the cascade confirms nothing
   on a protected env — only a human, or a rollout's approval, §2.4/§21.5)
7. a rollout's runs (rollout_id set) never cascade, the rollout being over or not; and no run is
   created on a member of an active rollout (§21.4)
```

Steps 4–5, as implemented (`cascade()`, called when a tracked run ends `finished`: after any
tracked apply — a run without a group roots its own, the runs it triggers carry its id as
`run_group_id` — and after an **empty plan** of a run that carries a group, since it may be the
parent a downstream waits for):
- *The whole group is decided at once*, under a lock on the group's root run (two parents finishing
  at the same moment are serialized: the second sees the first's outcome). Every environment
  reachable from the root's env — the group's *scope*, loaded edge level by edge level from the
  root's env — is settled in topological order. With a run in the group it is `done` (finished;
  *applied* if confirmed), `broken` (failed, discarded, canceled) or `active`. Without one, from its
  upstreams in the scope, in this order: one `broken` → `broken`; else one `active` or `waiting` →
  `waiting`; else (all settled) it is *decided*: a run is created if an edge from an *applied*
  upstream passes its policy (step 2), else it is `skipped` (settled, no run).
- *A decision is taken once.* An environment is decided by the cascade of the run that settles its
  last upstream — the only cascade from which it is reachable once all its upstreams are settled.
  A later cascade of the group (from a run that is not upstream of it) keeps it as it is: a
  `skipped` environment is never revisited, even if an upstream's live outputs change meanwhile
  (e.g. an apply of that upstream outside the group), so it never gets a late run in the group.
  `on_output_change` is therefore read once, against the outputs current at that decision.
- *Multi-parent gating.* So a downstream waits for every upstream in the scope that may still run —
  a direct parent, or one only reached through another environment of the group, also one still
  `waiting` itself — whatever the order the edges are read in, and is triggered once, by the last of
  them. An upstream outside the scope does not hold it back (its current outputs are used). A
  downstream gets at most one run per group.
- *Parent.* A triggered run's `parent_run_id` is the run of an *applied* upstream whose edge passed
  its policy — of those, the one that finished last (ties: the greater run id). It is not
  necessarily the run that released the downstream (e.g. an empty plan, or a parent whose `never`
  edge did not pass); that one is recorded in the `run.triggered` audit context as
  `released_by_run_id`, next to `parent_run_id`.
- *Stop on failure.* A run of the group ending `failed`, `discarded` or `canceled` triggers nothing
  downstream, and every environment reachable from it — also through environments that never got a
  run — gets no run in that group.
- *Group status*, computed (not stored) and exposed on `GET /api/v1/runs/{id}` as `run_group`
  (`{id, status}`, for the group the run belongs to or roots; runs also carry `run_group_id` and
  `parent_run_id`): `running` while a run of the group is queued or active, else `partial_failure`
  if one failed, was discarded or canceled, else `succeeded`. A group can cross spaces (its edges
  can): the status counts only the runs of the spaces the caller may read (§2.4) — every run for an
  instance admin.
A cascade run is routed like any other plan (§4.2): it auto-confirms only on an `autodeploy`,
unprotected environment, with no mock and no `warn` check.
- *Environment rollouts* (§21). A run that belongs to a rollout never calls the cascade — even one
  finishing after its rollout was canceled: the rollout drives its members, and a member's
  downstreams outside the rollout are not triggered (they show as stale, §9.6; include them in the
  rollout, or trigger them). A downstream that is a member of an **active** rollout (any non-terminal
  state, `awaiting_approval` included) gets no cascade run either: it is settled `skipped` in the
  group, and the rollout plans it in its turn, reading the then-current outputs at claim. If that
  member had already planned (status other than `pending`), the rollout's creator and approver get
  a `rollout_upstream_changed` notification (§17.1) instead of a run planned behind the rollout's
  back, naming the run that would have been its parent (the rule above). Like any decision of the
  group, this one is taken once. Rollout runs carry no `run_group_id`: the rollout is their group.

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
   - `unconfirmed → confirmed` **refused** if `used_mocks` and `environment.allow_mock_apply=false` (default): a mocked plan serves to validate the config, not to be applied (409)
   - re-checked at the apply claim against the apply-time resolution: an input that falls back to a mock between confirm and apply fails the run (`dependency_mock:mock_apply_disabled`) on such an env
   - never autodeploy a mocked run
   - proposed runs (PR) use mocks freely (plan-only by nature)

**Good mock values**: plausible for the type expected by the provider (`vpc-mock00000000`, `subnet-mock...`) — documented, with examples per common AWS resource type.

### 9.4 Run groups

`POST /environments/{id}/runs` with `{"with_downstream": true}` in the body: the run roots its own group (`run_group_id` = its id), so even an empty plan of it cascades (§9.2); subsequent levels via cascade, which computes the subgraph (BFS from the root's env, level by level) and its topological order (Kahn). UI: graph colored by state (see DESIGN.md §5.4).

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

**C. Standing up a new region or a new environment across all stacks** (§21)

1. A variable set with a label **selector** (`region: eu-west-3`) carries everything the region
   changes (§3.4). ⚠ An environment's labels also **route its runs** (§7.2: a worker takes a run
   only if it carries all of the env's labels): a `region` label on the env needs a worker pool
   labelled `region: eu-west-3` too — often what a region wants anyway (workers in the region). If
   not, put the region set on the env by explicit attachment instead of a label selector.
2. `POST /spaces/{id}/environment-clones {reference_name: "prod", name: "prod-euw3", overrides:
   {labels: {region: "eu-west-3"}}}` creates `prod-euw3` in every stack that has a `prod`, with its
   governance and its dependency edges — the selector attaches the region set by itself, and a
   model's env variable or explicit attachment that would shadow one of its keys is left out
   (`not_copied`, §21.2). Fill in the reported `missing_secrets` (and the cloud integrations, if not
   copied); until then the new envs have `autodeploy` off.
3. `POST /rollouts {env_name: "prod-euw3"}`, approved once by an approver: the graph is planned and
   applied level by level, every creates-only plan confirmed under the approval; anything else stops
   for a human (§21.5).

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
- `↑N` chip on the env cell (DESIGN.md §5.1) and the env header; attenuated variant if `affects_project_root = false` ("the branch moved ahead, but not this folder"). Shipped: the chip (`ModifierChips`, overview matrix and env header) and the banner below (run page).
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
  "iat": <now>,
  "exp": <now + min(session_duration, max duration of the phase)>
}
```

`iss` is `STACKD_PUBLIC_URL`; the token is RS256, its header carries the signing key's `kid`. `session_duration` is the integration's (default 3600 s). For the `plan` phase the phase cap is 30 min. For the `apply` phase — an apply, or a mutating command run (§4.3) — the phase cap is the apply window (`apply_reclaim_window_seconds`, §3.7), so the cloud credentials never outlive the job's worker-lost reclaim cutoff.

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

The worker exchanges nothing itself in the simple case: it writes the token to a file and exports `AWS_WEB_IDENTITY_TOKEN_FILE` + `AWS_ROLE_ARN` + `AWS_ROLE_SESSION_NAME=stackd-{run_id}` (+ `AWS_REGION` when the integration sets one) to tofu and platform hooks only — never repo hooks — and the file exists only while one of them runs (§8.3) — the AWS SDK in the providers does the AssumeRoleWithWebIdentity natively. The session name = run_id → **CloudTrail traces every AWS action back to the run** (and therefore back to the human who confirmed: complete audit loop).

Fallback: no `cloud_integration` → classic static variables (variable set `aws-credentials`), current behavior.

### 10.5 Priority and coexistence

If a `cloud_integration` exists AND static AWS credentials are resolved → the OIDC credentials win, config warning displayed (a classic source of confusion). "Win" has to be literal: the AWS SDK's credential chain reads static keys **before** the web identity token, so layering the OIDC variables on top is not enough. Hence:

- **At claim (API)**: every environment-kind variable the SDK reads as a credential — `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_SECURITY_TOKEN`, `AWS_PROFILE`, `AWS_DEFAULT_PROFILE`, `AWS_SHARED_CREDENTIALS_FILE`, `AWS_CONFIG_FILE`, `AWS_CONTAINER_CREDENTIALS_{FULL,RELATIVE}_URI`, `AWS_CONTAINER_AUTHORIZATION_TOKEN` — is dropped before the payload is built: not in `env`/`sensitive_env`, not fetched when it is an external reference (§15), not in `mask_values`. The run's `variable_provenance` records it as `shadowed:<layer>` (e.g. `shadowed:set:aws-credentials`). Settings that are not credentials (`AWS_REGION`, …) stay.
- **On the worker**: the same names are dropped from the worker host's own environment (the trusted-dev `local` runner's mounted `~/.aws`) for tofu and platform hooks of a job carrying OIDC credentials.
- **Warning**: `GET`/`PUT /environments/{id}/cloud-integration` return `warnings: [{code: "static_aws_credentials_shadowed", variables: [names], detail}]` — names only, never values — shown on the env's cloud panel.

Without a `cloud_integration`, static credentials are the env's fallback (§10.4) and pass unchanged.

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

Auth: Basic (username ignored, `env`), password = JWT `{env_id, run_id, scope, typ: "state", iat, exp}` (HS256, `STACKD_JWT_SECRET`; invalid → 401, another env's → 403), minted at the claim (§7.2) for **one job**, not for the whole run. Its `scope` is fixed at mint time from the job, so a run's plan token and its apply token are different tokens with different rights:

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

The bucket is `S3_BUCKET` (default `stackd`). *Status*: the API does not request SSE-KMS on its writes yet — encryption at rest is the bucket's default encryption, and versioning the bucket's own setting (PLAN Phase 3).

`artifacts/{run_id}/plan.tfplan` is the run's **saved plan** (§7.3): it holds the run's variable values in clear, sensitive ones included, so the API encrypts it **AES-256-GCM with the application key** before it reaches S3 (layout `nonce(12) || ciphertext || tag`, AAD = `stackd:artifact:<s3 key>` so a blob moved under another run's key does not decrypt) — on top of the bucket's SSE. Its key is recorded on the run (`runs.plan_artifact_key`). **Retention**: it lives only while the run can still be applied. On any transition to a terminal state (`finished`, `failed`, `discarded`, `canceled` — whatever the path: worker event, user, scheduler), `transition()` clears `plan_artifact_key` in the same transaction and the object is deleted once that transaction commits (best effort: a failed delete is logged; the blob stays encrypted and the API serves it to nobody, since only an `applying` run's worker can download it). `plan.json`/`outputs.json` are accepted but not archived yet.

`managed_state: false` → no HTTP backend injected, the repo keeps its own `backend` block. For a **partial** backend, set either/both (per-env, applied at init in this order — later `-backend-config` wins): `environments.backend_config_file` (repo-relative `.config`/`.tfbackend` path → `-backend-config=<file>`) and `environments.backend_config` (a `{key: value}` map → one `-backend-config=key=value` each, e.g. `bucket`/`key`/`region`/`dynamodb_table`/`use_lockfile`). The inline map is the way to onboard a new tier on shared code without a per-env `.config` file: set its own `key` so it doesn't collide with another env's state. Mutually exclusive with the managed HTTP backend.

### 11.4 Importing an existing stack

To adopt a stack that already has remote state into Stackd-managed state (`managed_state: true`):
`POST /api/v1/environments/{env_id}/state/import-session` (admin; 409 unless the env has
`managed_state` on) mints a short-lived (30 min),
run-less `rw` state token and returns a ready-to-use `http` backend config. The operator migrates
their current state with one standard command — `TF_HTTP_ADDRESS=… TF_HTTP_PASSWORD=… tofu init
-migrate-state`, the settings as `TF_HTTP_*` environment variables, never `-backend-config` (tofu
writes those, the token included, into `.terraform/`) — which `LOCK`s, uploads the state via §11.2 (stored as a `state_version` with
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
GET  /api/v1/oidc/keys ; POST /api/v1/oidc/keys/rotate { kms_key_arn? }   # admin; signing keys
                                                   #   (§3.11), rotation audited oidc_key.rotated

# Spaces & memberships (§2.4, Phase F)
GET|POST /api/v1/spaces                                  # create = instance admin
GET|PUT  /api/v1/spaces/{id}/members ; DELETE /api/v1/spaces/{id}/members/{user_id}   # space admin

# External secret sources (§15) — writes admin
GET|POST /api/v1/spaces/{id}/secret-sources ; PATCH|DELETE /api/v1/spaces/{id}/secret-sources/{sid}

# Stacks & environments
GET|POST /api/v1/stacks ; GET|PATCH|DELETE /api/v1/stacks/{id}          # DELETE (stack/env): 409 while
POST /api/v1/stacks/{id}/check-repo                                    #   a run or rollout is active (§3.2)
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
GET|PUT|DELETE /api/v1/environments/{id}/cloud-integration   # PUT/DELETE: admin
POST /api/v1/environments/{id}/cloud-integration/test    # admin; verification AssumeRole

# Runs
POST /api/v1/environments/{id}/runs      { type?, with_downstream?, commit_sha?, secret_overrides? }
POST /api/v1/environments/{id}/commands  { command, args, commit_sha? }     # §4.3
POST /api/v1/environments/{id}/promote   { from_environment_id }           # §9.7
GET  /api/v1/environments/{id}/runs ; GET /api/v1/runs/{id}   # the latter adds run_group (§9.2) and
                                    # confirmed_via: user | autodeploy | rollout (§21.5)
POST /api/v1/runs/{id}/confirm | /discard | /cancel   # cancel of an executing run = a stop
                                    # request to its worker (§4.1); can_apply if it holds apply creds
POST /api/v1/runs/{id}/vcs/resync                                          # §18.5
GET  /api/v1/runs/{id}/logs ?phase=&after_seq=          # hot or archived tier (§5.2)
GET  /api/v1/runs/{id}/logs/download ?phase=all|<phase> # plain text attachment (§5.2)
GET  /api/v1/runs/{id}/plan ; GET /api/v1/runs/{id}/checks
WS   /api/v1/ws                     sub: run:{id}, environment:{id}, user:{own id}, rollout:{id}

# Dependencies
GET|POST /api/v1/environments/{id}/dependencies          # references with mock_value
POST /api/v1/stacks/{id}/dependencies/link-by-name       { upstream_stack_id, trigger_policy? }
DELETE /api/v1/dependencies/{id}
GET /api/v1/environments/{id}/outputs ; GET /api/v1/graph

# Environment clones & rollouts (§21)
POST /api/v1/spaces/{space_id}/environment-clones
POST /api/v1/rollouts ; GET /api/v1/rollouts ; GET /api/v1/rollouts/{id}
POST /api/v1/rollouts/{id}/approve | /retry | /cancel

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
GET /api/v1/health         # any auth: DB, workers (online + heartbeat), active/queued runs, recent errors
GET /api/v1/logs           # admin: structured JSON buffer, filters level/logger/event/worker_id/
                           #   run_id/request_id/q, limit

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

Specified but **not served yet**: `GET /api/v1/run-groups/{id}` (§9.4).

---

## 13. Security — summary

| Surface | Measure |
|---|---|
| Human auth | Google OIDC + PKCE, JWKS/nonce, `hd` restriction, rotating refresh |
| Cloud credentials | **OIDC workload by default**: tokens signed per run/phase, trust policies on claims, session name = run_id (CloudTrail ↔ Stackd audit). Static = fallback |
| OIDC signing key | KMS (`kms:Sign`, required in production unless explicitly acknowledged) or AES-GCM in the DB (`oidc_signing_keys` §3.11), rotation with overlap and automatic retirement, short token TTL. `sub` wildcard allowed **only** on the stack segment; never on `tier` nor `phase` (§10.3) |
| Static secrets | AES-256-GCM, write-only, decrypted at claim, masked in the logs |
| Hooks | platform hooks not bypassable by PR; repo hooks without `sensitive_env` **nor cloud credentials**, with an allowlisted env, a masked `plan.json`, and no saved plan, tfvars, OIDC token file or repo token on disk while they run; the agent's `STACKD_*` settings reach no hook nor tofu; masking of sensitive values in their stdout; the process group killed when the hook exits, or past its 10 min timeout; a failing `fail` check stops the job at any stage (§8.3); on the `docker` runner each hook in its own container, a repo hook seeing the checkout only (§7.4). repo hooks at the `*_apply` stages only on a tier that allows them (`repo_hooks_at_apply`, off by default) |
| Mocks | apply forbidden by default (`allow_mock_apply=false`), badge, audit `mock_consumed` |
| External secrets | provider bootstrap credential AES-256-GCM write-only (`secret_sources.bootstrap_secret_encrypted` §15.1); live values fetched at claim, never persisted, masked; **fallback value (static or break-glass override) forbids apply by default** (`allow_fallback_apply=false`), badge, audit `secret.fallback_used` (§15) |
| Workers | revocable tokens, no incoming port, labels (isolated prod pool); production accepts the `docker` runner only (§7.4); tool binaries verified by signature then checksum when the runner images are built, and those images run only by digest (§7.4, supply-chain). The `local` runner (dev) runs the binary on its PATH, at the stack's version |
| State | S3 SSE-KMS via API only; per-job state tokens scoped `ro` (proposed runs, read-only commands) / `plan` (lock, never write) / `rw` (apply, mutating command, import session), refused once their run is terminal; handed to tofu only, as `TF_HTTP_*` env (§11.2); audited force-unlock |
| Saved plans | the reviewed `plan.tfplan` is AES-256-GCM-encrypted by the API (AAD bound to its S3 key), served only to the run's apply job, applied as-is (never re-planned), deleted on any terminal state (§7.3, §11.3) |
| Outbound notifications | https only; the host must resolve to public addresses only; the POST dials the vetted IP (Host/SNI/certificate stay on the hostname), ignores env proxies (`HTTPS_PROXY`…) by design, never follows redirects |
| Apply permissions | tier per env × `allowed_tiers` set per user (§2.4, configurable catalog, non-linear); distinct `can_destroy`; double lock with the OIDC trust policy on the tier name |
| Protected envs | autodeploy forbidden and 4-eyes (also automatic on a tier flagged `requires_four_eyes`, e.g. `prod`), never bypassed (cascade included); the *right* to apply comes from the tier, not from `protected`. A rollout's approval (§21.5) counts as the human decision only for creates-only plans in an environment never applied, approved under four-eyes, and only while the environment's tier, `protected` and four-eyes are what the approver saw |
| Proposed runs | plan-only, state RO, mocks allowed; **secrets not injected**: repo code runs inside tofu (§7.4), and a pull request's has not been reviewed. The claim drops every sensitive variable, of either kind, and every external secret reference (§15, not even fetched) — so nothing of them is in `sensitive_env`, the tfvars, `mask_values` or the run's `variable_provenance`. One the plan needs gets no value, never a stand-in: tofu uses the variable's default in the repo, or fails with `No value for required variable`. Kept, as a plan needs them: the **plan role**'s OIDC credentials (read-only by design, §10.3 — scope it so), the `ro` state token (the state itself is readable: it can hold secrets of its own), the repo token to clone (it reaches neither tofu nor a hook, §8.3). A drift run (§19), a proposed run of the env's last applied commit, keeps the secrets. **Decided: no opt-in.** A per-environment opt-in giving proposed runs the secrets was specified and is rejected: it would hand them to unreviewed code |
| Webhooks | HMAC SHA-256 (secret per repo, `stacks.webhook_secret_encrypted` §3.1), anti-replay 5 min |
| Sessions | access JWT 15 min in Bearer header; refresh httpOnly `SameSite=Strict` + CSRF double-submit, rotation with reuse detection → family revocation (§2.5) |
| Encryption at rest | AES-256-GCM, random 96-bit nonce per value, never reused (§1) |
| Rate limiting | Google callback + `/auth/refresh`, `/webhooks/github`, `discover-inputs`: per-IP token buckets, per replica (§20.3). Worker register/claim are not throttled yet; hardening Phase 7 |
| Audit | append-only **at the DB level** (INSERT-only role + anti-update/delete trigger §6.1), denormalized, transactional, 2 years |

---

## 14. Tests — minimal strategy

- **API unit tests**: transitions (`can_apply` per tier including apply-everywhere-except-prod, `can_destroy`, auto 4-eyes on a `requires_four_eyes` tier or a `protected` env **and its human-triggerer-only scope**, mock-apply blocking, warn → forced confirmation), 6-layer variable resolution + provenance (space-scoped attachments), a proposed run's claim payload carrying no secret (a tracked or drift run's still does), hook merging (platform→repo order; `*_apply` repo hooks skipped on a tier without `repo_hooks_at_apply`, §8.3), check results kept with their stage across the plan and apply jobs (a job failing on no check included, its warnings audited), an `after_apply` failure recording outputs and clearing drift without cascading, cycle detection, multi-parent cascade (§9.2), the rollout gate (§21.5: creates-only on a never-applied env, the pinned commit, four-eyes and member changes re-checked at auto-confirmation, a cancel racing the gate, the rollout-then-run lock order), mock resolution (real > mock > error), signature/claims of workload tokens (tier segment in the `sub`, wildcard limited to the stack segment), Google id_token validation, **refresh rotation + reuse detection → family revocation**.
- **Integration**: Postgres testcontainers; **concurrent claim → only one wins, the loser catches `23505` and re-polls**; HTTP backend with real `tofu` + Garage; log idempotency; **masking of sensitive values (`tfvars` + env) in a hook's stdout**; `after_plan` hook that reads plan.json; **WS fan-out via `LISTEN/NOTIFY`**; **single execution of a periodic task under advisory lock with 2 replicas**; AssumeRoleWithWebIdentity against a mock STS (moto).
- **E2E**: the live compose stack + fixture repos (built-in `terraform_data`, no provider) → on a throwaway copy of the demo graph, a 2-stack cascade **with mocks** (mocked plan → upstream apply → real cascade), the role/mock gates, and the `run.confirmed` audit actor (DEV §7).
- **Agent**: exit codes (a plan only on 0/2) and the plan JSON failing closed, secret masking and the masked `plan.json`, hooks at every stage and from both sources (`fail` stops the job, `warn` goes on, the timeout kills the process group, a background process dies with its hook), what a repo hook can read (no saved plan, tfvars, OIDC token file or repo token; an allowlisted env), the `STACKD_*`-free env of tofu and platform hooks, saved-plan upload and fail-closed apply, commit pinning, the `TF_HTTP_*` backend env (tofu only), the command time budget from `cred_phase`, token encoding in clone URLs, writing the OIDC token and exporting the env vars.

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

**New table `secret_sources`** (scoped per space, §3.0):

```sql
CREATE TABLE secret_sources (
    id                        uuid PRIMARY KEY DEFAULT uuidv7(),
    space_id                  uuid NOT NULL REFERENCES spaces(id) ON DELETE CASCADE,
    name                      text NOT NULL,
    provider                  secret_provider NOT NULL,   -- enum: proton_pass (the only one implemented; vault | aws_secrets_manager | ... to come)
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

**A reference stays in its space.** `secret_source_id` must name a source of the variable's own
space — the stack's for a stack or environment variable, the set's for a variable-set variable — on
create and on every update that re-points it; otherwise **404** `Secret source not found` (another
space's source is answered like a missing one, its existence not confirmed). At claim, a reference
whose source is not in the environment's space (a row predating the check) fails the run closed,
`secret_unavailable:{var}`, with no fallback.

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
override, used for that run only: it is sealed AES-256-GCM on the run
(`runs.secret_overrides_encrypted`, never returned) and read back at its claims:

```
POST /api/v1/environments/{id}/runs
  { ..., "secret_overrides": { "<variable_name>": "<value>" } }
```

- **Permission**: supplying an override is an apply-affecting bypass → requires
  `can_apply(user, env)` (§2.4). It is rejected unless the targeted variable's
  `secret_fallback_mode = break_glass` (a source must opt in to being overridable).
- The override value is injected like a sensitive variable, added to `mask_values`, and sets
  `run.used_secret_fallback = true`, provenance `secret_override:{source_name}`.
- Audit `secret.fallback_overridden` (target: the run), `context = {variable, source}` — **the value itself
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

`/api/v1/runs/{run_id}/comments` — listing & posting are open to anyone who can read the run
(collaboration, reader+ in its space); editing a body is author-only; resolving a thread needs the
author or `role ∈ {approver, admin}`; deleting needs the author or admin.

| Method | Path | Who | Effect |
|---|---|---|---|
| GET | `/comments` | reader+ | list (oldest first; replies carry `parent_id`) |
| POST | `/comments` | reader+ | create `{body, anchor?, parent_id?}` — one level of replies: `parent_id` names a root comment (a reply to a reply → 422) |
| PATCH | `/comments/{id}` | author / approver | edit `body` (author) and/or `resolved` (author or approver+) |
| DELETE | `/comments/{id}` | author / admin | delete (cascades replies); a root with replies: admin only (409 otherwise) |

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
> (`notification_outbox`, §7.5): a per-user inbox surfaced as a bell in the UI. Side records only — no run-state
> change (`transition()` untouched), live over the existing `LISTEN/NOTIFY` transport (§5.3).

### 17.1 Model & events

`user_notifications` (per user): `user_id`, `kind`, `run_id?`, `comment_id?`, `context jsonb`,
`read_at?`, `created_at`. `kind ∈ approval_request | run_finished | run_failed | comment_reply |
mention | drift_detected | rollout_paused | rollout_succeeded | rollout_failed |
rollout_upstream_changed` (`drift_detected`: §19.4; the `rollout_*` ones: §21.7, context
`{rollout_id, name, …}`, no `run_id`). Each row is created **in the same transaction** as its trigger:

- **`transition()`** (§4.2): `→ unconfirmed` fans out `approval_request` to every eligible approver
  — the users who pass `can_apply` on the env (§2.4), evaluated with their **effective** space
  membership (members of the env's space, instance admins included); a `destroy` run additionally
  requires `can_destroy`; excluding the triggerer. `→ finished` / `→ failed` notify the run's human
  triggerer (`run_finished` / `run_failed`); `canceled` and `discarded` notify nobody.
- **comment create** (§16): a reply notifies the root author; `@mention` tokens (matched against a
  user's email local-part or full email) notify the mentioned users. Never the author. The composer
  offers `@` autocomplete from `GET /users/mentionable` (a minimal directory readable by any auth),
  inserting `@<email-local-part>` so it resolves against the parser.
- **rollouts** (§21.7): `advance_rollout()` notifies the rollout's creator and approver of a pause or
  a change of pause reason (`rollout_paused`) and of the end (`rollout_succeeded` /
  `rollout_failed`); the cascade (§9.2) notifies them of an upstream applied under a member that
  already planned (`rollout_upstream_changed`). A member run is an ordinary run for the
  `transition()` notifications above (its triggerer is the rollout's creator).

### 17.2 Live delivery — private per-user channel

`notify()` emits `pg_notify('user_<uid>', …)` alongside the row. The WS hub maps a `user:<id>`
subscription to `user_<id>` **only for the connecting user's own id** (`channel_for(sub, user_id)`,
§5.3) — the feed is private; you cannot listen on someone else's signals. The bell subscribes to its
own channel and invalidates the `["notifications"]` query on any signal (no payload trusted).

### 17.3 API

`GET /api/v1/notifications` (current user, newest first, capped at 50) ; `POST /api/v1/notifications/read`
(`{ids?}` — omit to mark all of the user's unread as read). Opening the bell marks all read.
`DELETE /api/v1/notifications/{id}` dismisses one of the caller's own; `DELETE /api/v1/notifications`
clears the caller's already-read ones (unread ones are kept).

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
`vcs_outbox` (transactional outbox, same shape as the notification outbox §7.5): `id`, `run_id`,
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
not state vs code), signalled by `refresh_only` in the claim payload. Unlike a pull request's
proposed run, a drift run gets the environment's secrets (§13): it plans the last applied commit,
reviewed code that already ran with them, and refreshing needs them as much as applying did. To
keep drift behind user work, the claim order-by (§7.2) sorts `is_drift` ascending — a queued user run is always claimed
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
`last_drift_checked_at` + `drift_run_id`. Front: a neutral drift chip (glyph + label, never colour
alone — DESIGN §7) on the env page (linking the drift run's plan) and in the overview matrix, a
"Drift" group in the overview's attention zone, and a "Drift checks" filter in an env's run history.

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
stack is imported lazily). Spans are those of the two instrumentations — every HTTP request (the
worker API's claim and events included) and its SQL queries, service name `stackd-api`; no custom
span is emitted.

### 20.3 Guardrails
- **Rate limiting** (`app/ratelimit.py`): an in-process token bucket per (limiter, client IP),
  per-replica (no Redis in the MVP). The buckets live in an LRU hard-capped at 10 000 keys under a
  lock (the least recently used bucket is evicted at the cap), so memory stays bounded. Applied to `auth/google/callback` + `auth/refresh` (one shared
  bucket, 30/min, burst 15), `webhooks/github` (120/min, burst 40), and `discover-inputs` (10/min, burst 5). 429
  problem+json when empty. `/me` is intentionally unthrottled (the UI polls it). Dev login is not
  throttled (dev-only, dropped in prod).
- **Discovery clone caps**: the input-discovery clone is already `--depth 1 --single-branch` +
  30 s-bounded; Phase H adds a post-clone **size budget** (`STACKD_DISCOVERY_MAX_REPO_MB`, default
  200 → 413) and a **`.tf` file cap** (`STACKD_DISCOVERY_MAX_TF_FILES`, default 500 → 413).

### Invariants
`/metrics` and traces never include secret values or tfvars; metrics are cardinality-bounded (label
by state/phase/result, never by run id).

---

## 21. Environment rollouts — declare once, apply the graph (post-MVP)

> **Status:** delivery in three lots (PLAN Phase 7): (1) the cascade gaps of §9.2 (multi-parent
> gating, stop on failure) — **done**; (2) the multi-stack declaration (§21.2) — **done**; (3) the
> rollout itself (§21.3–§21.8) — **done**. Rollout expiry is evaluated when the rollout moves (an
> event, an approval) and by a periodic task (§7.5) within a scheduler tick.

**Need.** Standing up a new environment or a new region today means creating one environment per
stack, re-creating its dependency edges, then planning and confirming every environment by hand, in
the right order. The goal: **declare the new environment once** (from an existing one), then **apply
the whole dependency graph with a single approval**, without weakening any guard.

**Non-goals.** No region dimension in the model (§9.5: the region stays configuration, carried by
labels and variable sets); no synchronisation of an environment matrix over time (the PLAN Phase 7
"environment matrix" track stays open); no destroy rollout; no cross-space rollout.

### 21.1 Concepts

- **Environment clone** (§21.2): a one-shot creation, in N stacks of a space, of an environment named
  `name`, modelled on the environment named `reference_name` of each stack — settings, governance and
  dependency edges included. Nothing links the clones to their reference afterwards.
- **Rollout** (§21.3): an ordered, approved plan → apply of a set of environments (its *members*)
  along their dependency edges. A rollout is created, then **approved once** by an approver; from then
  on each member is planned when its upstream members are done, and **auto-confirmed** when its plan
  passes the rollout gate (§21.5). Anything else pauses the member for an ordinary human confirmation.

### 21.2 Declaring: environment clone

```
POST /api/v1/spaces/{space_id}/environment-clones
{
  "reference_name": "prod",              # the env each target stack already has
  "name": "prod-euw3",                   # the new env, same name in every stack
  "stack_ids": [...]?,                   # default: every stack of the space with a `reference_name` env
  "overrides": {                         # optional; applied on top of the reference's settings.
                                         # Each key may be omitted, never `null` (→ 422)
    "tier"?, "branch"?, "labels"?,       # labels: merged onto the reference's (override wins per key);
                                         # they also route the env's runs to workers (§7.2, §9.5 C)
    "autodeploy"?, "protected"?, "require_second_pair_of_eyes"?, "drift_check_enabled"?
  },
  "copy_cloud_integration": false,       # true requires the instance admin role (§10)
  "dry_run": false                       # true: validate and compute everything, then roll back —
                                         # the response is the preview (DESIGN §5.11); no row and
                                         # no audit event persists
}
```

All or nothing, in **one transaction**. Permission: the global `writer` role and `writer` in the
space (what creating environments takes), plus the **instance** `admin` role when
`copy_cloud_integration` is true (as `PUT …/cloud-integration`), and `writer` on the space of every
shared upstream an edge is re-created from (as creating that edge by hand, §3.8) — else 403. Validation first: a target stack
without a `reference_name` env, or already holding a `name` env → **422** listing the stacks; the tier
must exist (§2.4); `name` follows the environment naming rules; an override sent as `null` → **422**
(every overridable setting is required on an environment). Integrity conflicts at write time are
named for what they are: `name` created concurrently in a target stack → **409**; the override tier
deleted between the check and the insert → **422** `Unknown tier`; any other concurrent change → **409**
`Conflicting change`. In every case nothing is created.

What each new environment gets from its stack's reference environment:

| Item | Copied? |
|---|---|
| tier, branch, `protected`, `require_second_pair_of_eyes`, `autodeploy`, `managed_state`, `drift_check_enabled`, labels, position | yes (then `overrides`) |
| `allow_mock_apply`, `allow_fallback_apply` | **no** — always `false` on a new env (§9.3, §15.5) |
| unmanaged-state backend settings (`backend_config_file`, `backend_config`) | **never**: they would point the new env at the reference's state. A reference with `managed_state=false` → **422** (clone a managed-state env, or create that env by hand) |
| env-level variables | non-sensitive ones: yes, same value; external secret references (§15): yes, the **reference** (not a value) — its static fallback is a literal secret: not copied, the mode reset to `error`, listed in `to_configure`; literal **sensitive** values: **no** — listed in the response as `missing_secrets` (the region's secrets usually differ; a secret is never duplicated silently). Except when it **shadows a label set** (below) |
| explicit variable-set attachments to the env | yes, same priority — except when it **shadows a label set** (below). Tier attachments and label selectors (§3.4) apply by themselves — the recommended place for region values, e.g. a set selecting `region=eu-west-3` |
| **platform hooks** scoped to the env (§8.1) | **yes, always** — a new env never starts with less governance than its model |
| notification targets scoped to the env (§17) | yes |
| cloud integration (§10) | only with `copy_cloud_integration` (admin); otherwise listed as `to_configure`. Role ARNs often differ per region or account |
| dependency edges (§3.8) | see below |

**Label sets win over the model's region (shadowing).** A set attached by selector sits at the
weakest resolution layer (§3.4 layer 1), below explicit env attachments (layer 4) and env variables
(layer 6). A clone whose labels select a set the reference is not selected by (recipe C, §9.5:
`region=eu-west-3` → `region-euw3`) would otherwise keep the model's value for every key that set
provides — the new env would plan creates in the old region. So an env-level variable whose
(kind, name) a **newly selected** label set provides, or an explicit attachment of a set providing
any such key, is **not copied**; each is listed in `not_copied`
(`{stack, environment, item: "variable" | "variable_set", name, reason: "shadows_label_set", keys,
label_sets}`). A set selected by both the reference's and the clone's labels is not "new": an env
value overriding it in the model keeps doing so in the clone. The shadowing that tier attachments,
stack attachments and stack variables already exert is not changed (they are not copied).

**Autodeploy is never on while something is left to fill.** A secret not copied leaves the env-level
input unset, and resolution then falls back to a lower layer holding the same name (a stack variable,
a set) — e.g. the dev `db_password`. A new env with `missing_secrets` or `to_configure` entries is
therefore created with `autodeploy=false` whatever the reference or the overrides say
(`autodeploy_off_reason: "left_to_fill"`; `"protected"` when §3.2 forced it); it is turned on by hand
once filled. Each `missing_secrets` entry carries `falls_back_to`: the provenance (`stack`, `set:<name>`)
of the value the input would resolve to meanwhile, or `null` when nothing provides it.

**Dependency edges.** Let *C* be the set of reference environments being cloned. For each edge of the
reference graph, with its `trigger_policy` and its `output_references` (mocks included):
- both ends in *C* → the edge is re-created between the two **new** environments;
- the upstream **outside** *C* (a shared, non-cloned environment — e.g. a global DNS env) → the new
  environment depends on **that same** upstream;
- the downstream outside *C* → **not** copied: an existing environment never starts depending on the
  new one by side effect.
The resulting edges mirror an acyclic subgraph plus edges from existing envs: no cycle can appear (the
§3.8 DFS still runs). Each edge of the response carries `upstream_kind`: `cloned` (both ends new),
`shared` (a genuinely shared env: its stack has no `reference_name` env, or it lives in another
space), or `reference_of_uncloned_stack` — the upstream **is** a `reference_name` env of a stack of
the space left out of an explicit `stack_ids`. The latter is wired like a shared upstream (nothing else
exists to wire it to) but usually is not what was meant: the new region's `app` would read the **old**
region's `network` outputs. It is therefore also listed in `warnings`.

**Warnings** (preview and response, `warnings: [{stack, environment, kind, …}]`; they never block —
the user decides in the review step, DESIGN §5.11):
- `reference_upstream` (`upstream`: `stack/env`, `upstream_env_id`): an edge of kind
  `reference_of_uncloned_stack` (above). Clone that stack too, or re-wire the edge afterwards.
- `tier_changed` (`reference_tier`, `tier`, `secret_references`, `variable_sets`, `cloud_integration`):
  a `tier` override moves the clone to another tier than its reference's (e.g. `prod` → `dev`). The
  clone still carries what was set up for the reference's tier — its external secret references
  (e.g. prod's vault paths), its explicit set attachments, its platform hooks and, when copied, its
  cloud integration — now under a tier with other gates and other users allowed to apply (§2.4).
  Each is named so the user checks it belongs in the new tier. Tier attachments re-select by
  themselves (§3.4) and are not listed.

Audit, same transaction: `environment.created` per environment (context `cloned_from`, `autodeploy`,
`autodeploy_off_reason`), the existing actions for each copied item, each with `cloned_from` (the id
of the item copied, or of the reference env for an attachment or a cloud integration):
`variable.created` (every copied env-level variable, secret references included — `scope: "env"`),
`variable_set.attached`, `hook.created`, `notification_target.created`, `cloud_integration.created`,
`dependency.created` (`via: "clone"`, `upstream_kind`); and one `environment.cloned` summary
(context: reference name, new name, stacks, `missing_secrets`, `not_copied`, `warnings`). Response: the
created environments (`id`, `stack_id`, `stack_name`, `name`, `tier`, `autodeploy`,
`autodeploy_off_reason`, `label_sets`: the sets its labels select, `{name, new}` — `new` when the
reference is not selected by it), the edges (`upstream_env_id`, `downstream_env_id`, `upstream_kind`),
`missing_secrets`, `to_configure`, `not_copied`, `warnings`.

### 21.3 Model

```sql
env_rollouts (
  id uuid PK, space_id FK spaces ON DELETE RESTRICT,
  name text,                             -- display name: the env_name rolled out, the single name
                                         --   of the explicit members, or "<n> environments"
  state enum('awaiting_approval','running','paused','succeeded','failed','canceled'),
  created_by_user_id FK users,
  approved_by_user_id FK users nullable, approved_at timestamptz nullable,
  approval_expires_at timestamptz nullable,
  pause_reason text nullable,            -- needs_confirmation | approval_expired |
                                         --   approver_lost_rights | member_changed_since_approval
  created_at, updated_at, finished_at nullable
)

env_rollout_members (
  rollout_id FK env_rollouts ON DELETE CASCADE,
  environment_id FK environments ON DELETE CASCADE,  -- see "Deleting a member" below
  level int,                             -- topological level within the members (Kahn), 0 = roots
  status enum('pending','planning','needs_confirmation','applying','done','failed','skipped','canceled'),
  run_id FK runs ON DELETE SET NULL nullable,  -- the member's current run
  active bool,                           -- true while the rollout is not terminal
  approved_tier text nullable,           -- what the last approval saw of the environment (§21.5):
  approved_protected bool nullable,      --   its tier, `protected`, and whether four-eyes applied
  approved_four_eyes bool nullable,      --   to it (§2.4) — set by every approval
  PRIMARY KEY (rollout_id, environment_id)
)

-- One active rollout per environment.
CREATE UNIQUE INDEX one_active_rollout_per_env ON env_rollout_members (environment_id) WHERE active;
```

`runs` gains `rollout_id uuid FK nullable` (`ON DELETE SET NULL`), and `triggered_by` gains the value
`rollout` (§3.5). `trigger_user_id` of a rollout run is the rollout's **creator**, so four-eyes (§2.4)
keeps its meaning on every member run. Rollout runs carry no `run_group_id` (§9.2).

**Deleting a member.** An environment that is a member of an **active** rollout is not deleted: the
environment (or stack) deletion answers **409 `Active rollout`** (problem+json carrying `rollout_id`)
— the rollout would otherwise lose a member, and its downstream members an upstream, silently;
cancel the rollout or let it end first. The check runs under the deletion's `FOR UPDATE` on the
environment rows (§3.2): a rollout created meanwhile either commits first and is seen, or fails on
the foreign key. A **finished** rollout's member row goes with its environment (`ON DELETE
CASCADE`): the rollout's history then lists the remaining members.

Like a run's, a rollout's state changes through **one function**, `advance_rollout(session, rollout,
…)` (and its `_set_state`), called in the same transaction as the event that moves it (the approval,
a member run's `transition()`, a retry, a cancel, the expiry task of §7.5); never an `UPDATE
env_rollouts SET state = …` anywhere else. Every such transaction first **locks the rollout's row**
(`SELECT … FOR UPDATE`) and re-reads its members: two member events at once (two parents of the same
member finishing, an apply and a cancel) are serialized, and the second sees what the first
committed.

**Lock order.** The rollout's row is always locked **before** a member run's row: `transition()` on
a run with a `rollout_id` takes the rollout's lock first (a worker event, a human confirm or discard,
worker-lost), as the approve, cancel and retry endpoints do (the rollout, then its runs), and the
gate locks the rollout before it judges anything (§21.5). A worker claim — which already holds its
environment's row (§7.2) — takes the rollout's lock with `NOWAIT`: on a rollout busy right now it
claims nothing and the worker polls again, and a lock it cannot get, a deadlock or a serialization
failure never surfaces as a 500 from the claim.

**`running` and `paused`.** A started rollout is `running` while nothing waits; it is `paused` while
something waits, with **one** `pause_reason` — the most severe of, in order:
- `approval_expired`, `approver_lost_rights`, `member_changed_since_approval` — the **gate is
  closed**: no member is started and none is confirmed under the approval; runs already planning or
  applying finish (an apply is never cut short), a plan stopping at `unconfirmed`. Only a
  re-approval (§21.6) reopens it — `approver_lost_rights` and `member_changed_since_approval` are
  kept until then even once their cause is gone (a right granted back, a flag reverted);
- `needs_confirmation` — a member's plan waits on a human; the gate stays **open**: the other
  branches go on (members start, creates-only plans are confirmed under the approval).
`paused → running` (nothing waits any more: the last waiting plan was confirmed or discarded, or a
re-approval reopened the gate with nothing else waiting) is audited `rollout.resumed` by the system,
context `{from, previous_reason}`; the human action behind it is audited on its own (`run.confirmed`,
`run.discarded`, `rollout.approved`). A change of reason while paused is audited `rollout.paused`
again, with the new and the previous reason, and notified (`rollout_paused`, §21.7).

### 21.4 Lifecycle

```
create ──▶ awaiting_approval ──approve──▶ running ⇄ paused ──▶ succeeded | failed
                 │                          │    ▲                              │
                 │                          │    └──────────── retry ───────────┘
                 └──────── cancel ──────────┴──▶ canceled
```

1. **Create** — `POST /api/v1/rollouts {space_id?, env_name | environment_ids}` (`writer`;
   `space_id` defaults to the bootstrap space, as for any create, §3.0). Members:
   the environments named `env_name` in the space (typically the output of a clone), or the explicit
   list (same space). Levels are computed from the edges **between members**; an upstream outside the
   members must already have outputs (or a mock) — the preview says which members will plan on mocks,
   and therefore pause (§21.5). 409 if a member already belongs to an active rollout. Audited
   `rollout.created`. The response is the **preview**: members by level, their tiers, the edges, and
   the members already known to pause (they will plan on a mock, or lack a secret); whether a plan
   changes or destroys something is only known once it is made.
2. **Approve** — `POST /api/v1/rollouts/{id}/approve` (§21.6). The rollout moves to `running` and its
   level-0 members are planned.
3. **Plan of a member** — a `tracked` run, `triggered_by=rollout`, `rollout_id` set, audited
   `run.triggered` by the system (context `via: rollout`, `rollout_id`, `environment_id`, `commit`).
   It plans the head of the member's branch — or, once the rollout has **pinned** a commit for that
   stack and branch (§21.6), exactly that commit (`commit_sha` set at trigger; a worker checking out
   anything else fails it, `commit_mismatch`, §7.3). Queued like any run: one active run per
   environment still holds (§3.5). A member is started only while the gate is open (§21.3): the
   approval must still cover it (approval valid, the approver's right, the member unchanged since
   the approval — §21.5); otherwise nothing starts and the rollout pauses with that reason.
4. **Outcome of a member's plan** (in the run's `transition()` transaction):
   - empty diff → `finished`; the member is `done` (its existing outputs feed its downstream);
   - the plan passes the **rollout gate** (§21.5) → `planning|checking → confirmed` by the system,
     on the approval (§21.6) — the same transition as an autodeploy (§4.2), with the gate as its
     condition; the member is `applying` once claimed;
   - otherwise → the run stops at `unconfirmed` like any plan; the member is `needs_confirmation`
     and the rollout `paused` — `needs_confirmation` when the plan itself needs a human (the other
     branches go on), or the gate-closing reason when only the approval failed to cover it. A human
     confirms (ordinary `can_apply` + four-eyes) or discards the run.
5. **Apply finished** → the member is `done`, its outputs captured (§9.1). Each downstream member
   whose **every** upstream member is `done` is then planned — multi-parent gating.
6. **Failure** — a member run `failed`, `discarded` or `canceled` → the member is `failed` and every
   member reachable from it is `skipped`; independent branches continue. When nothing is left to
   run: `succeeded` if every member is `done`, else `failed` (the members say which part failed).
7. **Retry** — `POST /api/v1/rollouts/{id}/retry` (`writer`) on a `failed` rollout: its failed members
   go back to `pending` and their skipped descendants too; it runs again only under a valid approval
   that still covers them (§21.6), otherwise it waits in `paused` (`approval_expired`, …). A retried
   member plans the commit the rollout pinned (§21.6). Audited `rollout.retried`.
8. **Cancel** — `POST /api/v1/rollouts/{id}/cancel` (`writer`): no new member run starts; the
   members' `queued`, `unconfirmed` and `confirmed`-but-unclaimed runs are canceled through
   `transition()` (§4.1); a run a worker is executing finishes on its own (§4.1) but triggers
   nothing — neither in the rollout nor through the generic cascade; one stopping at `unconfirmed`
   is never confirmed under the approval (a human may still confirm or discard it). Audited
   `rollout.canceled`.
9. **Approval expiry** — evaluated whenever the rollout moves, and by a periodic task (§7.5) that
   advances every started rollout whose approval has expired: it pauses (`approval_expired`) within
   a scheduler tick, not only at its next event.

**The generic cascade.** A rollout run never triggers the generic cascade (§9.2), the rollout being
active or not — even a member run finishing after a cancel. A member of an active rollout gets no
cascade run from an upstream outside the rollout: the rollout plans it in its turn (fresh outputs
at claim); a member that already planned is reported (`rollout_upstream_changed`, §21.7) instead.
A member's downstreams outside the rollout are not triggered by its apply: they show as stale
(§9.6) — include them in the rollout, or trigger them.

### 21.5 The rollout gate (auto-confirmation)

A member's plan is confirmed by the system **only if all** of the following hold; otherwise it
stops at `unconfirmed` for a human. The gate first **locks the rollout** (§21.3), so a cancel or a
re-approval committed meanwhile is seen.

- the rollout is started (`running` or `paused`), its gate open (no gate-closing `pause_reason`,
  §21.3) and its approval valid: `now < approval_expires_at`; the run is the member's current run;
- the plan **only creates**: `plan_summary.add > 0` and **every other counter is 0** — `change`,
  `destroy` (a replacement counts as a destroy), `import` (adopting existing resources is a human
  decision), `forget` (a resource dropped from the state), and any counter a newer tool reports
  (Terraform's `action_invocation`: an action is no creation). Fail closed: a summary **missing**
  one of `add`, `change`, `destroy`, `import`, `forget` (an older agent), or holding a counter that
  is no integer, fails the gate (§3.5, §7.3);
- the run is `tracked` (never `destroy`), `used_mocks = false` (§9.3), `used_secret_fallback =
  false` (§15.5), and has **no `warn` or `fail` check** (§8.3);
- the environment **has never been applied**: no stored state version (§11), no run of it that
  ever reached `applying` (or a command run that reached `running`, §4.3), and a managed state
  (with `managed_state=false` nobody can tell: fail closed). A member with existing infrastructure
  always stops for a human: the approval stands up new environments, it does not change live ones;
- the plan is on the **commit the rollout pinned** for the member's stack and branch (§21.6): the
  run's `commit_sha` is a full SHA, equal to that of the rollout's first member run of that stack
  and branch that knew its commit;
- the approval still **covers** the member at that moment (otherwise the rollout pauses with the
  reason, closing the gate):
  - the approver still passes `can_apply(approver, env)` (§2.4: role and tier; a revoked right or a
    removed membership is not inherited by the rollout — a non-admin approver without a membership
    in the space fails, never falling back to their instance defaults) → else `approver_lost_rights`;
  - the environment's tier, `protected` and four-eyes requirement (§2.4: the tier's flag — a missing
    tier row requiring it —, `protected`, `require_second_pair_of_eyes`) are **those the approval
    recorded** (§21.3), and four-eyes, if it applies, holds between the approver and the creator →
    else `member_changed_since_approval`. A writer who protects a member, or moves it to another
    tier, after a self-approval does not get its plans applied on that approval.

A member plan that fails the gate is **never** confirmed by the environment's own `autodeploy`
either: it stops at `unconfirmed`.

**`protected` environments.** `protected` forbids *autodeploy* — applying with nobody having
decided (§2.4, §3.2, §13). A rollout's approval is a named decision, for that environment, under
four-eyes (§21.6): it **counts as the confirmation** of a protected member's plan when, and only
when, the plan only creates, in an environment never applied, on the pinned commit, and the
environment is still what the approver saw. Anything else — a plan that changes, destroys, imports,
forgets or warns; an environment with infrastructure — stops for a human, as on any protected env.

The auto-confirmation goes through `transition(run, confirmed, actor=system, …)` with
`confirmed_by_user_id = approved_by_user_id`, audited `run.confirmed` with context
`{via: "rollout", rollout_id, approved_by, approved_by_email}` — the apply's "who" (§6.2) is the
approver, who never saw this very plan: `GET /api/v1/runs/{id}` says `confirmed_via: "rollout"`,
and the run page, the audit page and the CSV export (its `context` column, §6.4) all show the
confirmation as **made under the rollout approval of** the approver, never as a plain confirm. The
apply claim re-checks the mock and secret-fallback gates as for any run (§4.2).

### 21.6 Approval

`POST /api/v1/rollouts/{id}/approve` on `awaiting_approval`, or — a **re-approval** — on a started
rollout whose gate is closed (`approval_expired`, `approver_lost_rights`,
`member_changed_since_approval`) or whose approval has expired (the response's `approvable`, §21.7);
409 otherwise:

- the caller passes `can_apply(caller, env)` for **every** member (all or nothing: **403** listing
  the environments it may not apply);
- **four-eyes**: if any member's tier requires it (a missing tier row counts as requiring it), or
  any member is `protected` or sets `require_second_pair_of_eyes`, the approver ≠ the rollout's
  creator — **403** otherwise, whatever the state (a re-approval included) — the same rule a confirm
  applies to a run's triggerer (§2.4), which also applies to a member run's human confirmation
  (`triggered_by=rollout`, triggerer = the creator), and which the gate re-checks at each
  auto-confirmation (§21.5);
- records, per member, what it saw: `approved_tier`, `approved_protected`, `approved_four_eyes`
  (§21.3);
- sets `approved_by_user_id`, `approved_at`, `approval_expires_at = now +
  stackd_rollout_approval_ttl_seconds` (default 24 h): an approval does not hang around for days;
- on a re-approval, the gate runs again on every member whose run waits at `unconfirmed`: one
  whose plan passes it (§21.5) — it paused only on the approval — is confirmed by the system
  (`unconfirmed → confirmed`, the one system-actor path of that transition, §4.2);
- audited `rollout.approved` with, per member environment, its tier, `protected` and four-eyes, the
  expiry and whether it is a re-approval.

**What an approval binds.** This rollout's members, as the approval saw them (tier, protection,
four-eyes), the gate of §21.5 — creates-only plans in never-applied environments — and, per stack
(and branch), **one commit**: the first member run of a stack in the rollout plans the branch head
and its commit becomes the rollout's **pin** for that stack and branch; every later member run
there — another member of that stack, a retry — is triggered on that commit (§21.4 step 3), and
the gate refuses a plan on any other one (a member run started before the pin was known). A
commit pushed after the approval therefore reaches a stack only if that stack's first member run
had not started yet; the gate still judges that plan, and the approval never confirms a change to
existing infrastructure.

### 21.7 API, live updates, notifications

```
POST /api/v1/spaces/{space_id}/environment-clones                      # §21.2
POST /api/v1/rollouts            { space_id?, env_name | environment_ids }  # → the detail (preview)
GET  /api/v1/rollouts ?space_id=&state=        # summaries: state, members, done
GET  /api/v1/rollouts/{id}                     # detail
POST /api/v1/rollouts/{id}/approve | /retry | /cancel   # → the detail
WS   sub: rollout:{id}                         # membership-gated; signal {kind: rollout_event, rollout_id}
```

The detail: the rollout (state, `pause_reason`, creator, approver, approval expiry) and per member:
stack/env names, tier, `protected`, level, status, run id and state, plan summary, `auto_confirmed`
(its run was confirmed by the system), `waiting_reason` (why a `needs_confirmation` member's plan
stopped: changes, destroys, imports, forgets, another kind of action, a mock, a fallback, a check,
existing infrastructure, a commit other than the pin; else the approval no longer covering it —
expired, the approver's right, a member changed since the approval; or, for a queued run, that no
online worker can take it — labels or tool version, §7.2/§7.4), `expected_mocks` (outputs an
upstream outside the rollout has not published, a mock standing in) — plus the edges between members,
`can_approve {allowed, reason}` for the viewer, `approvable` (an approval is what the rollout waits
for: its first, or a re-approval, §21.6) and `approval_ttl_seconds` (the approval dialog states
the real duration). Creating needs `writer` in the space; approving, `can_apply` on every member;
retry and cancel, `writer`.

WS events invalidate the rollout's queries (DESIGN §6), like run events. Notifications (§17.1), to
the rollout's creator and approver, context `{rollout_id, name, …}` (no `run_id`); the inbox links
them to the rollout page:
- `rollout_paused` — on every pause and change of pause reason (context `reason`);
- `rollout_succeeded`, `rollout_failed`;
- `rollout_upstream_changed` — an upstream outside the rollout applied while a member that already
  planned waits or applies on the previous outputs (context `environment_id`, `upstream_env_id`,
  `upstream_run_id`, §21.4).

### 21.8 Audit taxonomy additions (extends §6.2)

The exhaustive list (each a `target_kind=rollout` event, except `environment.cloned` — the space —
and the run ones — the run); a clone's per-item events are the existing actions (§21.2, §6.2):

```
environment.cloned          # §21.2 summary (target: the space)
rollout.created             # user; context: name, environments
rollout.approved            # user; context: environments {id: {tier, protected, four_eyes}},
                            #   expires_at, re_approval
rollout.paused              # system; context: reason, previous_reason, from — on every pause and
                            #   change of reason
rollout.resumed             # system; paused → running; context: from, previous_reason
rollout.retried             # user
rollout.canceled            # user
rollout.succeeded / rollout.failed                    # system
run.triggered               # system, target the member run; context via=rollout, rollout_id,
                            #   environment_id, commit
run.confirmed               # system, target the member run; context via=rollout, rollout_id,
                            #   approved_by, approved_by_email (§21.5)
run.canceled                # user (the canceller), target each member run a cancel stops;
                            #   context via=rollout, rollout_id
```

### 21.9 Invariants preserved

- **`transition()` only** (CLAUDE §4.1): member runs, their auto-confirmation and their cancellation go
  through it; rollout states through `advance_rollout()`.
- **Same-transaction audit** (§6.3) for every clone item, rollout state change and auto-confirmation.
- **Secrets**: a clone never copies a literal sensitive value; nothing is returned in clear.
- **`can_apply`**: checked for every member at approval and again at each auto-confirmation; a
  destroy is never auto-confirmed; four-eyes holds between creator and approver, at the approval
  and at each auto-confirmation; a member whose tier, protection or four-eyes changed since the
  approval is not auto-confirmed.
- **No change to live infrastructure without a human on the plan**: the approval only ever confirms
  creates-only plans in environments never applied, on the rollout's pinned commit per stack.
- **One active run per environment** (§3.5) and **one active rollout per environment**.
- **Mocks** (§9.3): a member that consumed one never auto-confirms; its apply stays gated by
  `allow_mock_apply`, which a clone never turns on.
- **Sensitive outputs** are never propagated (§9.1) — unchanged.
