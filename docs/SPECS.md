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
max_apply_tier enum(dev|staging|prod) nullable,  -- max tier where the user can confirm an apply (§2.4)
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

The "apply everywhere except prod" need depends on the targeted environment, not just on the user. We express it through a **tier** on the environment and a **cap** on the user, rather than through a full policy system (sufficient for the vast majority of orgs; per-space RBAC remains Phase 7).

- `environments.tier` enum(`dev`|`staging`|`prod`) — implicit ordering `dev < staging < prod`.
- `users.max_apply_tier` — maximum tier where the user can **confirm an apply**. NULL = none (can read and plan, never apply).
- Apply rule: `confirm` allowed iff `role ∈ {approver, admin}` **AND** `max_apply_tier >= env.tier`. Example: Bob `max_apply_tier=staging` confirms in dev/staging, denied in prod; Alice `prod` confirms everywhere.
- `users.can_destroy` — a run `type=destroy` (trigger AND confirm) requires `can_destroy=true` **in addition** to the tier rule. A destruction is more dangerous than an apply: a distinct, explicit right.

**Relationship with `protected`**: we **decouple** sensitivity and access right, which were mixed until now. `environments.protected` now carries only its own effects — forcing confirmation (no autodeploy) and enabling 4-eyes; *who* can confirm now comes from the tier. Consequence: an env can be `tier=prod` without being `protected` (restricted apply but autodeploy possible for the authorized) and vice versa.

**4-eyes / tier consistency**: for `tier=prod` environments, self-confirmation is forbidden by default (the triggerer ≠ the confirmer), whether `require_second_pair_of_eyes` is checked or not — the rule follows from the tier instead of being a flag to maintain everywhere.

**Scope of 4-eyes**: the "triggerer ≠ confirmer" rule only bites on runs with a **human** triggerer (`triggered_by=manual`, `trigger_user_id` set). A run with no human at its origin (`webhook`, `dependency`) has no `trigger_user_id`: any confirmer authorized at the tier can confirm it (there is no one to oppose). This is not a bypass — the access guard remains the tier (`can_apply`); 4-eyes only prevents *the same person* from triggering **and** confirming.

**Double-lock boundary**: when workload OIDC is active (§10), the prod apply restriction must **also** live in the AWS trust policy (`sub` claim filtered on `run:prod:*:apply`), not only in Stackd — otherwise bypassing the API bypasses the control. Both layers express the same rule; the `tier` feeds the token's `sub` (§10.2).

**Assumed limitation**: the tier is linear (nested rights: whoever can prod can do anything). An env "sensitive but not prod" (customer sandbox, compliance) is not expressed cleanly and would then justify explicit per-env permissions — out of MVP scope.

Per-env option `require_second_pair_of_eyes`: the triggerer cannot confirm (redundant with the prod tier rule, useful for staging).

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

First level of the hierarchy (breadcrumb `space / stack / env / run`, DESIGN §4) and parent of `stacks`, `variable_sets`, `worker_pools`. **Per-space RBAC** is pushed to Phase 7; in the MVP the table exists and the bootstrap creates a `default` space to which everything is attached. No **exposed multi-space CRUD** nor Google group mapping before RBAC (seed/dev may insert other spaces directly — e.g. `demo` in DEV §7 — this is not the public API). All `space_id` FKs point to a space from Phase 1 — the entity is not optional, only its CRUD is.

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
tier enum(dev|staging|prod),       -- §2.4: carries the apply/destroy permissions
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
  created_at, updated_at
)

