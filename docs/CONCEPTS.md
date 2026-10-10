# CONCEPTS.md — Stackd concepts, explained with examples

> A concept guide for everyone using or building Stackd. It teaches the **mental model** with
> concrete, copy-pasteable examples. For the exhaustive field-by-field truth, see **SPECS.md** —
> this document never contradicts it, it illustrates it.

## Key concepts at a glance

Read this first if you are new to Stackd: each line is expanded, with examples, in the numbered
section given in the last column.

```
Space                  a team or project boundary: who can see and do what
└── Stack              the code: a Git repo + a folder (the template)
    ├── Environment    dev      ┐ real deployments of that code, each with its own
    ├── Environment    staging  ├ state, variables and rules
    └── Environment    prod     ┘
        └── Run        one change to one environment: plan → review → apply
            ├── Job    plan job: a worker plans the change and runs the checks
            └── Job    apply job: a worker applies the reviewed plan
```

| Concept | What it is | Why it matters | § |
|---|---|---|---|
| **Space** | A boundary for a team or project, holding its stacks, variable sets and worker pools. | Members get a role and a set of tiers *per space*; nothing crosses from one space to another. | 1 |
| **Stack** | The code: a Git repository, a folder in it, and the tool (OpenTofu or Terraform). | Written once, deployed many times. A stack never runs on its own. | 2 |
| **Environment** | One real deployment of a stack (`dev`, `prod`…): its branch, state, variables and protections. | Environments are fully isolated from each other — the opposite of Terraform workspaces. | 2 |
| **Variables & variable sets** | The code's inputs, set on the environment, the stack, or in sets shared across stacks. | The most specific value wins and the UI shows where each value came from. Secrets are write-only. | 3 |
| **Run** | One change to one environment: plan, wait for review, apply exactly the reviewed plan. | It is the unit of change, of review and of audit. One active run per environment at a time. | 4 |
| **Job** | One piece of a run that a worker executes: the *plan job*, then — once confirmed — the *apply job*. A command run has a single *command job*. | The two jobs of a run can land on different workers: the apply job starts from a fresh clone and the saved plan. | 4 |
| **Tier** | A label on an environment (`dev`, `prod`, or your own). | Decides who may approve it, and whether someone other than the person who started the run must confirm. | 5 |
| **Role** | *reader* (looks), *writer* (starts runs, edits configuration), *approver* (also confirms applies on their tiers), *admin* (also manages workers, users and settings). | Approving needs both the role **and** the environment's tier in your allowed tiers. | 5 |
| **Hook** | A command run at a fixed step of a job: a security scan, a cost check, a policy. | Platform hooks are enforced and trusted; hooks from the repo run without secrets. Each result is kept on the run as a *check*. | 6 |
| **Worker** | The agent that runs OpenTofu on your infrastructure. | It pulls its jobs (no inbound port), keeps nothing between jobs and can be replaced at any time. | 7 |
| **Managed state** | The `tfstate`, stored by Stackd in S3 behind its own HTTP backend. | Each job gets a short-lived token scoped to what it may do: read (a pull request's plan), lock (a plan), or write (an apply). | 8 |
| **Dependency & mock** | One environment's outputs feeding another's inputs (network → app). | An upstream change re-plans its dependents. A mock lets a new chain plan before its upstream exists, but blocks the apply by default. | 9 |
| **Cloud credentials (OIDC)** | Short-lived cloud credentials minted for each job. | No static cloud keys to store or rotate; the plan job assumes the plan role (read-only), the apply job the apply role. | 10 |
| **Staleness** | "This environment is behind its branch." | Tells you a merged change hasn't been applied yet; it never triggers anything by itself. | 11 |
| **Audit** | An append-only record of every action, written with the action itself. | Answers "who changed prod, and why?" | 12 |

### The life of a change

1. **Someone pushes, opens a pull request, or clicks *Plan*.** Stackd creates a run for the
   environment(s) tracking that branch. A pull request only ever gets a read-only plan.
2. **A worker runs the plan job**: it checks out the exact commit and plans with credentials
   minted for this job only. Hooks check the plan.
3. **The run waits for a human** (unless the environment autodeploys a clean plan). An approver
   allowed on that tier reads the diff, the checks and every input with its origin, then confirms
   or discards. If the tier requires four-eyes, that approver can't be the person who started the
   run.
4. **A worker runs the apply job**: it applies the saved plan — exactly what was reviewed, on the
   same commit.
5. **If the outputs changed, dependent environments re-plan** under their own approval rules.
   Everything above is in the audit log.

## Following the examples

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
variable sets, worker pools and secret sources. The bootstrap creates `default` (the dev seed adds
`demo`), and an instance admin can create more. A space is also a **permission boundary**: each
member has a membership in it (role, allowed tiers, destroy right), you only see and touch the
spaces you belong to (an instance admin reaches all), and configuration never crosses spaces (§3).

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
1. variable sets applying by rule          (auto_attach, or a label selector matching
                                            the environment)
2. variable sets attached to a tier        (by priority)
3. variable sets attached to a stack       (by priority)
4. variable sets attached to an environment (by priority)
5. stack variables                         (common to all environments of the stack)
6. environment variables                   ← always wins
```

A **variable set** is a named, reusable bundle (e.g. `common-aws`, `datadog`, `region-eu`) you
attach to many stacks and environments. `auto_attach: true` means "attach to every stack in the
space"; a `selector` (`{label: value}`) attaches it to every environment whose labels match.

**A set stays in its space.** You can only attach it to a stack or environment of the set's own
space (`400 Cross-space attachment` otherwise), and a **tier** attachment reaches that tier's
environments **in the set's space only** — tiers are a global catalog, the set is not. Deleting a
stack or an environment removes the attachments that target it.

**Worked example.** Suppose:

| Layer | defines |
|---|---|
| set `common-aws` (auto_attach) | `region = eu-west-1`, `tags = {team:core}` |
| set `region-us` (attached to environment `prod-us`) | `region = us-east-1` |
| stack variable | `cidr = 10.0.0.0/16` |
| environment variable on `prod-us` | `cidr = 10.9.0.0/16` |

Resolving variables for `prod-us`:

| Variable | Value | Provenance |
|---|---|---|
| `region` | `us-east-1` | `set:region-us` (a set attached to the environment beats auto_attach) |
| `tags` | `{team:core}` | `set:common-aws` |
| `cidr` | `10.9.0.0/16` | `env` (the environment's override beats the stack variable) |

See it for real:

```bash
curl -s localhost:8000/api/v1/environments/$ENV/resolved-variables -H "$AUTH" | jq
# [{ "name":"region","injected_name":"TF_VAR_region","value":"us-east-1","provenance":"set:region-us" }, ...]
```

Every resolved variable carries its **provenance** so the UI can show "inherited from `common-aws`"
or "overridden here". The provenance is also frozen onto the run (`variable_provenance`) for audit.

### Variable kinds

- `terraform` → written to an auto-loaded tfvars file that exists only while tofu runs (listed as
  `TF_VAR_<name>`).
- `environment` → injected as a process environment variable for the run.

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

A **run** is one change to one environment, from plan to apply. Its life is an explicit state machine — and
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

**Run and job.** A run is the *change*; a **job** is what a worker executes for it. A run that goes
all the way has two jobs: the **plan job** (`preparing` → `planning` → `checking`: clone, hooks,
`init`, `plan`, checks) and, once confirmed, the **apply job** (`applying`: fresh clone, apply the
saved plan, outputs). They can run on different workers. A [command run](guide/commands.md) (`import`, `state rm`…) has one **command job**;
a pull request's run has only a plan job. Hooks, credentials and state tokens are per job.

**Example — a manual plan that needs confirmation:**

```bash
# 1. trigger → run is created 'queued'
RUN=$(curl -s -X POST localhost:8000/api/v1/environments/$ENV/runs -H "$AUTH" -d '{}' | jq -r .id)

# 2. a worker claims it → preparing → planning, then reports a non-empty plan → 'unconfirmed'
# 3. a human confirms (subject to permissions, §5)
curl -s -X POST localhost:8000/api/v1/runs/$RUN/confirm -H "$AUTH"
# → confirmed → a worker claims the apply → applying → finished
```

**What gets applied is exactly what was reviewed.** The plan job saves its `plan.tfplan`; the API
encrypts it and keeps it in S3 until the run ends. The run is pinned to the commit the plan checked
out, and the apply job clones that commit and runs `tofu apply plan.tfplan` — it never re-plans. If
the saved plan is missing, or the state moved since the plan (tofu refuses a stale plan), the run
fails: trigger a new plan.

**Concurrency invariant:** at most **one active run per environment** (enforced by a partial unique
index). Two environments of the same stack can run in parallel; two runs on the *same* environment
cannot.

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

| User | role | allowed tiers | dev | staging | prod |
|---|---|---|---|---|---|
| Bob | writer | `{dev, staging}` | ❌ (not approver) | ❌ | ❌ |
| Carol | approver | `{dev, staging}` | ✅ | ✅ | ❌ (prod not in her set) |
| Alice | approver | `{dev, staging, prod}` | ✅ | ✅ | ✅ |
| Dan | approver | `{dev, prod}` | ✅ | ❌ | ✅ |

These are the **effective** permissions in the environment's space: a space membership can give
someone a different role, tier set or destroy right there than their instance defaults.

Extra rules:

- **Destroy** (`type=destroy`) additionally requires `can_destroy=true` — a separate, explicit right.
- **Four-eyes**: on a tier flagged `requires_four_eyes` (the seeded `prod`), or when the
  environment sets `require_second_pair_of_eyes`, **someone other than the person who triggered
  the run must confirm it**. It only bites human-triggered runs — a webhook or cascade run has no
  human to oppose.
- **Mock block**: a run that consumed mock outputs cannot be applied unless `allow_mock_apply=true`
  (see §9).

When a confirm is refused, the API returns the reason in clear, e.g. `403 "tier prod not in your
allowed tiers (dev, staging)"`, which the UI shows on the disabled Confirm button.

**Double lock with the cloud:** for prod, the restriction also lives in the AWS trust policy (§10),
so bypassing the API still can't assume the prod apply role.

---

## 6. Hooks & checks — custom flow steps

Hooks run shell commands at lifecycle stages: `before_init`, `after_init`, `before_plan`,
`after_plan`, `before_apply`, `after_apply`. They come from **two sources, merged**:

| Source | Where | Editable by | Use |
|---|---|---|---|
| **Platform** | UI/API (per stack or environment) | writer+ (audited) | governance — **not bypassable by a PR** |
| **Repo** | `.stackd.yml` in the repo | anyone who pushes | project-specific logic |

Order per stage: **platform (stack) → platform (environment) → repo**. Security-critical checks
belong on the platform side. The plan job runs the `*_init`, `*_plan` stages; the apply job the
`*_apply` ones.

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

- `on_failure: fail` → the job stops at that stage and the run fails (`run.check_failed`), at every
  stage. A failing `before_apply` hook means nothing is applied. A failing `after_apply` hook fails
  the run after the infrastructure changed: its outputs are still recorded, but nothing cascades.
- `on_failure: warn` → the run continues but is **forced through `unconfirmed`** even if autodeploy
  was on — a human must own the warning. (At an apply stage the run is already confirmed: the
  warning is only recorded.)
- Each hook has 10 minutes (`STACKD_HOOK_TIMEOUT_SECONDS` on the worker); past it, it is killed and
  fails by its `on_failure`. If tofu can't produce the plan JSON, the plan job fails rather than
  check an empty plan.

**Checks.** Every hook result is a *check*, kept on the run with the stage it ran at
(`check_results.results`: `ok`, `warn`, `fail`; `check_results.checks`: only warn/fail).

**Repo hooks get nothing secret**: of the worker's environment only an allowlist (`PATH`, `HOME`,
locale, proxies…), the run's non-sensitive environment variables, no cloud credentials, and a
`plan.json` with every sensitive value masked. While a repo hook runs, neither the saved plan, the
run's tfvars, the cloud token file nor the repo token is on disk. Platform hooks are trusted: they
get the run's secrets, the cloud credentials and the raw `plan.json`. Neither tofu nor any hook sees
the agent's own `STACKD_*` settings. See [Hooks](guide/hooks.md#what-hooks-can-see).

