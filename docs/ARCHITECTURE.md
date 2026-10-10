# Architecture

> The 10-minute tour of how Stackd works inside. [`SPECS.md`](SPECS.md) is the exhaustive technical
> truth; [`CONCEPTS.md`](CONCEPTS.md) explains each concept with worked examples.

## 🧭 System

**The API is the single source of truth. Workers have no inbound ports — they only pull.** The
queue is Postgres (`SELECT … FOR UPDATE SKIP LOCKED`), there's no broker, and Terraform talks to the
API's HTTP state backend rather than to S3 directly.

```mermaid
flowchart LR
    Browser["SPA (React)"]
    subgraph api_box["API (FastAPI) — source of truth"]
      API["REST /api/v1 · Worker API /worker/v1<br/>state backend · OIDC issuer · WS hub · scheduler"]
    end
    PG[("PostgreSQL 18<br/>queue · audit · LISTEN/NOTIFY")]
    S3[("S3 / Garage<br/>tfstate · logs · artifacts")]
    Worker["Worker (pull agent)<br/>+ ephemeral runner"]
    Git[("Git repo")]
    STS["Cloud STS<br/>(AssumeRoleWithWebIdentity)"]

    Hooks["Slack · webhooks · GitHub"]

    Browser <-->|"REST + WebSocket"| API
    API <--> PG
    API <-->|"states · encrypted saved plans"| S3
    API -->|"ls-remote head poll · check-repo · input discovery"| Git
    API -->|"notifications · PR status/comment"| Hooks
    Worker -->|"register · heartbeat · claim · events · logs<br/>saved plan up/down (outbound only)"| API
    Worker -->|"shallow clone @ pinned commit"| Git
    Worker -->|"tofu http backend (TF_HTTP_*)"| API
    Worker -->|"OIDC token"| STS
```

Only the API holds the S3 and database credentials: workers reach states and saved plans through
it.

## 🔄 Run state machine

Every state change goes through one function, `transition()`: it checks legality, does an atomic
guarded update, writes a `run_event` + (for human/terminal actions) an `audit_event` in the **same
transaction**, emits `LISTEN/NOTIFY`, and enqueues any notification. `checking` is skipped when
there are no `after_plan` hooks.

```mermaid
stateDiagram-v2
    [*] --> queued
    queued --> preparing: worker claims
    queued --> canceled
    queued --> discarded
    preparing --> planning
    preparing --> running: command run (import, state rm…)
    running --> finished
    running --> failed
    preparing --> failed
    planning --> checking: has after_plan hooks
    planning --> unconfirmed: diff, no autodeploy
    planning --> confirmed: autodeploy
    planning --> finished: empty diff
    checking --> unconfirmed
    checking --> confirmed
    checking --> finished
    unconfirmed --> confirmed: human confirm (can_apply + 4-eyes + mock-block)
    unconfirmed --> discarded
    unconfirmed --> canceled
    confirmed --> applying: worker claims apply (saved plan)
    applying --> finished
    planning --> failed
    checking --> failed
    applying --> failed
    finished --> [*]
    failed --> [*]
    discarded --> [*]
    canceled --> [*]
```

Not drawn: a closed pull request cancels its proposed runs from whatever in-flight state they are
in. A user cancels only `queued` or `unconfirmed` runs: stopping a job a worker is executing is not
implemented yet.

**Worker loss** fails only runs whose job is *executing* on the lost worker (`preparing`,
`planning`, `checking`, `running`, `applying`) — after 120 s, or, for a job holding apply
credentials (`applying`, a mutating command), after the apply budget + grace. `unconfirmed` and
`confirmed` runs are never reclaimed: they wait on a human or on the apply claim. Every terminal
transition drops the run's saved plan.

## 🤝 Worker pull protocol

One active run per environment (a partial unique index — the real concurrency guard); different
environments run in parallel. What a human confirms is the plan file the plan job made: the run is
pinned to the commit that plan checked out, and the apply job applies exactly that file.

```mermaid
sequenceDiagram
    participant W as Worker
    participant A as API
    participant S as S3
    participant H as Human
    W->>A: POST /worker/v1/register (pool token)
    loop every 20s
        W->>A: heartbeat → pending commands (e.g. diagnostics)
    end
    W->>A: POST /jobs/claim?wait=25 (long-poll)
    A-->>W: plan job: vars + secrets, hooks, state token (plan scope), plan OIDC token
    Note over W: clone the env branch head (or the pinned commit)
    W->>A: phase_started(planning, commit_sha)
    A->>A: pin the run to that commit
    Note over W: hooks · init · plan -out=plan.tfplan (tofu → /state/v1 via TF_HTTP_*)
    W->>A: PUT /jobs/{id}/artifacts/plan.tfplan (before after_plan hooks)
    A->>S: encrypt (AES-256-GCM) → artifacts/{run_id}/plan.tfplan
    Note over W: after_plan hooks (checking)
    W->>A: phase_finished (summary, checks) + streamed logs
    A->>A: transition() → unconfirmed
    H->>A: POST /runs/{id}/confirm
    A->>A: can_apply + 4-eyes + mock-block → confirmed
    W->>A: claim (the plan's worker first for 60 s, then any compatible worker)
    A-->>W: apply job: pinned commit, state token (rw scope), apply OIDC token, apply budget
    Note over W: clone the pinned commit · init
    W->>A: GET /jobs/{id}/artifacts/plan.tfplan
    A->>S: fetch + decrypt
    A-->>W: plan.tfplan (missing or unreadable → the job fails closed, no re-plan)
    Note over W: before_apply · apply plan.tfplan · output -json · after_apply
    W->>A: phase_finished (outputs)
    A->>A: transition() → finished (saved plan deleted), then cascade downstream
```

