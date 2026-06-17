# SPECS_H_UPDATE.md — Observability + API guardrails (Phase H)

> Plan: `PLAN_H_UPDATE.md`. Folds into `docs/SPECS.md` when shipped. No schema change.

## Metrics — `GET /metrics` (Prometheus, `prometheus-client`)
`stackd_runs_total{state}`, `stackd_queue_depth`, `stackd_workers_online`,
`stackd_claim_latency_seconds`, `stackd_run_duration_seconds{phase}`, `stackd_webhook_total{result}`.
The scheduler updates gauges on its tick.

## Tracing (OpenTelemetry, OTLP exporter behind `STACKD_OTLP_ENDPOINT`)
Spans: HTTP request → run `transition` → claim → worker phase events (linked via `run_id`). No-op
when the endpoint is unset.

## Guardrails
- Rate-limit `auth/*`, `webhooks/github`, and `discover-inputs` (token bucket per IP / per token).
- Discovery clone caps: `--depth 1` already; add a **size/time budget** (reject repos over N MB or
  clones over the existing 30 s) and a max `.tf` count parsed.

## Invariants
`/metrics` and traces never include secret values or tfvars; metrics are cardinality-bounded (label
by state/phase, never by run id).