---

## 7. Workers — the pull model

Workers are **self-hosted, stateless and disposable**. They **pull** work; the API never connects
out to them.

```
register (pool token) → heartbeat (every ~20s) → claim (long-poll)
   → clone → hooks → init → plan/apply → stream logs → report → repeat
```

- **Claim** is concurrency-safe: `SELECT … FOR UPDATE OF environment SKIP LOCKED` serialises per
  environment, and the `one_active_run_per_env` unique index is the hard guarantee (a losing racer gets a
  `23505` and simply re-polls).
- **Labels** target work to pools (e.g. a dedicated `prod` pool).
- **Apply affinity**: the worker that produced a plan is preferred for the apply for ~60s; past
  that, any compatible worker takes it. Either way the apply worker starts from a fresh clone of
  the pinned commit and applies the saved plan it downloads from the API (§4) — no workspace is
  reused, nothing is re-planned.
- **Per-job credentials**: each job gets its own state token, scoped to what it does — `ro` for a
  PR plan or a read-only command, `plan` for a plan job (it may lock, never write), `rw` for an
  apply job or a mutating command (§8) — and its own cloud credentials: the plan role for a plan
  job, the apply role for an apply job (§10).
- **Worker loss**: a worker silent > 60s → `offline`. A run whose job is *executing* on it
  (`preparing`, `planning`, `checking`, `running`, `applying`) → `failed (worker_lost)` after 120s.
  A job holding apply credentials (an `applying` run, a mutating command) is only reclaimed after
  the apply budget + grace (15 + 5 min by default), so a long apply isn't failed mid-flight and its
  tokens have expired before another worker takes the environment. `unconfirmed` and `confirmed` runs are
  never reclaimed: they wait on a human or on the apply claim, not on the plan worker.