**Per-job state tokens.** Each claim mints a state token for that job only, with a scope fixed at
mint time: `ro` (a PR or drift plan, a read-only command: read only, no lock), `plan` (a plan job:
read and lock while planning, never write), `rw` (an apply or a mutating command: write; TTL = the
apply budget + grace). A token whose run has ended can no longer write or lock. The worker hands it
to tofu only, as `TF_HTTP_*` environment variables — hooks never see it.

**Apply affinity.** A confirmed run is reserved for the worker that planned it for 60 s, then any
compatible worker takes it. Nothing is carried over in the workspace: the apply always starts from a
fresh clone of the pinned commit and the saved plan from the API.

## 🧬 Dependencies, mocks & cascade

At claim the API resolves each referenced input as **real upstream output › mock › explicit
error**. A run that consumed a mock is flagged `used_mocks` and can't be applied unless the env opts
in. When a tracked apply finishes, downstream environments cascade — protections are never bypassed.

```mermaid
flowchart TD
    subgraph net["network/prod (upstream)"]
      NO["output: network_name"]
    end
    subgraph app["app/prod (downstream)"]
      direction TB
      R{"resolve input<br/>network_name"}
      R -->|"real output exists"| Real["use real value"]
      R -->|"no output, mock set"| Mock["use mock → used_mocks=true<br/>apply blocked (allow_mock_apply=false)"]
      R -->|"no output, no mock"| Err["run fails: missing_upstream_output"]
    end
    NO -.->|"captured on apply"| R
    NetApply["network/prod apply finishes"] -->|"cascade (tracked)"| AppRun["new app/prod run"]
    AppRun --> R
```

## 🔔 Notifications — transactional outbox

`transition()` enqueues a row in `notification_outbox` in the same transaction (no I/O in the
request path). The scheduler drains it under an advisory lock with `FOR UPDATE SKIP LOCKED`, so a
rolled-back transition never notifies and two API replicas never double-send. No DB transaction is
held during the HTTP call.

```mermaid
flowchart LR
    T["transition() → unconfirmed / finished / failed"] -->|"same txn"| OB[("notification_outbox")]
    SCH["scheduler tick<br/>(advisory lock + SKIP LOCKED)"] -->|"drain unsent"| OB
    SCH -->|"match stack/env targets on_states"| DNS["resolve host once<br/>every IP must be public"]
    DNS -->|"https POST to the vetted IP<br/>(no env proxy, no redirect)"| POST["Slack / webhook"]
    POST -->|"ok → sent_at"| OB
    POST -->|"fail → retry (≤5)"| OB
```

The POST dials the address the SSRF check vetted, with Host, SNI and certificate checks on the
original hostname, so DNS rebinding can't redirect it to an internal host. It deliberately ignores
`HTTPS_PROXY` and other env proxies (a proxy would resolve the name again): an API whose only egress
is a proxy cannot deliver notifications. The PR status/comment post-back (`vcs_outbox`) follows the
same outbox pattern.

## 🗂️ Variable resolution

**Variables resolve in layers, with provenance** — the most specific layer wins, and the UI shows
*where* each value came from. Only variable sets of the environment's own space apply:

```mermaid
flowchart LR
    S1["sets by rule<br/>(auto_attach · label selector)"] --> R
    S0["tier-attached sets"] --> R
    S2["stack-attached sets"] --> R
    S3["env-attached sets"] --> R
    ST["stack variables"] --> R
    EN["env variables"] --> R
    R{{"resolve<br/>(most specific wins)"}} --> OUT["value + provenance badge"]
```

Upstream outputs and mocks (`dependency:`, `mock`) and external secret references are merged in at
claim time.

## 📈 Scaling

Workers are stateless pull agents, so **scaling out = running more of them** (e.g. bumping an ECS
service `desiredCount`). Postgres serializes claims with two guards (`FOR UPDATE … SKIP LOCKED` +
the `one_active_run_per_env` unique index). What's worth knowing:

- 🧩 **The unit of parallelism is the environment, not the worker** — one active run per env, but
  different envs run concurrently. Throughput ≈ `min(workers, envs with queued work)`.
- 🏷️ **Labels segment pools** — a worker only claims runs whose env labels are a subset of its own,
  so you can dedicate (and scale) a prod pool independently.
- ♻️ **Workers are interchangeable & disposable** — apply affinity prefers the worker that planned
  for 60 s, but the apply never depends on it (pinned commit + saved plan from the API). Losing a
  worker only fails the runs *executing* on it (`worker_lost`); runs waiting for a human or for the
  apply claim survive.
- 🔁 **Each worker runs `STACKD_MAX_CONCURRENT_JOBS` jobs at once** (default 1), always on
  different environments.
- 🗓️ **The scheduler runs in every API replica**, each task under its own advisory lock (held on a
  dedicated connection), so a task runs once per tick across replicas. The git head poll runs as a
  separate task (at most 8 `ls-remote` at a time), so slow repos never delay worker-loss detection
  or notifications.
- 📊 **No built-in autoscaler**, but `/api/v1/health` and `/queue` expose the signals an external one
  (ECS target-tracking, KEDA) can drive; `/metrics` serves Prometheus gauges. On Fargate, mind the
  runner execution model (no DinD).