variable_set_attachments (
  id uuid PK,
  variable_set_id FK,
  target_kind enum('stack','environment'),
  target_id uuid,                   -- stack → all its envs; environment → this env only
                                    -- polymorphic (stack OR env) → no DB FK; integrity is app-enforced
  priority int default 0,           -- orders the sets among themselves at resolution
  UNIQUE (variable_set_id, target_kind, target_id)
)
```

**Final resolution of a variable at claim time** (from weakest to strongest):

```
1. variable sets auto_attach            (by increasing priority)
2. variable sets attached to the stack  (by increasing priority)
3. variable sets attached to the env    (by increasing priority)
4. stack variables (environment_id NULL)
5. environment variables                ← always wins
```

At equal name and kind, the upper layer overrides. Two sources outside resolution are added at claim time: upstream outputs (`dependency:`) and mocks (`mock`) — see §9. The snapshot of **provenances** (`{"TF_VAR_region": "set:common-aws", "TF_VAR_cidr": "env", "TF_VAR_vpc_id": "dependency:network/prod", "TF_VAR_nlb_dns": "mock"}`) is frozen in `runs.variable_provenance` for audit and the UI badge (DESIGN.md §5.2: "inherited from…", "overridden here", `MOCK`). Deleting an attached set → 409 with the list of attachments (explicit detachment required).

### 3.5 `runs`

```
id uuid PK, environment_id FK,
type enum(tracked|proposed|destroy), state enum (§4),
commit_sha, commit_message, commit_author,
triggered_by enum(manual|webhook|dependency|api), trigger_user_id nullable,  -- 'api' reserved: no
                                     -- application tokens/PAT at MVP (auth = human Google + worker),
                                     -- the value is set when programmatic triggering arrives (Phase 7)
confirmed_by_user_id nullable,       -- who approved the apply (core of the audit)
parent_run_id nullable, run_group_id nullable, worker_id nullable,
plan_summary jsonb,                  -- {add, change, destroy, resources}
check_results jsonb,                 -- results of the after_plan hooks (§8)
resolved_inputs jsonb,               -- injected upstream outputs (non-sensitive)
used_mocks bool default false,       -- §9.3: at least one mock consumed
variable_provenance jsonb,           -- §3.4: provenance of each resolved variable
claimed_at, confirmed_at, finished_at, error nullable, created_at
```

Concurrency — 1 active run per environment:

```sql
CREATE UNIQUE INDEX one_active_run_per_env ON runs (environment_id)
WHERE state IN ('preparing','planning','checking','unconfirmed','confirmed','applying');
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

`offline` if heartbeat > 60 s; run in progress on an offline worker → `failed (worker_lost)` after 120 s. Targeting by labels (dedicated prod pool recommended).

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
                  │ planning │ hooks before_plan, plan -out + show -json
                  └────┬─────┘
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
         │     │confirmed │ (resumed by the same worker if possible)
         │     └────┬─────┘
         │     ┌────▼─────┐
         │     │ applying │ hooks before_apply, apply, output -json, hooks after_apply
         │     └────┬─────┘
         ▼          ▼
      ┌──────────────────┐
      │     finished     │──▶ scheduler hooks: capture outputs, cascade, audit
      └──────────────────┘
