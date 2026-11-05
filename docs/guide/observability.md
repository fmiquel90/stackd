# Observability: health, logs & diagnostics

Stackd is built so that one page and one endpoint answer the question "is the
platform healthy, and if not, why?". Health is a single live snapshot; logs are
a structured, filterable buffer; worker diagnostics let you debug a remote agent
without opening a shell. This page is task-oriented — pick the section that
matches what you're trying to find out.

## Health at a glance

`GET /api/v1/health` (any signed-in user) returns one snapshot: a DB
connectivity check, workers (online / total, and each worker's pool and pool
labels, labels, status, runner, agent version and last heartbeat), runs (active
— `unconfirmed` and `confirmed` included — / queued), and how many warnings and
errors the log buffer holds. The front **Workers & health** page renders this
snapshot (refreshed every 5 s) alongside, for admins, the worker pools and a
platform logs panel; the topbar shows workers online / total, in red when none
is online or the API does not answer, so you see degradation without leaving
your current screen.

```bash
curl -s localhost:8000/api/v1/health -H "Authorization: Bearer $TOKEN"
```

```json
{
  "status": "ok",
  "env": "development",
  "version": "0.1.0",
  "checks": { "database": "ok" },
  "workers": {
    "total": 1,
    "online": 1,
    "items": [
      { "id": "0190e…", "name": "worker-1", "pool": "local", "pool_labels": null,
        "labels": null, "status": "idle", "online": true,
        "last_heartbeat_at": "2026-10-10T09:31:02+00:00", "seconds_since_heartbeat": 3.2,
        "runner": "local", "version": null }
    ]
  },
  "runs": { "active": 1, "queued": 4 },
  "log_buffer": { "size": 812, "recent_warn_error": 3 }
}
```

