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

    Browser <-->|"REST + WebSocket"| API
    API <--> PG
    API <--> S3
    Worker -->|"register · heartbeat · claim · events · logs<br/>(outbound only)"| API
    Worker -->|"shallow clone @ commit"| Git
    Worker -->|"tofu http backend"| API
    Worker -->|"OIDC token"| STS
```

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
    confirmed --> applying: worker claims apply
    applying --> finished
    planning --> failed
    checking --> failed
    applying --> failed
    finished --> [*]
    failed --> [*]
    discarded --> [*]
    canceled --> [*]
```

## 🤝 Worker pull protocol

One active run per environment (a partial unique index — the real concurrency guard); different
environments run in parallel.

```mermaid
sequenceDiagram
    participant W as Worker
    participant A as API
    participant H as Human
    W->>A: POST /worker/v1/register (pool token)
    loop every 20s
        W->>A: heartbeat → pending commands (e.g. diagnostics)
    end
    W->>A: POST /jobs/claim?wait=25 (long-poll)
    A-->>W: job payload (resolved vars + secrets, hooks, backend, OIDC token)
    Note over W: clone @ commit · init · hooks · plan
    W->>A: events (phase_started/finished) + streamed logs
    A->>A: transition() → unconfirmed
    H->>A: POST /runs/{id}/confirm
    A->>A: can_apply + 4-eyes + mock-block → confirmed
    W->>A: claim (apply affinity to original worker)
    Note over W: apply · capture outputs
    W->>A: phase_finished (outputs)
    A->>A: transition() → finished, then cascade downstream
```

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
rolled-back transition never notifies and two API replicas never double-send.

```mermaid
flowchart LR
    T["transition() → unconfirmed / finished / failed"] -->|"same txn"| OB[("notification_outbox")]
    SCH["scheduler tick<br/>(advisory lock + SKIP LOCKED)"] -->|"drain unsent"| OB
    SCH -->|"match stack/env targets on_states"| POST["POST Slack / webhook"]
    POST -->|"ok → sent_at"| OB
    POST -->|"fail → retry (≤5)"| OB
```

## 🗂️ Variable resolution

**Variables resolve in layers, with provenance** — the most specific layer wins, and the UI shows
*where* each value came from:

```mermaid
flowchart LR
    S1["auto-attach sets"] --> R
    S2["stack-attached sets"] --> R
    S3["env-attached sets"] --> R
    ST["stack variables"] --> R
    EN["env variables"] --> R
    R{{"resolve<br/>(most specific wins)"}} --> OUT["value + provenance badge"]
```

## 📈 Scaling

Workers are stateless pull agents, so **scaling out = running more of them** (e.g. bumping an ECS
service `desiredCount`). Postgres serializes claims with two guards (`FOR UPDATE … SKIP LOCKED` +
the `one_active_run_per_env` unique index). What's worth knowing:

- 🧩 **The unit of parallelism is the environment, not the worker** — one active run per env, but
  different envs run concurrently. Throughput ≈ `min(workers, envs with queued work)`.
- 🏷️ **Labels segment pools** — a worker only claims runs whose env labels are a subset of its own,
  so you can dedicate (and scale) a prod pool independently.
- ♻️ **Workers are interchangeable & disposable** — apply affinity prefers the worker that planned,
  but expires; a lost worker's run is failed (`worker_lost`) by the scheduler.
- 📊 **No built-in autoscaler**, but `/api/v1/health` and `/queue` expose the signals an external one
  (ECS target-tracking, KEDA) can drive. On Fargate, mind the runner execution model (no DinD).