```

Terminal: `finished`, `failed`, `discarded`, `canceled`. `canceled`: user on `queued`/`unconfirmed`, or signal to the worker via heartbeat → SIGINT.

### 4.2 Transition rules

| Transition | Actor | Conditions |
|---|---|---|
| `queued → preparing` | worker (claim) | no active run on the env, env not locked, compatible labels |
| `planning → checking` | worker | plan OK **and ≥ 1 after_plan hook**. Without an after_plan hook, `checking` is skipped: the transitions `planning → unconfirmed / confirmed / finished` exist, with exactly the same conditions as their equivalents from `checking` |
| `checking → unconfirmed` | worker | checks OK or warn; non-empty diff. A warn **forces** confirmation even if autodeploy |
| `checking → confirmed` | system | all checks OK, non-empty diff, `autodeploy=true`, env not protected, `used_mocks=false` |
| `planning/checking → finished` | worker | empty diff (outputs captured after refresh) |
| `unconfirmed → confirmed` | user | `can_apply(user, env)` = role∈{approver,admin} AND `max_apply_tier >= env.tier` (§2.4); ≠ triggerer if tier=prod or 4-eyes; for a `destroy` run: `can_destroy` required; **blocked if `used_mocks` and `allow_mock_apply=false`** |
| `confirmed → applying` | worker | resume of the workspace (TTL 24 h), otherwise re-plan |
| `applying → finished` | worker | apply exit 0 + outputs uploaded |
| `* → failed` | worker/system | exit ≠ 0, hook `fail`, timeouts (prepare 10 / plan 30 / apply 60 min) |

Single function `transition(run, to_state, actor, payload)`: legality, atomic update guarded on `from_state`, `run_event`, audit event if the action is human or terminal, WS publication, scheduler hooks. The guarded UPDATE uses `RETURNING` (PG18: old + new tuple) to produce the `from→to` `run_event` without a re-read, in the same transaction.

### 4.3 Command runs

A `RunType.command` run executes **one allowlisted tofu/terraform subcommand** (`import`, `state list/show/rm/mv`, `taint`, `untaint`, `output`, `show`, `validate`, `providers`, `refresh`) instead of the plan→apply flow — for state surgery and adoption that `plan`/`apply` can't express. It is **not** arbitrary shell: the worker runs `<tool> <command> <args>` with the command taken verbatim from the allowlist. Lifecycle: `queued → preparing → running → finished | failed`. The subcommand + args live in `runs.command` (`{name, args}`). Endpoint: `POST /api/v1/environments/{env_id}/commands`. Permissions: read-only commands need `writer`; **mutating** commands (import, state rm/mv, taint, untaint, refresh) require `can_apply(user, env)` — the same gate as an apply. The worker receives the **apply** OIDC role for mutating commands and the (read-only) **plan** role otherwise; the trigger is audited (`run.command_triggered`) and so is completion (`run.command_executed`). `force-unlock` is **not** a command — it has its own endpoint (`DELETE …/state/lock`, §11.2).

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
- **Residual leak via `plan.json`**: an `after_plan` hook (infracost, jq, script) reads `plan.json`, which contains the variable values. Terraform marks `sensitive` the values it knows as such, but a hook that dumps the raw JSON can re-print a sensitive value to its stdout (and thus to the logs). Mitigations: (a) the value-based masking above also applies to hook stdout; (b) a short or transformed secret (base64, substring) escapes value-based masking — a **documented** limit, do not put exploitable secrets in non-sensitive `tfvars`. The check tools (tfsec/checkov/infracost) do not print values by default.
- ANSI sequences preserved (color rendering on the front side).

### 5.2 Two-tier storage

| Tier | Where | When | Usage |
|---|---|---|---|
| Hot | table `run_logs` | during the run + 7 d | live + recent |
| Cold | S3 `logs/{run_id}/{phase}.log.gz` | async archiving at end of run | history (1 year then lifecycle) |

`GET /runs/{id}/logs?phase=&after_seq=` serves one or the other transparently. `GET /runs/{id}/logs/download?phase=all`.

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

### 6.2 Taxonomy (MVP)

```
auth.login / auth.logout / auth.domain_denied / auth.refresh_reuse_detected
stack.* / environment.* (created|updated|deleted)
variable.* (created|updated|deleted)          # context: name, sensitive — NEVER the value
variable_set.* (created|updated|deleted|attached|detached)
hook.* (created|updated|deleted)
run.triggered / run.confirmed ⭐ / run.discarded / run.canceled
run.applied ⭐ / run.apply_failed / run.destroy_triggered
run.check_failed / run.check_warned           # results of the after_plan hooks
state.force_unlocked / state.version_downloaded / state.deleted
dependency.created / dependency.deleted / dependency.mock_consumed
cloud_integration.created / cloud_integration.updated / cloud_integration.deleted
worker_pool.created / worker_pool.token_rotated / worker_pool.deleted
worker.diagnostics_requested                  # read-only debug bundle (cf. §observability)
hook.* (created|updated|deleted)
user.role_changed / user.apply_tier_changed / user.destroy_permission_changed / user.disabled
```

`run.confirmed` + `run.applied` = the answer to "who applied what": Google identity of the confirmer, env, commit, plan summary, assumed IAM role (if OIDC), link to the logs.

### 6.3 Assumed double write

`run_events` = the fine-grained mechanics of the state machine. `audit_events` = the denormalized business journal. The audit event is written **in the same DB transaction** as the action — no bus, no loss.

### 6.4 API and UI

```
GET /api/v1/audit?actor=&action=&target_kind=&target_id=&stack=&environment=&from=&to=
GET /api/v1/audit/export?format=csv          (admin)
```

/audit page (filterable global journal), Activity tab per env (recent applies: who/when/commit/summary/logs), per-user view.

---

## 7. Worker protocol

### 7.1 Registration and heartbeat

```
POST /worker/v1/register   (Bearer pool_token) → worker_id + worker_token
POST /worker/v1/heartbeat  (20 s) → { "commands": [{"type":"cancel_job", ...}] }
```

Heartbeat = downstream command channel. No incoming connection.

### 7.2 Claim (long-poll)

```
POST /worker/v1/jobs/claim?wait=25
→ 204 if nothing
→ 200 {
    "job_id": "...",
    "phase": "plan" | "apply",   # job execution type — do not confuse with the
                                 # fine-grained phases of the run (§4) or of the logs (§5): a
                                 # "plan" job covers preparing+planning+checking
    "environment": { "id", "name": "prod", "stack_name": "core-network",
      "repo_url", "commit_sha", "project_root", "tool", "tool_version" },
    "repo_credentials": { "kind": "token", "token": "<decrypted, memory TTL>" },
    "env": { ... },                        # resolution sets→stack→env (§3.4)
    "sensitive_env": { ... },              # never logged
    "tfvars_json": { ... },
    "hooks": {                             # §8: merge platform + .stackd.yml
      "after_plan": [ {"name": "infracost", "command": "...", "on_failure": "warn",
                       "source": "platform"} ]
    },
    "backend": { "type": "http", "address": ".../state/v1/<env_id>",
                 "lock_address", "unlock_address",
                 "username": "env", "password": "<state_token scoped+TTL>" },
    "cloud_credentials": {                 # §10, if cloud_integration configured
      "provider": "aws",
      "oidc_token": "<signed workload JWT>",
      "role_arn": "arn:aws:iam::123:role/stackd-prod-plan",   # role of THE phase
      "region": "eu-west-1"
    },
    "resolved_inputs": { "TF_VAR_vpc_id": "vpc-0abc..." },
    "mock_inputs": { "TF_VAR_nlb_dns": "mock.example.internal" }   # §9.3
  }
