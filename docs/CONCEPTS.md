# CONCEPTS.md — Stackd concepts, explained with examples

> A concept guide for everyone using or building Stackd. It teaches the **mental model** with
> concrete, copy-pasteable examples. For the exhaustive field-by-field truth, see **SPECS.md** —
> this document never contradicts it, it illustrates it.

All examples assume the local dev stack is up (`task dev`) with the API on `http://localhost:8000`
and dev login enabled. Authenticate once:

```bash
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/dev/login \
  -H 'content-type: application/json' -d '{"persona":"admin"}' | jq -r .access_token)
AUTH="Authorization: Bearer $TOKEN"
```

---

## 0. The one-sentence mental model

> Stackd orchestrates `plan → human confirmation → apply` of Terraform/OpenTofu on **pull-based
> workers**, where **the API is the single source of truth** and every state change is an
> auditable event.

Everything below is a refinement of that sentence.

---

## 1. Space — the container

A **space** is the top of the hierarchy: `space / stack / environment / run`. It owns stacks,
variable sets and worker pools. At MVP there is exactly one space, `default` (RBAC per space is a
later phase). You rarely think about it; it's the namespace everything hangs off.

```
default                      ← space
└── core-network             ← stack   (a repo + a folder)
    ├── dev                  ← environment (an instance with its own state)
    ├── staging
    └── prod
```

---

## 2. Stack vs Environment — template vs instance

This is the most important distinction in Stackd.

| | **Stack** | **Environment** |
|---|---|---|
| What it is | a *template*: a Git repo + a subfolder + which tool | an *instance*: one real deployment |
| Holds | `repo_url`, `project_root`, `tool`, `tool_version` | `tier`, `branch`, `state`, `variables`, protections |
| Analogy | a class | an object |
| Runs? | never | **a run always belongs to an environment** |

Why not Terraform workspaces? Workspaces share code, backend and credentials and only suffix the
state key — easy to target the wrong one. A Stackd environment is **physically isolated**: its own
state, variables, protections and worker pool.

**Example — one stack, three environments:**

```bash
# Stack = the template
STACK=$(curl -s -X POST localhost:8000/api/v1/stacks -H "$AUTH" -H 'content-type: application/json' \
  -d '{"name":"core-network","repo_url":"https://github.com/acme/infra",
       "project_root":"network","tool":"opentofu","tool_version":"1.12.0"}' | jq -r .id)

# Environments = instances of that template
curl -s -X POST localhost:8000/api/v1/stacks/$STACK/environments -H "$AUTH" -H 'content-type: application/json' \
  -d '{"name":"dev","tier":"dev","branch":"main"}'
curl -s -X POST localhost:8000/api/v1/stacks/$STACK/environments -H "$AUTH" -H 'content-type: application/json' \
  -d '{"name":"prod","tier":"prod","branch":"main","protected":true,"require_second_pair_of_eyes":true}'
```

`dev` and `prod` run the *same code* but have different state, variables, tier and protections.

---

## 3. Variables & variable sets — configuration in layers

Configuration is composed from **layers**. The same variable name can be defined at several layers;
the strongest layer wins. From weakest to strongest:

```
1. variable sets, auto_attach        (shared defaults for the whole space)
2. variable sets attached to a stack (by priority)
3. variable sets attached to an env  (by priority)
4. stack variables                   (common to all envs of the stack)
5. environment variables             ← always wins
```

A **variable set** is a named, reusable bundle (e.g. `common-aws`, `datadog`, `region-eu`) you
attach to many stacks/envs. `auto_attach: true` means "attach to every stack in the space".

**Worked example.** Suppose:

| Layer | defines |
|---|---|
| set `common-aws` (auto_attach) | `region = eu-west-1`, `tags = {team:core}` |
| set `region-us` (attached to env `prod-us`) | `region = us-east-1` |
| stack variable | `cidr = 10.0.0.0/16` |
| env variable on `prod-us` | `cidr = 10.9.0.0/16` |

Resolving variables for `prod-us`:

| Variable | Value | Provenance |
|---|---|---|
| `region` | `us-east-1` | `set:region-us` (env-attached set beats auto_attach) |
| `tags` | `{team:core}` | `set:common-aws` |
| `cidr` | `10.9.0.0/16` | `env` (env override beats the stack variable) |

See it for real:

```bash
curl -s localhost:8000/api/v1/environments/$ENV/resolved-variables -H "$AUTH" | jq
# [{ "name":"region","injected_name":"TF_VAR_region","value":"us-east-1","provenance":"set:region-us" }, ...]
```