`status` turns `degraded` when the database check fails. A worker whose heartbeat
is older than 60 s shows as offline; see
[Workers & scaling](workers-and-scaling.md#worker-loss) for the worker-lost
lifecycle (only runs *executing* on a lost worker are failed).

Two unauthenticated endpoints sit outside `/api/v1` for probes and scrapers:
`GET /healthz` (liveness, `{"status": "ok"}` without touching the database) and
`GET /metrics` (Prometheus — counts only, never secrets). Restrict `/metrics` at
the network layer.

| Metric | Type | What it counts |
|---|---|---|
| `stackd_runs_total{state}` | gauge | runs per state, read from the database at scrape |
| `stackd_queue_depth` | gauge | runs waiting for a claim (`queued` + `confirmed`) |
| `stackd_workers_online` | gauge | workers that heartbeated within the offline window (60 s) |
| `stackd_claim_latency_seconds` | histogram | from a run's creation to each claim of it (plan and apply claims) |
| `stackd_run_duration_seconds{phase}` | histogram | from the last claim to the terminal state; `phase="apply"` if the run was confirmed, else `"plan"` |
| `stackd_webhook_total{result}` | counter | VCS webhooks received: `accepted`, `rejected`, `no_match` |

The gauges are database-wide; the histograms and the counter are kept per API
process, so sum them across replicas. Setting `STACKD_OTLP_ENDPOINT` also exports
OpenTelemetry traces (HTTP requests and SQL queries, service `stackd-api`) over
OTLP/HTTP; unset, tracing is off.

## Querying logs

`GET /api/v1/logs` (admin only) serves the structured JSON ring buffer: the
last 5,000 records of the API process that answers, in memory (each replica has
its own). Filter by `level` (that level and above), `logger`, `event`,
`worker_id`, `run_id`, `request_id`, or free-text `q`; `limit` (default 200, at
most 1,000) caps the newest-first answer. The **Workers & health** page has the
same view, filtered by level, worker or text.
Every HTTP response carries an `X-Request-ID` header (the caller's own, if the
request sent one), so you can copy it from a failing call and correlate that
request across all the log lines it produced.

```bash
# Everything that happened for one request
curl -s "localhost:8000/api/v1/logs?request_id=01J7Z…" -H "Authorization: Bearer $TOKEN"

# Errors touching a specific run
curl -s "localhost:8000/api/v1/logs?run_id=$RUN_ID&level=ERROR" -H "Authorization: Bearer $TOKEN"
```

### Log levels carry signal, not volume

Levels are chosen so that the default stream is the *interesting* stream:

- **INFO** — mutations (non-`GET` requests) and domain events (`run.transition`,
  `worker.claim`, …).
- **DEBUG** — reads (`GET`), polls and heartbeats. Hidden by default (they're noise).
- **WARNING** — all `4xx` responses, and any request slower than 1.5 s.
- **ERROR** — all `5xx` responses.

Set `STACKD_LOG_LEVEL=DEBUG` to surface the polls and heartbeats too when you're
chasing something low-level. `STACKD_LOG_FORMAT=json` (the default) writes one
JSON object per line on stderr; `pretty` is for reading locally.

The rate-limited routes answer `429` (problem+json) past their budget, counted
per client IP and per API replica: the Google sign-in callback and session refresh
(30/min), the GitHub webhook (120/min) and input discovery (10/min). Behind a reverse proxy, the client IP is
the one the API sees.

## Worker diagnostics

From the **Workers & health** page, an admin's **Diagnostics** button (on an
online worker) queues a read-only debug bundle: platform, runner and API URL,
`tofu` / `terraform` / `git` versions on the agent's host, disk usage, running
containers (`docker` runner), environment variable *names*, and the last 50 agent
log lines. The request is audited (`worker.diagnostics_requested`). There is no
inbound connection to a worker — the request is delivered over
the **heartbeat command channel** (the worker pulls it on its next 20 s beat) and
the result comes back via `POST /worker/v1/commands/{id}/result`.

```bash
curl -s -X POST localhost:8000/api/v1/workers/$WORKER_ID/diagnostics -H "Authorization: Bearer $TOKEN"
# then read it back once the worker has answered:
curl -s localhost:8000/api/v1/workers/$WORKER_ID/diagnostics -H "Authorization: Bearer $TOKEN"
```

!!! warning "Diagnostics expose names, never values"
    The bundle lists environment variable **names** only — never their values.
    Secrets are never logged anywhere: `sensitive` variables are masked by the
    agent and never appear in logs, health output, or a diagnostics bundle.

## Run logs: hot, archived, purged

A run's log lines land in the database first. About a minute after the run ends,
the scheduler archives them to S3 as `logs/<run_id>/<phase>.log.gz`; the
database rows are then purged `LOG_HOT_RETENTION_DAYS` (default 7) days after
archiving by a daily retention task. Reads don't change: the run page and
`GET /api/v1/runs/{id}/logs` serve whichever tier holds the lines, and
`GET /api/v1/runs/{id}/logs/download` returns them as plain text. While S3 is
unreachable, runs stay unarchived (logged as `logs.archive_failed`) and keep
their rows until a later tick succeeds.

The same daily task deletes expired refresh-token families and — only when
`STACKD_AUDIT_PURGE_DATABASE_URL` names the distinct purge role — audit events
older than `AUDIT_RETENTION_DAYS` (default 730), recording an `audit.purged`
event. Each step logs `scheduler.retention` (with a count) or
`scheduler.retention_failed`.

## Inspecting object storage (dev)

Managed states, saved plans and archived logs live in S3 (Garage in dev), reached
only by the API. `task s3-ls` lists the dev bucket with the API's own S3 settings:
`states/<environment_id>/…` for state versions, `artifacts/<run_id>/plan.tfplan`
for the saved plan of a run that can still be applied (encrypted, deleted when
the run ends), `logs/<run_id>/…` for archived run logs. If a plan with changes fails while uploading its plan file, the
API cannot reach the bucket: run `task s3`, then `task dev`. See
[Dev environment §2](../DEV.md#2-docker-compose-stack).

## Watching a run live

You don't need to poll for an active run. The run page and the dependency graph
subscribe over WebSocket: state transitions, new log lines, and graph status
update in place as the worker reports them.

## See also

- [Workers & scaling](workers-and-scaling.md) — worker lifecycle, pools, heartbeat
- [Concepts](../CONCEPTS.md) — §13 Observability
- [SPECS](../SPECS.md) — endpoints, worker protocol, logging rules
