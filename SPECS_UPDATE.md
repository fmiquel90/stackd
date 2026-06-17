# SPECS_UPDATE.md — Technical specs for the post-MVP phases

> Companion to `docs/SPECS.md`; section `§U*` ↔ phase in `PLAN_UPDATE.md`. Folded into `SPECS.md`
> when each phase ships. Conventions unchanged: UUIDv7, `timestamptz` UTC `_at`, RFC 9457 errors,
> Pydantic ≠ ORM, state only via `transition()`, audit in the same tx, secrets never logged/returned.

---

## §U1 — VCS feedback loop (Phase A)

### Data model
```
runs  (add)
  pr_number        int  null        -- the PR that spawned a `proposed` run
  vcs_provider     text null        -- 'github' (enum-by-string; gitlab/bitbucket later)
  vcs_comment_id   bigint null      -- the posted PR comment, for idempotent edit
  vcs_head_sha     text null        -- commit the check is reported against (may differ from commit_sha)
```
GitHub App credentials (space- or instance-level — instance-level for MVP):
```
config (env)
  STACKD_GITHUB_APP_ID
  STACKD_GITHUB_APP_PRIVATE_KEY      -- PEM, mounted as a file/secret
  STACKD_GITHUB_API_URL  = https://api.github.com   -- GHE override
```
Fallback when no App configured: the stack's `repo_secret` (token) — must carry
`pull-requests:write` + `checks:write` (or `statuses:write`).

### Webhook ingestion (`webhooks/router.py`)
On `pull_request` (`opened`/`synchronize`/`reopened`): create the `proposed` run as today, **and**
persist `pr_number`, `vcs_provider='github'`, `vcs_head_sha = pr.head.sha`. On `closed`: best-effort
discard the open proposed run for that PR.

### Post-back (new `app/vcs/` module, driven by `transition()`)
A transition hook (same place that publishes to WS / fires platform hooks, §4.2) on a run with
`vcs_provider` set:
- **Check / commit status** on `vcs_head_sha`: `queued|planning → pending`, `unconfirmed → success`
  (neutral "plan ready, review"), `failed → failure`, `discarded/canceled → cancelled`.
  Prefer the **Checks API** (`POST /repos/{o}/{r}/check-runs`, then PATCH) when on a GitHub App;
  fall back to the **Status API** (`POST /repos/{o}/{r}/statuses/{sha}`) with a PAT.
- **PR comment**: one comment per run, **edited in place** (`vcs_comment_id`): the `+a ~c −d`
  summary, mocked/fallback badges, check results, and a deep link to `/runs/{id}`. Created on first
  plan completion; updated on each subsequent transition.
- Auth: GitHub App → installation token (cached, 1h TTL) resolved from the repo owner; else the
  stack PAT. All network calls are best-effort + retried; a VCS failure **never** fails the run
  (logged + surfaced as a run warning).

### Endpoints
- `POST /api/v1/webhooks/github` — unchanged contract, now also persists PR metadata.
- `POST /api/v1/runs/{id}/vcs/resync` (writer) — re-post the comment/check (manual recovery).

### Open decisions (defaults in **bold**)
- **GitHub App** (clean multi-repo, no per-stack PAT scope juggling) — PAT path kept as fallback.
- **One comment edited in place** (not a new comment per event) — less PR noise.

### Invariants
Post-back is a side-effect of `transition()`, never a separate source of truth; a VCS outage leaves
the run correct. Sensitive plan values stay masked in the comment (reuse the artifact masking).

---

## §U2 — Drift detection (Phase B)

### Data model
```
environments  (add)
  drift_status          text  default 'unknown'  -- unknown | in_sync | drifted | error
  last_drift_checked_at timestamptz null
  drift_run_id          uuid null                -- the proposed run that last detected drift
  drift_check_enabled   bool default true
config (env)
  STACKD_DRIFT_INTERVAL_SECONDS = 21600  (6h)     -- per-env minimum spacing
```
New notification/inbox kind: `drift_detected`.

