# Observability: health, logs & diagnostics

Stackd is built so that one page and one endpoint answer the question "is the
platform healthy, and if not, why?". Health is a single live snapshot; logs are
a structured, filterable buffer; worker diagnostics let you debug a remote agent
without opening a shell. This page is task-oriented — pick the section that
matches what you're trying to find out.

## Health at a glance

`GET /api/v1/health` (any signed-in user) returns one snapshot: a DB
connectivity check, workers (online / total, and each worker's pool, labels,
status and last heartbeat), runs (active / queued), and how many warnings and
errors the log buffer holds. The front **Workers & health** page renders this
snapshot alongside a live logs panel, and a green/red status dot sits in the
topbar so you see degradation without leaving your current screen.

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
      { "name": "worker-1", "pool": "local", "labels": null, "status": "idle", "online": true,
        "last_heartbeat_at": "2026-10-10T09:31:02+00:00", "seconds_since_heartbeat": 3.2 }
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
`GET /healthz` (liveness) and `GET /metrics` (Prometheus: runs by state, queue
depth, workers online, claim latency, run duration, webhook results — counts
only, never secrets). Restrict `/metrics` at the network layer.

## Querying logs

`GET /api/v1/logs` (admin only) serves the structured JSON ring buffer. Filter
by `level`, `event`, `worker_id`, `run_id`, `request_id`, or free-text `q`.
Every HTTP response carries an `X-Request-ID` header, so you can copy it from a
failing call and correlate that request across all the log lines it produced.

```bash
# Everything that happened for one request
curl -s "localhost:8000/api/v1/logs?request_id=01J7Z…" -H "Authorization: Bearer $TOKEN"

# Errors touching a specific run
curl -s "localhost:8000/api/v1/logs?run_id=$RUN_ID&level=ERROR" -H "Authorization: Bearer $TOKEN"
```

### Log levels carry signal, not volume

Levels are chosen so that the default stream is the *interesting* stream:

- **INFO** — mutations and domain events (`run.transition`, `worker.claim`, …).
- **DEBUG** — reads, polls and heartbeats. Hidden by default (they're noise).
- **WARNING** — all `4xx` responses.
- **ERROR** — all `5xx` responses.

Set `STACKD_LOG_LEVEL=DEBUG` to surface the polls and heartbeats too when you're
chasing something low-level.

## Worker diagnostics

From the **Workers & health** page, a per-worker button queues a read-only debug
bundle: tool versions, disk usage, environment variable *names*, and recent agent
logs. There is no inbound connection to a worker — the request is delivered over
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

## Inspecting object storage (dev)

Managed states and saved plans live in S3 (Garage in dev), reached only by the
API. `task s3-ls` lists the dev bucket with the API's own S3 settings:
`states/<environment_id>/…` for state versions, `artifacts/<run_id>/plan.tfplan`
for the saved plan of a run that can still be applied (encrypted, deleted when
the run ends). If a plan with changes fails while uploading its plan file, the
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
