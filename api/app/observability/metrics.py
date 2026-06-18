from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

# Prometheus metrics (SPECS §H). Cardinality is bounded — labels are state/phase/result only, never
# a run id. Gauges are refreshed from the DB at scrape time (see render_metrics); counters and
# histograms are incremented at the relevant code points.

runs_total = Gauge("stackd_runs_total", "Runs by state (snapshot at scrape)", ["state"])
queue_depth = Gauge("stackd_queue_depth", "Runs waiting to be claimed (queued + confirmed)")
workers_online = Gauge("stackd_workers_online", "Workers seen within the offline window")

claim_latency = Histogram(
    "stackd_claim_latency_seconds",
    "Time a run waited in queue before a worker claimed it",
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 300),
)
run_duration = Histogram(
    "stackd_run_duration_seconds",
    "Wall-clock from claim to terminal state, by job phase",
    ["phase"],
    buckets=(5, 15, 30, 60, 120, 300, 600, 1800, 3600),
)
webhook_total = Counter("stackd_webhook_total", "VCS webhooks received", ["result"])


def render() -> tuple[bytes, str]:
    """Serialize the current registry for the /metrics endpoint."""
    return generate_latest(), CONTENT_TYPE_LATEST