```

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

Apply affinity: a `confirmed` run is reserved for its originating worker for **60 s** (`AND (r.worker_id = :wid OR r.confirmed_at < now() - interval '60 seconds')` on confirmed runs). Past that delay (dead or saturated worker), any compatible worker takes it and does an **automatic re-plan** before the apply (workspace absent → §4.2).

### 7.3 Events and artifacts

```
POST /worker/v1/jobs/{id}/events
  { "event": "phase_started"|"phase_finished"|"job_failed",
    "phase": "...", "exit_code": 0,
    "result": { "has_changes": true, "summary": {...},
                "checks": [{"name":"infracost","status":"warn","detail":"..."}] } }

PUT /worker/v1/jobs/{id}/artifacts/plan.tfplan | plan.json | outputs.json
```

### 7.4 Course of a job on the agent side (pseudo-code)

```python
job = claim()
ws = Workspace(job.job_id)
ws.git_clone(job.environment.repo_url, job.environment.commit_sha, depth=1)
tf = ensure_tool(job.environment.tool, job.environment.tool_version)  # verifies the checksum
                                       # SHA-256 (and the cosign/GPG signature if available) of the
                                       # downloaded binary against a pinned list — refuse otherwise (supply-chain)

if job.cloud_credentials:                    # §10 OIDC workload
    token_file = ws.write_secret("oidc_token", job.cloud_credentials.oidc_token)
    extra_env = { "AWS_WEB_IDENTITY_TOKEN_FILE": token_file,
                  "AWS_ROLE_ARN": job.cloud_credentials.role_arn,
                  "AWS_ROLE_SESSION_NAME": f"stackd-{job.job_id}" }

hooks = merge_hooks(job.hooks, ws.load_stackd_yml())   # platform first, §8

if job.phase == "plan":
    write_backend_override(ws, job.backend)
    write_tfvars(ws, job.tfvars_json, job.mock_inputs)
    run_hooks(hooks.before_init); run(tf, "init", "-input=false"); run_hooks(hooks.after_init)
    run_hooks(hooks.before_plan)
    # plan/apply run with -json: the agent streams each event's human @message to the log (masked)
    # AND collects the structured events — the `change_summary` event gives the authoritative
    # add/change/destroy counts and `diagnostic` events surface the real error as the run's `error`.
    code, events = run_json(tf, "plan", "-json", "-out=plan.tfplan", "-detailed-exitcode")
    # 0 = no changes, 2 = changes, 1 = error
    plan_json = run(tf, "show", "-json", "plan.tfplan")   # still produced for after_plan hooks
    upload_artifacts(plan_json, "plan.tfplan")
    checks = run_hooks(hooks.after_plan, expose="plan.json")   # checking phase
    report(phase_finished, has_changes=(code == 2), summary=..., checks=checks)
    if code == 2: keep_workspace(ttl="24h")   # the apply (auto-confirmed OR confirmed
                                              # manually) reuses this workspace