Every resolved variable carries its **provenance** so the UI can show "inherited from `common-aws`"
or "overridden here". The provenance is also frozen onto the run (`variable_provenance`) for audit.

### Variable kinds

- `terraform` → injected as `TF_VAR_<name>` / written to a tfvars file.
- `environment` → injected as a process env var for the run.

### Sensitive variables are write-only

Mark a variable `sensitive: true` and it is **AES-256-GCM encrypted at rest** and **never returned
in clear** — the API masks it as `•••`, decrypting only to build the worker payload, and the agent
masks the literal value in logs.

```bash
curl -s -X POST localhost:8000/api/v1/stacks/$STACK/variables -H "$AUTH" -H 'content-type: application/json' \
  -d '{"kind":"environment","name":"DD_API_KEY","value":"super-secret","sensitive":true}'
# GET later → "value":"•••"   (the plaintext is never readable again through the API)
```

---

## 4. Runs & the state machine

A **run** is one execution against one environment. Its life is an explicit state machine — and
**the only way the state ever changes is the `transition()` function**, which writes a `run_event`,
an `audit_event` (for human/terminal actions) and a live WebSocket signal, all in one DB transaction.

```
queued → preparing → planning → [checking] → unconfirmed → confirmed → applying → finished
                                                  │ discard → discarded
   (terminal: finished · failed · discarded · canceled)
```

- `checking` only appears if there are `after_plan` hooks.
- `unconfirmed` = **waiting for a human** (the amber state — see DESIGN).
- If the plan has no changes, the run jumps straight to `finished`.

**Example — a manual plan that needs confirmation:**

```bash
# 1. trigger → run is created 'queued'
RUN=$(curl -s -X POST localhost:8000/api/v1/environments/$ENV/runs -H "$AUTH" -d '{}' | jq -r .id)

# 2. a worker claims it → preparing → planning, then reports a non-empty plan → 'unconfirmed'
# 3. a human confirms (subject to permissions, §5)
curl -s -X POST localhost:8000/api/v1/runs/$RUN/confirm -H "$AUTH"
# → confirmed → a worker claims the apply → applying → finished
```

**Concurrency invariant:** at most **one active run per environment** (enforced by a partial unique
index). Two environments of the same stack can run in parallel; two runs on the *same* env cannot.

---

## 5. Tiers, roles & permissions — who can apply what

Two orthogonal axes:

- **`role`** (global capability): `reader < writer < approver < admin`. *What* you can do in nature
  (read, manage config, confirm, administer).
- **`tier`** on the environment (a configurable catalog, e.g. `dev`/`staging`/`prod`/`qa`) ×
  **`allowed_tiers`** set on the user. *Where* you can apply.

> **Anyone (writer+) can trigger a plan on any environment** — a plan changes nothing. Only the
> **apply confirmation** is gated.

`can_apply(user, env)` = `role ∈ {approver, admin}` **AND** `env.tier ∈ user.allowed_tiers` (set
membership — tiers are not ordered, so a grant can be non-contiguous like `{dev, prod}`).

**Examples:**

| User | role | ceiling | dev | staging | prod |
|---|---|---|---|---|---|
| Bob | writer | staging | ❌ (not approver) | ❌ | ❌ |
| Carol | approver | staging | ✅ | ✅ | ❌ (tier prod required) |
| Alice | approver | prod | ✅ | ✅ | ✅ |

Extra rules:

- **Destroy** (`type=destroy`) additionally requires `can_destroy=true` — a separate, explicit right.
- **4-eyes**: on `tier=prod` (or when `require_second_pair_of_eyes` is set), the **triggerer cannot
  confirm their own run**. It only bites human-triggered runs — a webhook/cascade run has no human
  to oppose.
- **Mock block**: a run that consumed mock outputs cannot be applied unless `allow_mock_apply=true`
  (see §9).

When a confirm is refused, the API returns the reason in clear, e.g. `403 "tier prod required —
your ceiling is staging"`, which the UI shows on the disabled Confirm button.

**Double lock with the cloud:** for prod, the restriction also lives in the AWS trust policy (§10),
so bypassing the API still can't assume the prod apply role.

---

## 6. Hooks & checks — custom flow steps

Hooks run shell commands at lifecycle stages: `before_init`, `after_init`, `before_plan`,
`after_plan`, `before_apply`, `after_apply`. They come from **two sources, merged**:

