# Observability: health, logs & diagnostics

Stackd is built so that one page and one endpoint answer the question "is the
platform healthy, and if not, why?". Health is a single live snapshot; logs are
a structured, filterable buffer; worker diagnostics let you debug a remote agent
without opening a shell. This page is task-oriented — pick the section that
matches what you're trying to find out.

## Health at a glance

`GET /api/v1/health` returns one snapshot: a DB connectivity check, workers
(online / total with each one's last heartbeat), runs (active / queued), and a
short list of recent warnings and errors. The front **Workers & health** page
renders this snapshot alongside a live logs panel, and a green/red status dot
sits in the topbar so you see degradation without leaving your current screen.

```bash
curl -s localhost:8000/api/v1/health -H "Authorization: Bearer $TOKEN"
```

```json
{
  "db": "ok",
  "workers": { "online": 2, "total": 3, "last_heartbeat_at": "2026-06-14T09:31:02Z" },
  "runs": { "active": 1, "queued": 4 },
  "recent": [
    { "level": "WARNING", "event": "http.request", "request_id": "01J…", "msg": "404 GET /api/v1/runs/unknown" }
  ]
}
```

A worker whose heartbeat is older than 60 s shows as offline; see
[Workers & scaling](workers-and-scaling.md) for the worker-lost lifecycle.

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

## Watching a run live

You don't need to poll for an active run. The run page and the dependency graph
subscribe over WebSocket: state transitions, new log lines, and graph status
update in place as the worker reports them.

## See also

- [Workers & scaling](workers-and-scaling.md) — worker lifecycle, pools, heartbeat
- [Concepts](../CONCEPTS.md) — §13 Observability
- [SPECS](../SPECS.md) — endpoints, worker protocol, logging rules