### Scheduler task (`scheduler/tasks.py`, advisory-locked like the others)
A `detect_drift` task on the 10s loop, gated by `last_drift_checked_at + interval`:
for each `drift_check_enabled` env with no active run, enqueue a **read-only proposed run**
(`RunType.proposed`, refresh-only plan). On completion:
- plan has changes → `drift_status='drifted'`, `drift_run_id`, emit `drift_detected` once per
  transition into drift (debounced — no repeat notification while it stays drifted);
- no changes → `in_sync`; plan errored → `error`.
A successful **apply** sets `in_sync` and clears `drift_run_id`.

### Worker
No new job type — reuse the `proposed` plan (`plan -refresh-only` or a normal plan; record only the
summary). The drift run must not consume a worker slot ahead of user runs (lower claim priority).

### Front
A `drift` chip on `/stacks` env cells and the env header (DESIGN §3.2 spectrum: neutral, with a
label — not a state colour). A filter "show drifted".

### Invariants
Drift runs are read-only; **never auto-apply**. Concurrency unchanged (one active run per env — a
drift run is skipped if a run is active).

---

## §U3 — Security hardening (Phase C)

### Masking (`worker/agent/masking.py`, `claim.py`)
- Feed the masker **all** sensitive values, including those routed to `sensitive_env` (env-kind
  secrets) — today only tfvars/known mask_values are guaranteed; assert env secret values are in
  `mask_values`.