elif job.phase == "apply":
    ws = restore_workspace(job.job_id)       # or re-plan if absent
    run_hooks(hooks.before_apply)
    run(tf, "apply", "-input=false", "plan.tfplan")
    outputs = run(tf, "output", "-json")
    upload_artifacts(outputs, "outputs.json")
    run_hooks(hooks.after_apply)
    report(phase_finished)

ws.cleanup()
```

`docker` runner: each command (terraform AND hooks) runs in `stackd/runner:<tool>-<version>` (image including the common check tools: tfsec, checkov, infracost, jq).

### 7.5 Periodic tasks (internal scheduler, multi-replica)

The scheduler module (PLAN §2.1) carries background tasks that must run **exactly once** even with several API replicas:

| Task | Frequency | Effect |
|---|---|---|
| `worker_lost` detection | 30 s | heartbeat > 60 s → `offline`; active run on a worker offline for > 120 s → `failed (worker_lost)` (§3.7) |
| Git staleness polling | 15 min | `git ls-remote` per (repo, branch), dedup by repo → update `head_sha` (§9.6) |
| Cold log archiving | end of run | `run_logs` → S3 gz, purge hot > 7 d (§5.2) |
| Refresh token / audit purge | daily | expired families (§2.5), audit > 2 years (§6.1, audited purge) |

**Single-execution guarantee**: each task takes a dedicated **PG advisory lock** (`pg_try_advisory_lock(<task_key>)`) before running; a replica that does not obtain the lock skips its tick. No permanently elected leader (no external dependency), no double execution. The tasks are **idempotent** anyway (re-running a staleness computation or an archive breaks nothing) — the advisory lock mainly avoids redundant work and races on the `worker_lost` transitions.

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
- **Cloud credentials (§10) not exported to repo hooks.** The `AWS_WEB_IDENTITY_TOKEN_FILE` / `AWS_ROLE_ARN` variables are injected only into the environment of **terraform** invocations, never into that of **repo** hooks by default (same reasons as `sensitive_env`: a `.stackd.yml` pushed via PR must not be able to assume the prod apply role and exfiltrate). Opt-in per env if a hook legitimately needs the cloud. **Platform** hooks (non-bypassable) have access to it.
- **Repo hooks at the `*_apply` stages on tier=prod**: forbidden by default. On a `tier=prod` env, only **platform** hooks execute at `before_apply`/`after_apply`; a repo hook at these stages is ignored with a visible warning (it would run with the prod write role). The `*_init`/`*_plan` repo stages remain allowed (plan role = ReadOnly).
- `plan.json` available for reading at the `after_plan` stage and beyond.
- Timeout per hook: 10 min (configurable). Logs in a dedicated section of the viewer.
- **`on_failure: fail`** → run `failed`, audit `run.check_failed`.
- **`on_failure: warn`** → the run continues but mandatorily goes through `unconfirmed` (even with autodeploy), warning badge + detail on the run page, audit `run.check_warned`. A human takes responsibility.
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

`POST /environments/{id}/runs?with_downstream=true`: subgraph (BFS), topological sort (Kahn), run group, root launched, subsequent levels via cascade. UI: graph colored by state (see DESIGN.md §5.4).

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
stale            = head_sha present AND head_sha ≠ last_applied_sha
```

**Updating `head_sha`** (from most precise to fallback):
1. **Push webhook** (Phase 5): immediate update of all envs tracking the branch — even if the webhook does not trigger a run (`project_root` filtering), it always updates `head_sha`.
2. **Fallback polling**: `git ls-remote` per (repo, branch) every 15 min (periodic task, deduplicated by repo) — covers absent or lost webhooks.
3. **Manual**: `POST /api/v1/environments/{id}/refresh-head`.

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

S3 for the bytes, HTTP for the interface: per-run scoped tokens without distributed AWS credentials, visible locking, audit, refusal of a regressive serial.

### 11.2 Endpoints

