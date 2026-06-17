# PLAN_H_UPDATE.md — Phase H: Observability + API guardrails

> Status: **todo** · Prio P3 · Effort M · Risk L. Spec: `SPECS_H_UPDATE.md`. Index: `PLAN_UPDATE.md`.
> When shipped, rename to `PLAN_H_UPDATE.md.done` (+ `SPECS_H_UPDATE.md.done`).

**Goal**: make the control plane operable and harder to abuse.

- **In**: Prometheus `/metrics` (run counts by state, queue depth, workers online, claim latency);
  OpenTelemetry traces (request → claim → worker events) behind an OTLP env; structured-log export;
  API rate-limiting on auth + webhook + discovery; repo clone size/time caps on discovery.
- **Out**: a bundled dashboards stack (ship the metrics, not Grafana).
- **Touches**: `main.py` (middleware), `observability/`, `scheduler` (gauges), `webhooks`,
  `environments/router.py` (discovery caps); new deps `prometheus-client`, `opentelemetry-*`.
- **Acceptance**: `/metrics` scrapeable; a trace spans a full run; discovery rejects an oversized repo.
