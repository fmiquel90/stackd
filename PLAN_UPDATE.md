# PLAN_UPDATE.md — Post-MVP improvement plan

> Companion to `docs/PLAN.md`. Captures the work agreed after the "sincere analysis" review.
> Technical detail lives in `SPECS_UPDATE.md` (sections `§U*`). Same invariants as `CLAUDE.md`
> apply (state only via `transition()`, audit in the same tx, no secret in logs, `can_apply`).
>
> Nothing here widens the MVP definition retroactively — these are explicit, scoped phases. Each is
> shippable on its own and gated by `task test` + `task e2e`.

## 0. Why this exists

The MVP is architecturally sound (single source of truth, stateless workers, OIDC dynamic creds,
real-DB tests). The gaps that block adoption / enterprise trust are, in order:

1. **It lives beside Git, not inside it** — a PR triggers a plan but nothing comes back to the PR.
2. **No drift detection** — table-stakes for the category.
3. **Security sharp edges** — substring-only secret masking; untrusted repo code on the worker.
4. **Correctness/UX** — HCL-syntax variables, worker throughput, onboarding surface.
5. **Scale/enterprise** — per-stack RBAC, real multi-space, platform observability.

## 1. Phases (priority · effort · risk)

| Phase | Theme | Prio | Effort | Risk |
|---|---|---|---|---|
| **A** | VCS feedback loop (PR comment + commit checks) | P1 | M | M (external API, tokens) |
| **B** | Drift detection (scheduled plan + badge + notify) | P1 | S–M | L |
| **C** | Security hardening (masking + runner trust) | P1 | M | M (security-sensitive) |
| **D** | Correctness: HCL-syntax tfvars | P2 | S | L |
| **E** | Worker concurrency (N jobs / worker) | P2 | M | M (touches the claim loop) |
| **F** | RBAC granularity + real multi-space | P2 | L | M (touches authz everywhere) |
| **G** | Front: code-splitting + test foundation | P2 | S–M | L |
| **H** | Platform observability + API guardrails | P3 | M | L |
| **I** | Later: module registry, run tasks, SSO/SAML | P3 | L | — |

Effort: S ≈ 1–2 days, M ≈ 3–5 days, L ≈ 1–2 weeks (single dev).

## 2. Phase detail

### Phase A — VCS feedback loop  (SPECS_UPDATE §U1)
**Goal**: a PR shows the plan result *in GitHub* (comment + commit status / check), closing the
review loop. Reuses the existing `proposed` run created on `pull_request` (`webhooks/router.py`).
- **In**: store the PR number/head SHA on the run; a post-back service that comments the
  `+a ~c −d` summary + run link and sets a commit **check/status**; idempotent comment update
  (one comment per run, edited on state change); GitHub App auth (install token) with PAT fallback.
- **Out**: GitLab/Bitbucket (interface designed for it, GitHub first); inline plan-line comments.
- **Touches**: `webhooks/`, `runs/` (transition hook on proposed runs), new `vcs/` module, config
  (GitHub App id/key), 1 migration (`runs.pr_number`, `runs.vcs_*`).
- **Acceptance**: open a PR on a fixture repo → a check appears, a comment is posted, and it updates
  to finished/failed when the proposed run resolves. e2e extended with a mock VCS server.

### Phase B — Drift detection  (SPECS_UPDATE §U2)
**Goal**: detect when real infrastructure diverges from the last applied state.
- **In**: a scheduler task that, per `managed`/tracked env on a configurable cadence, runs a
  **read-only `proposed` plan** and records `environments.drift_status` + `last_drift_checked_at`;
  a `drift` badge on `/stacks` and the env; an inbox/notification on newly-drifted envs.
- **Out**: auto-remediation (never auto-apply drift).
- **Touches**: `scheduler/tasks.py` (new advisory-locked task), `environments` model + schema,
  `notifications`/`inbox` (new kind), front (badge), 1 migration.
- **Acceptance**: mutate state out-of-band in e2e → drift task flips the env to `drifted` and emits
  a notification; a successful apply clears it.