| Method | Endpoint | Behavior |
|---|---|---|
| `GET` | `/state/v1/{env_id}` | 200 + latest state, 404 otherwise |
| `POST` | `/state/v1/{env_id}?ID=<lock_id>` | verifies lock, refuses regressive serial (409), upload S3, `state_version` linked to the run, audit |
| `LOCK` | `/state/v1/{env_id}/lock` | 200 or **423** + holder |
| `UNLOCK` | `/state/v1/{env_id}/lock` | requires `rw` scope (a `ro` proposed-run token never locks); verifies lock_id |
| `DELETE` | `/state/v1/{env_id}` | admin, soft-delete, audited |

Auth: Basic, password = JWT scoped `{env_id, run_id, scope, exp}`. Scope `ro` for proposed runs.

### 11.3 S3 layout

```
s3://stackd-<org>/
  states/{environment_id}/{version_uuid}.tfstate    # SSE-KMS, bucket versioning ON
  logs/{run_id}/{phase}.log.gz
  artifacts/{run_id}/plan.tfplan | plan.json | outputs.json
```

`managed_state: false` → nothing injected, the repo keeps its `backend "s3"` block (existing CarCutter compat).

### 11.4 Importing an existing stack

To adopt a stack that already has remote state into Stackd-managed state (`managed_state: true`):
`POST /api/v1/environments/{env_id}/state/import-session` (admin) mints a short-lived (30 min),
run-less `rw` state token and returns a ready-to-use `http` backend config. The operator migrates
their current state with one standard command — `tofu init -migrate-state -backend-config=...` —
which `LOCK`s, uploads the state via §11.2 (stored as a `state_version` with
`created_by_run_id = NULL`), then `UNLOCK`s. Audited as `state.import_session_created`; the address
uses the public URL (the operator runs locally). For stacks that should keep their own backend, use
`managed_state: false` (§11.3) instead — no migration needed.

---

## 12. REST API (main surface)