- **Command channel.** The heartbeat response is also the *downward* channel (still no inbound to
  the worker). Today it carries **diagnostics**: an admin clicks a button, a `pending` command is
  queued, the worker picks it up on its next heartbeat, runs a **read-only** bundle (versions, disk,
  environment variable *names* — never values, recent agent logs) and posts it back. Same mechanism will carry
  `cancel_job` later. Delivery latency is bounded by the heartbeat interval (~20 s).

**Run an agent:**

```bash
POOL=$(curl -s -X POST localhost:8000/api/v1/worker-pools -H "$AUTH" \
  -H 'content-type: application/json' -d '{"name":"local"}' | jq -r .token)
cd worker && STACKD_POOL_TOKEN=$POOL STACKD_API_URL=http://localhost:8000 uv run python -m agent.main
```

---

## 8. Managed state — S3 behind an HTTP backend

The tfstate bytes live in **S3**, but Terraform talks to **Stackd's HTTP backend**, not S3 directly.

```
terraform ──backend "http"──▶ Stackd API ──(SSE-KMS)──▶ S3
                                  └─▶ Postgres: versions, locks, audit
```

Why: per-job **scoped tokens**, **locking visible in the UI** with audited force-unlock, refusal
of a regressive serial, and every write tied to the run that produced it. The token's scope follows
the job: `ro` (PR plans, read-only commands: read only), `plan` (a plan may lock, never write),
`rw` (an apply or a mutating command). Once its run has ended, a token can no longer write or lock.