| Source | Where | Editable by | Use |
|---|---|---|---|
| **Platform** | UI/API (per stack or env) | writer+ (audited) | governance — **not bypassable by a PR** |
| **Repo** | `.stackd.yml` in the repo | anyone who pushes | project-specific logic |

Order per stage: **platform (stack) → platform (env) → repo**. Security-critical checks belong on
the platform side.

**`.stackd.yml` example:**

```yaml
version: 1
hooks:
  after_plan:
    - name: infracost
      command: infracost breakdown --path plan.json --format table
      on_failure: warn          # run continues but MUST be confirmed by a human
    - name: no-prod-deletes
      command: jq -e '[.resource_changes[]|select(.change.actions|index("delete"))]|length==0' plan.json
      on_failure: fail          # run → failed
```

- `on_failure: fail` → the run fails (`run.check_failed`).
- `on_failure: warn` → the run continues but is **forced through `unconfirmed`** even if autodeploy
  was on — a human must own the warning.

Repo hooks never receive `sensitive_env` or cloud credentials by default, and repo hooks at
`*_apply` stages are ignored on `tier=prod` (they'd run with the prod write role).

---

## 7. Workers — the pull model

Workers are **self-hosted, stateless and disposable**. They **pull** work; the API never connects
out to them.

```
register (pool token) → heartbeat (every ~20s) → claim (long-poll)
   → clone → setup tool → hooks → init → plan/apply → stream logs → report → repeat
```

- **Claim** is concurrency-safe: `SELECT … FOR UPDATE OF environment SKIP LOCKED` serialises per
  env, and the `one_active_run_per_env` unique index is the hard guarantee (a losing racer gets a
  `23505` and simply re-polls).
- **Labels** target work to pools (e.g. a dedicated `prod` pool).
- **Apply affinity**: the worker that produced a plan is preferred for the apply for ~60s (it still
  has the workspace); otherwise any compatible worker re-plans then applies.
- A worker silent > 60s → `offline`; an active run on a dead worker → `failed (worker_lost)`.
- **Command channel.** The heartbeat response is also the *downward* channel (still no inbound to
  the worker). Today it carries **diagnostics**: an admin clicks a button, a `pending` command is
  queued, the worker picks it up on its next heartbeat, runs a **read-only** bundle (versions, disk,
  env var *names* — never values, recent agent logs) and posts it back. Same mechanism will carry
  `cancel_job` later. Delivery latency is bounded by the claim long-poll (~25 s).

**Run an agent:**

```bash
POOL=$(curl -s -X POST localhost:8000/api/v1/worker-pools -H "$AUTH" -d '{"name":"local"}' | jq -r .token)
STACKD_POOL_TOKEN=$POOL STACKD_API_URL=http://localhost:8000 python -m agent.main
```

---

## 8. Managed state — S3 behind an HTTP backend

The tfstate bytes live in **S3**, but Terraform talks to **Stackd's HTTP backend**, not S3 directly.

```
terraform ──backend "http"──▶ Stackd API ──(SSE-KMS)──▶ S3
                                  └─▶ Postgres: versions, locks, audit
```

Why: per-run **scoped tokens** (read-only for PR plans), **locking visible in the UI** with audited
force-unlock, refusal of a regressive serial, and every write tied to the run that produced it. The
worker receives the backend config in its claim payload and injects it via `-backend-config` — **zero
changes to the user's Terraform code**. Set `managed_state: false` to keep your own `backend "s3"`.

---

## 9. Dependencies, outputs & mocks — the differentiator

Environments can depend on each other and pass **outputs** as inputs. Example: `app/dev` needs the
`network_name` produced by `network/dev`.

```bash
# app/dev depends on network/dev: map upstream output → downstream input, with a mock for bootstrap
curl -s -X POST localhost:8000/api/v1/environments/$APP_DEV/dependencies -H "$AUTH" -H 'content-type: application/json' -d '{
  "upstream_env_id":"'$NET_DEV'",
  "trigger_policy":"on_output_change",
  "references":[{"output_name":"network_name","input_name":"network_name","mock_value":"mock-network"}]
}'
```

**Resolution rule at claim time: real value > mock > explicit error.**

| Situation | What `app/dev` gets |
|---|---|
| `network/dev` already applied, output exists | the **real value**, provenance `dependency:core-network/dev` |
| `network/dev` never applied, a `mock_value` exists | the **mock**, run flagged `used_mocks=true` |
| `network/dev` never applied, no mock | run **fails** immediately (`missing_upstream_output`) |
| upstream output is `sensitive` | config error (sensitive outputs are never propagated) |

**Mocks solve the chicken-and-egg of bootstrapping a cascade:** you can plan `app/dev` with
`network_name = "mock-network"` *before* `network/dev` has ever run — to validate config. But a run
that used mocks **cannot be applied** (`allow_mock_apply=false` by default) — a mocked plan is for
validation, not for real changes. The UI shows a violet `MOCKED` badge.

**Cascade.** When `network/dev` finishes an apply, Stackd captures its outputs and triggers the
downstream `app/dev` run automatically (`triggered_by=dependency`), passing the now-real value. The
cascade **never bypasses protections** — a protected downstream still stops at `unconfirmed`.

```
network/dev  ──apply finished──▶  capture outputs  ──cascade──▶  app/dev run (real network_name)
```

---

## 10. OIDC workload credentials — zero static cloud secrets

Stackd is itself an **OIDC issuer**. At each claim it signs a short-lived **workload token** scoped
to the run and phase; AWS exchanges it for a role via `AssumeRoleWithWebIdentity`. No static AWS
keys anywhere.

Token subject (the basis of trust policies):

```
sub = run:<tier>:<stack>:<phase>      e.g.  run:prod:core-network:apply
```

**Plan and apply assume different roles** — a PR plan physically cannot modify infra:

```hcl
# Trust policy of the prod APPLY role — refuses everything else
condition {
  test     = "StringLike"
  variable = "stackd.example.com:sub"
  values   = ["run:prod:*:apply"]   # any stack, but tier=prod AND phase=apply
}
```

The wildcard is allowed **only on the stack segment** — `tier` and `phase` are always fixed; that's
the double lock with §5. The worker writes the token to a file and exports
`AWS_WEB_IDENTITY_TOKEN_FILE` / `AWS_ROLE_ARN` for terraform only (never for repo hooks). The
`AWS_ROLE_SESSION_NAME = stackd-<run_id>` ties every CloudTrail action back to the run — and thus to
the human who confirmed it.

```bash
curl -s -X PUT localhost:8000/api/v1/environments/$PROD/cloud-integration -H "$AUTH" -d '{
  "plan_role_arn":"arn:aws:iam::123:role/stackd-prod-plan",
  "apply_role_arn":"arn:aws:iam::123:role/stackd-prod-apply","region":"eu-west-1"}'
curl -s -X POST localhost:8000/api/v1/environments/$PROD/cloud-integration/test -H "$AUTH"  # verify AssumeRole
```

---

## 11. Webhooks & Git staleness

A Git webhook (HMAC-verified, secret per repo) maps a pushed branch to the environments tracking it,
filtered by `project_root`:

- **push** → a `tracked` run per matching env, and `head_sha` advances.
- **pull request** → a `proposed` run: plan-only, read-only state, secrets off, mocks allowed.

**Staleness** answers "applied ≠ tip of branch": Stackd compares the last applied commit to the
branch head (`head_sha`, refreshed by webhook, by `git ls-remote` polling, or manually). A stale env
shows a `↑N` chip; a stale `unconfirmed` run shows a banner with *Re-plan*. Staleness is information,
never an automatic trigger.

---

## 12. Audit vs logs — two different journals

| | **Audit** (`audit_events`) | **Logs** (structured JSON) |
|---|---|---|
| Question | *who did what, when* | *what is the system doing / why is it broken* |
| Examples | `run.confirmed`, `run.applied`, `user.role_changed` | `run.transition`, `worker.claim`, `http.request` |
| Storage | Postgres, **append-only at the DB level** (INSERT-only role + trigger), 2 years | in-memory ring buffer + stdout, ephemeral |
| Written | in the **same transaction** as the action | best-effort, never blocks the request |
| Audience | compliance, "who applied to prod last week?" | operators debugging |

They never duplicate each other: a login is an **audit** event, not a debug log.

```bash
curl -s "localhost:8000/api/v1/audit?action=run.applied" -H "$AUTH" | jq   # who applied what
```

---

## 13. Observability — health & logs at a glance

- `GET /api/v1/health` — DB check, workers (online/total + last heartbeat), runs (active/queued),
  recent warnings/errors. The front **Workers & health** page renders this with a live logs panel,
  and a green/red dot sits in the topbar.
- `GET /api/v1/logs` (admin) — the structured JSON ring buffer, filterable by `level`, `event`,
  `worker_id`, `run_id`, `request_id`, free-text `q`. Every HTTP response carries an `X-Request-ID`
  so you can correlate a request across all its log lines.

**Log levels carry signal, not volume:** mutations and domain events (`run.transition`,
`worker.claim`, …) are `INFO`; reads, polls and heartbeats are `DEBUG` (hidden by default); all 4xx
are `WARNING`, 5xx are `ERROR`. Set `STACKD_LOG_LEVEL=DEBUG` to see everything.

- **Worker diagnostics**: from the Workers page, a per-worker button runs the read-only bundle
  (§7) — the simplest remote debug without opening a shell.
- **First-login walkthrough**: a short tour explains the model on first connection. "Seen" is
  stored server-side (`users.onboarded_at`) — never in browser storage (invariant #8).

---

## 14. Notifications — closing the human-in-the-loop

The product's heart is `plan → human confirmation → apply`. Notifications make sure the human
actually *learns* a decision is waiting — without staring at the UI.

A **notification target** is an outbound webhook attached to a **stack** or an **environment**
(exactly like a platform hook), firing on a chosen set of run states:

| state | when it fires | typical use |
|---|---|---|
| `unconfirmed` | a plan is ready and **awaits confirmation** | ping the approver: "prod apply waiting" |
| `failed` | a run failed | alert on breakage |
| `finished` | a run finished (incl. apply) | deploy log / changelog |

Two kinds: `slack` (posts `{"text": …}` to a Slack/Mattermost incoming webhook, with a deep link to
the run) and `webhook` (a structured JSON envelope: state, run id, stack/env, tier, commit, url).

**Example — Slack the approver whenever a prod apply is pending:**

```bash
curl -s -X POST localhost:8000/api/v1/environments/$ENV_ID/notifications -H "$AUTH" \
  -H 'content-type: application/json' -d '{
    "name": "prod-approvals",
    "kind": "slack",
    "url": "https://hooks.slack.com/services/XXX/YYY/ZZZ",
    "on_states": ["unconfirmed", "failed"]
  }'
```

**How it works — a transactional outbox, not a side-effect.** When `transition()` moves a run to a
notify-worthy state, it inserts a row into `notification_outbox` **in the same DB transaction** as
the state change (no HTTP in the request path). A background scheduler task drains the outbox under
a `pg_try_advisory_lock` (one replica at a time) with `FOR UPDATE … SKIP LOCKED`, resolves the
matching targets (env-level for the run's env + stack-level for its stack, filtered by `on_states`),
and POSTs. Consequences worth knowing:

- a **rolled-back** transition never notifies (the outbox row rolls back with it);
- two API replicas never double-send (the lock + `SKIP LOCKED`);
- delivery is **at-least-once** with retries (up to 5 attempts), then the row is dead-lettered and
  the failure is logged — a flaky Slack never blocks or fails a run.

Deep links use `STACKD_APP_URL` (the SPA base, e.g. `http://localhost:5173`).

---

## 15. Quick reference

| Term | One line |
|---|---|
| **Space** | top container; one `default` at MVP |
| **Stack** | template: repo + folder + tool |
| **Environment** | instance: tier + branch + state + variables + protections |
| **Variable set** | reusable bundle of variables, attached to stacks/envs |
| **Provenance** | where a resolved variable came from (`set:…`, `stack`, `env`, `dependency:…`, `mock`) |
| **Run** | one execution against one env, driven by the state machine |
| **`transition()`** | the only thing that changes a run's state |
| **Tier** | configurable catalog; gates apply via the user's `allowed_tiers` set (not ordered) |
| **4-eyes** | triggerer ≠ confirmer on prod / when required |
| **Hook** | shell step at a lifecycle stage; platform (governance) or repo |
| **Worker** | stateless agent that pulls and runs jobs |
| **Managed state** | tfstate in S3 via Stackd's HTTP backend |
| **Mock output** | placeholder value to bootstrap a cascade; blocks apply |
| **Cascade** | downstream runs triggered when an upstream apply finishes |
| **Workload token** | per-run OIDC token, `sub=run:<tier>:<stack>:<phase>` |
| **Audit** | who-did-what, append-only |
| **Proposed run** | plan-only run from a PR (RO state, no secrets) |
| **Notification target** | outbound Slack/webhook on a stack/env, firing on chosen run states |
| **Outbox** | run-event notifications enqueued in-txn, drained by the scheduler (at-least-once) |