```
# Auth
GET  /api/v1/auth/google/start | /callback ; POST /auth/refresh | /logout ; GET /me

# OIDC issuer (workload)
GET  /.well-known/openid-configuration ; GET /oidc/jwks

# Stacks & environments
GET|POST /api/v1/stacks ; GET|PATCH|DELETE /api/v1/stacks/{id}
POST /api/v1/stacks/{id}/check-repo
GET|POST /api/v1/stacks/{id}/environments ; GET|PATCH|DELETE /api/v1/environments/{id}
POST /api/v1/environments/{id}/refresh-head        # Git staleness, §9.6
GET|POST|PATCH|DELETE /api/v1/stacks/{id}/variables[...]
GET|POST|PATCH|DELETE /api/v1/environments/{id}/variables[...]

# Variable sets
GET|POST /api/v1/variable-sets ; GET|PATCH|DELETE /api/v1/variable-sets/{id}
GET|POST|PATCH|DELETE /api/v1/variable-sets/{id}/variables[...]
GET|POST|DELETE /api/v1/variable-sets/{id}/attachments
GET /api/v1/environments/{id}/resolved-variables    # merged view + provenance

# Hooks (platform)
GET|POST|PATCH|DELETE /api/v1/stacks/{id}/hooks[...] | /api/v1/environments/{id}/hooks[...]

# Cloud integrations (OIDC)
GET|PUT|DELETE /api/v1/environments/{id}/cloud-integration
POST /api/v1/environments/{id}/cloud-integration/test    # verification AssumeRole

# Runs
POST /api/v1/environments/{id}/runs            { type?, with_downstream? }
GET  /api/v1/environments/{id}/runs ; GET /api/v1/runs/{id}
POST /api/v1/runs/{id}/confirm | /discard | /cancel
GET  /api/v1/runs/{id}/logs ?phase=&after_seq= ; GET /runs/{id}/logs/download
GET  /api/v1/runs/{id}/plan ; GET /api/v1/runs/{id}/checks
WS   /api/v1/ws                                sub: run:{id}, environment:{id}

# Dependencies
GET|POST /api/v1/environments/{id}/dependencies          # references with mock_value
POST /api/v1/stacks/{id}/dependencies/link-by-name
DELETE /api/v1/dependencies/{id}
GET /api/v1/environments/{id}/outputs ; GET /api/v1/graph ; GET /api/v1/run-groups/{id}

# Audit
GET /api/v1/audit ; GET /api/v1/audit/export

# Users & permissions (admin)
GET   /api/v1/users
PATCH /api/v1/users/{id}     # role, max_apply_tier, can_destroy, disabled (audited)

# Workers & execution queue
GET|POST|DELETE /api/v1/worker-pools ; GET /api/v1/workers
GET /api/v1/queue          # runs in progress + waiting, with computed blocking reason
                           # (active_run|env_locked|no_compatible_worker|apply_affinity_hold)
POST|GET /api/v1/workers/{id}/diagnostics   # admin: read-only debug bundle (via heartbeat)

# Observability & onboarding
GET /api/v1/health         # DB, workers (online + heartbeat), active/waiting runs, recent errors
GET /api/v1/logs           # admin: structured JSON buffer, filters level/event/worker_id/run_id/q
POST /api/v1/auth/me/onboarded              # marks the walkthrough seen (persisted server-side)

# State
GET /api/v1/environments/{id}/state/versions[...]
DELETE /api/v1/environments/{id}/state/lock              # force-unlock (admin, audited)

# Webhooks
POST /api/v1/webhooks/github                             # HMAC

# Worker API (agents — detail §7)
POST /worker/v1/register | /heartbeat | /jobs/claim
POST /worker/v1/jobs/{id}/events | /jobs/{id}/logs
PUT  /worker/v1/jobs/{id}/artifacts/{name}
POST /worker/v1/commands/{id}/result        # result of a downstream command (diagnostics…)

# State backend HTTP (Terraform — detail §11)
GET|POST|DELETE /state/v1/{env_id} ; LOCK|UNLOCK /state/v1/{env_id}/lock
```

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
| State | S3 SSE-KMS via API only; scoped tokens, RO for PR; audited locking |
| Apply permissions | tier per env × `max_apply_tier` cap per user (§2.4); distinct `can_destroy`; double lock with the OIDC trust policy on the tier |
| Protected envs | autodeploy forbidden, 4-eyes (auto if tier=prod), never bypassed (cascade included); the *right* to apply comes from the tier, not from `protected` |
| Proposed runs | plan-only, state RO, secrets not injected by default, mocks allowed |
| Webhooks | HMAC SHA-256 (secret per repo, `stacks.webhook_secret_encrypted` §3.1), anti-replay 5 min |
| Sessions | access JWT 15 min in Bearer header; refresh httpOnly `SameSite=Strict` + CSRF double-submit, rotation with reuse detection → family revocation (§2.5) |
| Encryption at rest | AES-256-GCM, random 96-bit nonce per value, never reused (§1) |
| Rate limiting | login, `/auth/refresh`, `/webhooks/*`, `/worker/v1/register`, claim: quotas per IP/identity (anti-bruteforce and anti-abuse). MVP: simple middleware; hardening Phase 7 |
| Audit | append-only **at the DB level** (INSERT-only role + anti-update/delete trigger §6.1), denormalized, transactional, 2 years |

---

## 14. Tests — minimal strategy

- **API unit tests**: transitions (`can_apply` per tier including apply-everywhere-except-prod, `can_destroy`, auto 4-eyes on prod tier **and its human-triggerer-only scope**, mock-apply blocking, warn → forced confirmation), 5-layer variable resolution + provenance, hook merging (platform→repo order, exclusion of `*_apply` repo hooks on prod), cycle detection, multi-parent cascade, mock resolution (real > mock > error), signature/claims of workload tokens (tier segment in the `sub`, wildcard limited to the stack segment), Google id_token validation, **refresh rotation + reuse detection → family revocation**.
- **Integration**: Postgres testcontainers; **concurrent claim → only one wins, the loser catches `23505` and re-polls**; HTTP backend with real `tofu` + Garage; log idempotency; **masking of sensitive values (`tfvars` + env) in a hook's stdout**; `after_plan` hook that reads plan.json; **WS fan-out via `LISTEN/NOTIFY`**; **single execution of a periodic task under advisory lock with 2 replicas**; AssumeRoleWithWebIdentity against a mock STS (moto).
- **E2E**: ephemeral compose + fixture repo + `local_file` provider → bootstrap of a 2-stack cascade **with mocks** (mocked plan → upstream apply → real cascade), assertions on the final state AND the audit events (triggered → checked → confirmed → applied, mock_consumed).
- **Agent**: exit codes, secret masking, workspace resume, cancellation, writing the OIDC token and exporting the env vars.

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
5-layer resolution, by calling the provider:

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
secret.unavailable            -- run failed because a reference couldn't resolve and no fallback applied
```

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