The worker adds a bare `backend "http" {}` block and hands the address and the token to tofu
**only**, as `TF_HTTP_*` environment variables — never `-backend-config`, which tofu would persist
in `.terraform/` (readable by repo hooks) and in the saved plan. **Zero changes to the user's
Terraform code**: the repo declares no backend. Set `managed_state: false` to keep your own
`backend "s3"`.

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
the double lock with §5. The worker exports `AWS_WEB_IDENTITY_TOKEN_FILE` / `AWS_ROLE_ARN` to tofu
and platform hooks only — never to repo hooks — and the token file exists only while one of them
runs. The
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

- **push** → a `tracked` run per matching environment, and `head_sha` advances.
- **pull request** → a `proposed` run: plan-only, read-only state, mocks allowed. (Withholding
  secrets from it is specified but not implemented yet: today it gets the environment's secrets
  like any plan — see [Runs & approvals](guide/runs-and-approvals.md#proposed-runs-from-a-pr).)

**Staleness** answers "applied ≠ tip of branch": Stackd compares the commit of the last finished
tracked run (every run is pinned to the commit it planned) to the branch head (`head_sha`, refreshed
by webhook, by `git ls-remote` polling every 15 min, or manually). The API exposes it on each
environment (`stale`, `commits_ahead`); the `↑N` chip and the *Re-plan* banner on a stale
`unconfirmed` run (DESIGN §5.1–5.2) are not in the UI yet. Changing an environment's branch resets
its recorded head; a push deleting the branch clears it. Staleness is information, never an
automatic trigger.

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
the run) and `webhook` (a structured JSON envelope: state, run id, stack/environment, tier, commit, url).

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
matching targets (those on the run's environment + those on its stack, filtered by `on_states`),
and POSTs — with no DB transaction held during the HTTP call. Consequences worth knowing:

- a **rolled-back** transition never notifies (the outbox row rolls back with it);
- two API replicas never double-send (the lock + `SKIP LOCKED`);
- delivery is **at-least-once** with retries (up to 5 attempts), then the row is dead-lettered and
  the failure is logged — a flaky Slack never blocks or fails a run.

Deep links use `STACKD_APP_URL` (the SPA base, e.g. `http://localhost:5173`).

**SSRF guard.** A target URL must be `https` and its host must resolve to public addresses only
(no private, loopback, link-local or metadata IP), checked at create/update and again at every
delivery. The POST connects to the IP that check vetted (Host, SNI and certificate stay on the
hostname), never follows redirects, and **ignores `HTTPS_PROXY` and other proxy variables by design**: a proxy
would resolve the name again. An API whose only egress is a proxy cannot deliver notifications.
Targets are deleted with their stack or environment.

---

## 15. Quick reference

| Term | One line |
|---|---|
| **Space** | top container and permission boundary (`default`, plus any an admin creates) |
| **Stack** | template: repo + folder + tool |
| **Environment** | instance: tier + branch + state + variables + protections |
| **Variable set** | reusable bundle of variables, attached to tiers, stacks or environments of its own space |
| **Provenance** | where a resolved variable came from (`set:…`, `stack`, `env`, `dependency:…`, `mock`) |
| **Run** | one change to one environment, from plan to apply, driven by the state machine |
| **Job** | what a worker executes for a run: the plan job, the apply job, or a command job |
| **`transition()`** | the only thing that changes a run's state |
| **Tier** | configurable catalog; gates apply via the user's `allowed_tiers` set (not ordered) |
| **Four-eyes** | the person who triggered a run can't confirm it, on a `requires_four_eyes` tier or when the environment requires it |
| **Hook** | shell step at a lifecycle stage; platform (governance, trusted) or repo (no secrets) |
| **Check** | a hook's result (`ok`, `warn`, `fail`), kept on the run with its stage |
| **Worker** | stateless agent that pulls and runs jobs |
| **Managed state** | tfstate in S3 via Stackd's HTTP backend, per-job scoped tokens |
| **Saved plan** | the reviewed `plan.tfplan`, kept encrypted until the run ends; the apply applies exactly it |
| **Mock output** | placeholder value to bootstrap a cascade; blocks the apply unless the environment sets `allow_mock_apply` |
| **Cascade** | downstream runs triggered when an upstream apply finishes |
| **Workload token** | per-job OIDC token, `sub=run:<tier>:<stack>:<phase>` |
| **Audit** | who-did-what, append-only |
| **Proposed run** | plan-only run from a PR (`ro` state token; still gets secrets today) |
| **Notification target** | outbound Slack/webhook on a stack or environment, firing on chosen run states |
| **Outbox** | run-event notifications enqueued in-txn, drained by the scheduler (at-least-once) |