- **Cleartext tripwire**: after a phase, if a known sensitive value appears verbatim in an artifact
  or output it shouldn't (e.g. a non-sensitive output echoing a secret), mark the run with a
  `secret_leak_suspected` warning (don't hard-fail by default; configurable).
- Don't stream a raw `show` of sensitive attributes — rely on terraform's `(sensitive value)`.
- Documented residual limit (kept): value-based masking can't catch a *transformed* secret
  (base64/substring). The tripwire narrows, doesn't eliminate, this.

### Runner trust model (`worker/agent/runner.py`, `main.py`, deploy)
- **`docker` runner contract** (prod): one ephemeral container per job, image carries **no
  long-lived cloud creds**; the OIDC token is written to a 0600 file, mounted read-only, deleted in
  `finally`; workspace removed after the job; optional egress allowlist via the run network.
- **Repo hooks run untrusted**: `sh -c <repo command>` receives `repo_env` only — **no `AWS_*`,
  no sensitive vars, no cloud creds** (already the design, §8.3) — add a regression test asserting it.
- Dev (`local` runner + mounted `~/.aws`) is explicitly "trusted dev only" — documented, never prod.

### Invariants
Reinforces §13 (secrets) and §8.3 (hook isolation). No change to `can_apply`/four-eyes.

---

## §U4 — HCL-syntax variables (Phase D)

Builds on the shipped `_tfvar_value` (JSON parse of `hcl` values into `*.auto.tfvars.json`).
- The claim payload tags which terraform vars are `hcl` (already known server-side).
- **Worker**: for `hcl` vars, instead of JSON-encoding, write a generated **`zzz_stackd.auto.tfvars`
  (HCL)** with `name = <raw value>` (verbatim), so real HCL syntax (`{ a = "b" }`, function calls)
  parses natively. Non-hcl vars stay in `stackd.auto.tfvars.json`. Both files auto-load; HCL file
  name sorts last so it wins ties deterministically.
- Masking still applies to the HCL file content (sensitive hcl values).

### Invariants
Resolution order (§3.4) unchanged — this is purely *how* a resolved value is written to disk.

> Note: dependency **outputs** are already typed (`EnvOutput.value` is JSONB; `capture_outputs`
> stores native values) — they are **not** affected and need no change.

---

## §U5 — Worker concurrency (Phase E)

### Config
```
STACKD_MAX_CONCURRENT_JOBS = 1   (worker; today's behaviour)
```
### Claim loop (`worker/agent/main.py`)
- The poll loop maintains up to `MAX_CONCURRENT_JOBS` in-flight jobs, each on its own thread (the
  heartbeat thread is already independent). It claims while it has a free slot, sleeps on the
  long-poll otherwise.
- `claim` stays one-run-at-a-time on the wire; the API's `SELECT … FOR UPDATE SKIP LOCKED` (§7.2)
  and the one-active-run-per-env partial unique index (§3.5) already make concurrent claims safe.
- Heartbeat reports `busy`/`idle` from the in-flight count; the worker advertises capacity so the
  scheduler/queue can reason about it (optional `capacity` field on heartbeat).

### Invariants
One active run per environment is unchanged — concurrency is **across environments** only. State
backend locking (§11.2) already guards same-env races.

---

## §U6 — RBAC granularity + multi-space (Phase F)

### Data model
```
space_memberships
  id uuid PK, space_id FK, user_id FK,
  role enum(reader, writer, approver, admin),
  allowed_tiers text[] ,            -- per-space tier ceiling (overrides the global user.allowed_tiers)
  can_destroy bool default false,
  UNIQUE (space_id, user_id)
```
`users.role`/`allowed_tiers`/`can_destroy` become **instance defaults**; the effective permission is
the space membership when present. `can_apply(user, env)` (§ invariant #4) gains a space lookup:
`role∈{approver,admin}` **for that space** AND `env.tier ∈ membership.allowed_tiers`.

### Scoping
Every list/get/mutate resolves the active space from the resource (stack→space, env→stack→space) and
checks membership. The implicit `get_default_space` is removed; a migration creates one membership
per existing user for the existing space (no behaviour change for current data).

### API / front
- `GET /api/v1/spaces`, `POST /spaces` (admin), `POST/DELETE /spaces/{id}/members`.
- Front shell gains a **space switcher**; all queries key by space id.

### Invariants
Four-eyes (`tiers.requires_four_eyes`, fail-closed) and the tier-as-set model (§2.4) are unchanged —
just scoped per space. Highest blast radius: land last, behind a full `task e2e` permission pass.

---

## §U7 — Front: splitting + tests (Phase G)

- **Bundle**: route-level `React.lazy`/`Suspense`; Vite `build.rollupOptions.output.manualChunks`
  isolating `@xyflow/react`+`dagre` (graph) and `react-virtuoso`+`anser` (logs). Target initial
  chunk < 250 KB gz (today ~200 KB gz in one ~650 KB file).
- **Tests** (vitest is already a dep, unused): Testing-Library unit tests for the identity
  components (`PhaseRail`, `StateBadge`, `PlanDiff`, `ProvenanceBadge`) and for pure logic
  (variable resolution provenance, `can_apply` gating in the UI). One **Playwright** happy path
  (dev-login → trigger plan → confirm) added as a CI job against `task dev`.
- No product behaviour change.

---

## §U8 — Observability + API guardrails (Phase H)

### Metrics — `GET /metrics` (Prometheus, `prometheus-client`)
`stackd_runs_total{state}`, `stackd_queue_depth`, `stackd_workers_online`, `stackd_claim_latency_seconds`,
`stackd_run_duration_seconds{phase}`, `stackd_webhook_total{result}`. Scheduler updates gauges on its tick.

### Tracing (OpenTelemetry, OTLP exporter behind `STACKD_OTLP_ENDPOINT`)
Spans: HTTP request → run `transition` → claim → worker phase events (linked via `run_id`). No-op
when the endpoint is unset.

### Guardrails
- Rate-limit `auth/*`, `webhooks/github`, and `discover-inputs` (token bucket per IP / per token).
- Discovery clone caps: `--depth 1` already; add a **size/time budget** (reject repos over N MB or
  clones over the existing 30 s) and a max `.tf` count parsed.

### Invariants
`/metrics` and traces never include secret values or tfvars; metrics are cardinality-bounded
(label by state/phase, never by run id).

---

## Migrations introduced (summary)

| # (next free) | Phase | Change |
|---|---|---|
| 0021 | A | `runs.pr_number, vcs_provider, vcs_comment_id, vcs_head_sha` |
| 0022 | B | `environments.drift_status, last_drift_checked_at, drift_run_id, drift_check_enabled` |
| 0023 | F | `space_memberships` table + backfill |

(Phases C/D/E/G/H need no schema change.)

## Open decisions to confirm

1. **§U1** GitHub App (recommended) vs PAT-only for post-back.
2. **§U3** cleartext tripwire: warn (default) vs hard-fail the run.
3. **§U6** per-stack grants now, or per-space only for this phase (per-stack later)?