### Phase C — Security hardening  (SPECS_UPDATE §U3)
**Goal**: shrink the secret-leak surface and pin down the untrusted-code trust model.
- **In**:
  - **Masking**: keep value masking but (a) also feed env-var secret *values* to the masker,
    (b) add a "suspicious cleartext" detector that fails/flags a run if a known sensitive value
    appears un-transformed in an output it shouldn't, (c) prefer Terraform-native `sensitive`
    (don't stream raw `show` of sensitive attributes).
  - **Runner trust**: formalize the `docker` runner contract — ephemeral per-job container, **no
    long-lived creds baked in**, OIDC token file mounted read-only and removed after, optional
    egress allowlist; document that repo hooks (`sh -c`) run untrusted and never receive
    cloud/secret env (already true for repo hooks — assert with a test).
- **Out**: full sandbox/microVM (gVisor/firecracker) — documented as a later option.
- **Touches**: `worker/agent/{masking,runner,main}.py`, deploy docs, SPECS §5/§8/§13.
- **Acceptance**: tests prove (a) env secret values are masked in logs, (b) a repo hook gets no
  `AWS_*`/sensitive env, (c) the docker runner leaves no creds/workspace behind.

### Phase D — HCL-syntax variables  (SPECS_UPDATE §U4)
**Goal**: support real HCL values (`{ a = "b" }`, expressions) for `hcl` variables, not just JSON.
- **In**: for `hcl` terraform variables, the worker writes a generated **`.auto.tfvars` (HCL)**
  file (`name = <raw value>`) instead of forcing the value through JSON; JSON path kept for
  non-hcl. (Builds on the `_tfvar_value` JSON parse already shipped.)
- **Out**: validating the HCL server-side (terraform validates at plan).
- **Touches**: `workers/claim.py` (mark hcl vars), `worker/agent/{main,workspace}.py`.
- **Acceptance**: an `hcl` var `{ a = "b" }` reaches terraform as an object; `["a","b"]` as a list.

### Phase E — Worker concurrency  (SPECS_UPDATE §U5)
**Goal**: a worker runs several jobs at once (throughput) without breaking the per-env single-active
invariant.
- **In**: `STACKD_MAX_CONCURRENT_JOBS` (default 1 = today); the claim loop dispatches each job to a
  bounded worker pool/thread; heartbeat already independent (done). One active run per env stays
  enforced by the partial unique index (§3.5) — concurrency is across *different* envs.
- **Out**: cancelling a running job mid-flight (separate `cancel_job` command, later).
- **Touches**: `worker/agent/main.py` (loop), claim semantics (claim N).
- **Acceptance**: e2e variant with 2 envs + 1 worker, `MAX_CONCURRENT_JOBS=2` → both plan in
  parallel; status reporting (idle/busy) reflects in-flight count.

### Phase F — RBAC granularity + real multi-space  (SPECS_UPDATE §U6)
**Goal**: move from space-wide role+tier to per-space (and optionally per-stack) grants, and wire
spaces end-to-end (drop the implicit `get_default_space`).
- **In**: a `space_memberships` table (user × space × role); space scoping on every list/mutation;
  space selector in the UI; migration that backfills the existing single space.
- **Out**: full OPA policy engine (stays Phase 7 in PLAN.md); per-resource ACLs beyond stack/space.
- **Touches**: `auth/deps.py`, every router's scoping, `spaces/`, front shell (space switcher),
  migrations. **Highest blast radius — do after A–E.**
- **Acceptance**: a user in space X can't see/mutate space Y; tier ceiling still enforced per space.

### Phase G — Front: splitting + tests  (SPECS_UPDATE §U7)
**Goal**: kill the single ~650 KB bundle and the zero-test situation (vitest is configured, unused).
- **In**: route-level `React.lazy` + `manualChunks` (react-flow/dagre, anser/virtuoso isolated);
  vitest + Testing Library tests for the identity components and the resolution/permission logic;
  one Playwright happy path (dev-login → plan → confirm) in CI.
- **Touches**: `front/` only; CI adds a Playwright job.
- **Acceptance**: initial JS chunk < 250 KB gz; `task test` runs front unit tests; Playwright green.

### Phase H — Observability + API guardrails  (SPECS_UPDATE §U8)
**Goal**: make the control plane operable and harder to abuse.
- **In**: Prometheus `/metrics` (run counts by state, queue depth, worker online, claim latency);
  OpenTelemetry traces (request → claim → worker events) behind an OTLP env; structured-log export;
  API rate-limiting on auth + webhook + discovery; repo clone size/time caps on discovery.
- **Touches**: `main.py` (middleware), `observability/`, `scheduler` (gauges), `webhooks`,
  `environments/router.py` (discovery caps).
- **Acceptance**: `/metrics` scrapeable; a trace spans a full run; discovery rejects an oversized repo.

### Phase I — Later (not scheduled now)
Module registry (read-only), outbound run-tasks/webhooks enrichment, SSO/SAML. Kept on the roadmap,
not specced in detail until A–H land.

## 3. Recommended sequence

```
A ─┐
B ─┼─ ship together (the "now part of the workflow" story)
C ─┘  (security can run in parallel — different files)
D, G  quick wins, anytime (low risk, parallelizable)
E     after C (touches the same worker loop)
F     last of the P1/P2 block (highest blast radius)
H     ongoing / before any real prod exposure
```

Two decisions to confirm before Phase A (see SPECS_UPDATE §U1): **GitHub App vs PAT** for posting
back, and **one comment edited in place vs append**. Defaults proposed there.

## 4. Cross-cutting rules

- Every phase: a migration per schema change (never edit a merged one); `task e2e` green if it
  touches runs/permissions/state; docs (`SPECS.md`/`DESIGN.md`) updated, not just these `_UPDATE`
  files (which are folded back into the authoritative docs once a phase ships).
- No new heavy dependency without noting it here (Phase H: `prometheus-client`, `opentelemetry-*`;
  Phase G: `@playwright/test`, `@testing-library/react`).
