# TESTING.md — how Stackd is tested

> The test suite **is** the functional contract. This document explains the testing philosophy,
> how to run everything, and what each test file covers. It complements `DEV.md` (environment) and
> `SPECS.md` (the behaviour being verified). If a test and this doc disagree, the test wins —
> update the doc.

---

## 1. Philosophy

- **Real Postgres, never a DB mock** (CLAUDE §5). Unit/integration tests run against a real
  PostgreSQL 18 spun up with [testcontainers](https://testcontainers.com/); migrations are applied
  with Alembic before the suite. This catches what an in-memory fake never would: the
  `one_active_run_per_env` partial unique index, `FOR UPDATE … SKIP LOCKED`, `LISTEN/NOTIFY`,
  `uuidv7()`, JSONB `<@` containment, cascade FKs.
- **Real AWS surfaces are mocked with [moto](https://docs.getmoto.org/)** (S3 for the state
  backend and the saved plans, STS for `AssumeRoleWithWebIdentity`). Everything else is real.
- **Three rings of confidence:**
  1. **Unit** — pure logic with no I/O (e.g. `can_apply`, the masker, plan-summary parsing).
  2. **Integration** — the API exercised over HTTP via an in-process ASGI client against the real
     DB; a *simulated* worker drives runs by POSTing worker events.
  3. **End-to-end** — the **live** compose stack with a **real** worker container executing
     OpenTofu, driven over HTTP. This is the non-regression contract (DEV §7).
- **No state change outside `transition()`** and **audit in the same transaction** are not just
  runtime invariants — they're asserted (`test_ws_notify`, `test_audit`, every confirm/cascade test).

---

## 2. Running the tests

All commands go through the [Taskfile](https://github.com/fmiquel90/stackd/blob/main/Taskfile.yml):

```bash
task test     # API unit + integration (testcontainers) + front vitest
task e2e      # live end-to-end scenario (brings up the stack, seeds, runs api/e2e)
task lint     # ruff check + ruff format --check (api)
```

Under the hood:

| Task | Command | Needs |
|---|---|---|
| `task test` | `docker compose exec -T api uv run pytest` then `cd front && pnpm test` (`vitest run`) | compose `api` up, Docker for testcontainers |
| `task e2e` | `docker compose exec -T api uv run pytest e2e -v` (deps: `dev`) | full stack **+ a live worker** + the provisioned dev S3 |

`task test` does **not** run the worker suite. Run it directly — it needs no DB and no stack:

```bash
cd worker && uv run pytest
```

Run an API subset inside the api container:

```bash
docker compose -f deploy/docker-compose.dev.yml exec -T api uv run pytest tests/test_runs.py -v
docker compose -f deploy/docker-compose.dev.yml exec -T api uv run pytest tests/test_notifications.py::test_dispatcher_delivers_to_matching_targets
```

CI (`.github/workflows/ci.yml`) runs ruff, pytest and mypy for the API, ruff and pytest for the
worker, and typecheck + vitest + build for the front; `e2e.yml` runs the live scenario.

### pytest configuration (`api/pyproject.toml`)

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"                          # plain `async def test_*`, no decorator
asyncio_default_fixture_loop_scope = "session" # one event loop for the whole run …
asyncio_default_test_loop_scope = "session"    # … so the session-scoped engine isn't cross-loop
testpaths = ["tests"]                          # bare `pytest` only collects unit/integration
pythonpath = ["."]                             # make `app` importable from tests/ AND e2e/
```

`testpaths = ["tests"]` is why `api/e2e/` is **not** collected by `task test` — it lives outside
`tests/` so the testcontainers conftest never applies to it (the e2e wants the live DB, not a
throwaway one). `task e2e` points pytest at `e2e` explicitly.

---

## 3. Shared test infrastructure

### `api/tests/conftest.py` — the integration harness

| Fixture | Scope | What it does |
|---|---|---|
| `_database` | session | Starts a `postgres:18` testcontainer (asyncpg driver), sets `DATABASE_URL`, a JWT secret, a 32-byte encryption key, `STACKD_DEV_AUTH=true`, `STACKD_ALLOWED_DOMAINS`, an moto-friendly `AWS_REGION`, **disables the scheduler** (`STACKD_RUN_SCHEDULER=false`) so it can't fail runs mid-assertion, then runs `alembic upgrade head`. |
| `_seed` | session, autouse | Calls `app.seed.seed()` so the `default`/`demo` spaces and the tier catalog exist before any test. |
| `client` | function | An `httpx.AsyncClient` over `ASGITransport(app)` (in-process, no socket). **Deletes all `runs` before each test** so the global claim queue starts clean. Enters the app lifespan context. |

Tests that need the scheduler's tasks (worker loss, head polling, drift) call them directly.

### `api/tests/conftest_phase2.py` — run/worker helpers

| Helper | Purpose |
|---|---|
| `login(client, persona)` | Dev-login as `admin`/`alice`/`bob`; returns an `Authorization` header dict. |
| `make_stack(client, h, name)` | `POST /stacks`; returns the stack id. |
| `make_env(client, h, stack_id, name, tier, *, autodeploy, protected)` | `POST /stacks/{id}/environments`; returns the env id. |
| `register_worker(client, admin, pool_name, worker_name="w1")` | Creates a pool, registers a worker, returns the worker's auth header. |
| `event(client, wh, job_id, name, *, phase, result)` | `POST /worker/v1/jobs/{id}/events` — the lever that drives a run through phases from a *simulated* worker. |

### `api/e2e/conftest.py` — the live harness

| Fixture | Difference from the unit harness |
|---|---|
| `http` | Real `httpx.AsyncClient` over **HTTP** to `STACKD_E2E_BASE_URL` (default `http://localhost:8000`) — not ASGI. Talks to the running api container, which shares Postgres with the live worker. |
| `graph` | Builds a **throwaway copy** of the seeded demo graph through the public API, as admin, in the `demo` space (`e2e-<MMDDhhmmss>-<tag>-network` / `-app`: same repos, tiers, settings, variable-set attachments, dependency + mock). It reads the seed straight from the DB (the API hides mock values). Teardown ends what is still active, then deletes the copy if the scenario passed; otherwise it keeps it and names it in a warning. Every run starts from never-applied environments, so the scenario is re-runnable without `task reset` (DEV §7). |

### `worker/tests/` — no conftest

Worker tests import agent modules directly (`agent.main`, `agent.workspace`, `agent.runner`,
`agent.masking`, `agent.diagnostics`…) and fake the API client where a job is driven end to end.
No DB and no external network: the clone tests run real `git` against local repositories (one
dials a closed loopback port, `test_hook_secrets.py` serves one over a loopback HTTP server). Hooks
run for real (`sh -c`); tofu is a stand-in script, except in the one test that runs the real
OpenTofu when `tofu` is on the `PATH` (skipped otherwise).

---

## 4. Test map (what each file covers)

One line per file. The test names are descriptive: `uv run pytest --collect-only -q` lists them
all. Counts in parentheses include parametrized cases and drift as tests are added — see §5.

### API — unit & integration (`api/tests/`)

| File | Covers |
|---|---|
| `test_auth.py` (5) | Dev-login personas; `/refresh` CSRF double-submit; refresh rotation and reuse detection (the family is revoked); onboarding flag; unauthenticated `/me` → 401 problem+json. |
| `test_permissions.py` (9) | `can_apply` (pure): approver + tier set, writer refused, non-contiguous sets, empty set, custom tier, `can_destroy`; a space membership overrides the instance tiers, role and destroy right. |
| `test_spaces.py` (8) | Per-space RBAC: non-members blocked on env-scoped reads, `/graph` and space listing scoped to memberships, grant then revoke, create = instance admin, members listing = space admin, create in an unknown space → 404. |
| `test_tiers.py` (4) | Tier catalog: name charset (OIDC `sub` safety), `allowed_tiers` validated, deleting a tier strips grants, a tier in use can't be deleted. |
| `test_users.py` (3) | User listing is admin-only; the `@mention` directory is readable by anyone; tier/destroy changes are audited. |
| `test_audit.py` (2) | Mutations land in `/audit`, filter by action, CSV export columns; export is admin-only. |
| `test_runs.py` (9) | Run lifecycle; one active run per env under concurrency (`23505`); tier gate; four-eyes; autodeploy and `warn` forcing `unconfirmed`; confirm only from `unconfirmed`; mock block; repo token masked in the claim; the claim carries `cred_phase`. |
| `test_commit_pin.py` (6) | An unpinned run is pinned to the commit its plan checked out and the apply gets it; staleness then works for a manual run; a pinned SHA must match (`commit_mismatch` fails the run); a ref is re-pinned to its SHA; a malformed SHA → 400. |
| `test_saved_plan.py` (10) | The plan is stored encrypted and served only to the run's apply job; it can't be replaced after the plan phase; a blob moved to another run's key doesn't decrypt; no saved plan → nothing served; proposed runs save none; destroy runs plan with `-destroy`; 64 MiB cap; deleted once the run is terminal, kept if that transition rolls back. |
| `test_commands.py` (8) | Command runs: allowlist, read-only for writers, mutating needs `can_apply`, lifecycle + audit; mutating → `rw` token with the apply-window TTL, read-only → `ro` token with the 6 h TTL. |
| `test_promote.py` (5) | Promotion pins the source's applied commit (audited); needs an applied source; refuses cross-stack; distinct 404 for a missing source vs target; the source stays space-gated. |
| `test_deletion.py` (7) | Stack/env deletion refused (409) while a run is active, allowed once it is terminal; attachments, hooks and notification targets removed with their target (audited); an attach racing a deletion leaves no orphan (variable set, hook, notification target). |
| `test_variables_api.py` (12) | Layered resolution and provenance; sensitive values write-only; attached set can't be deleted; selectors on stack/env labels; tier attachments; cross-space attach refused; a tier attachment stays in the set's space; input discovery; backend config file round trip; `protected` forces no autodeploy. |
| `test_tfvars.py` (4) | `hcl` variables routed to the generated `.auto.tfvars`, everything else to JSON. |
| `test_secret_sources.py` (8) | External secret references: live value masked, static fallback + apply gate, `error` mode fails closed, break-glass override (needs apply right), source CRUD hides the bootstrap secret, in-use source can't be deleted, a fallback appearing at the apply claim is gated. |
| `test_proposed_secrets.py` (3) | A proposed run's claim carries no secret — no sensitive variable of either kind, no external reference (not fetched), nothing in `mask_values` or the provenance — and keeps the plan role and the repo token; a tracked run and a drift run still get every secret. |
| `test_dependencies.py` (10) | Mocks (used, block apply, real value wins, missing → fail), anti-cycle, cascade; `link-by-name` idempotent, skips hand-made and racing edges and in-request cycles; `/graph` hides edges outside the caller's spaces. |
| `test_hooks.py` (2) | Platform hook CRUD, tagged `platform` in the claim; writer gate. |
| `test_check_results.py` (4) | Every check result is kept with its stage in `check_results.results`, the warn/fail ones in `checks`: a plan failing on a check records it with the ones before; an apply's checks add to the plan's; a job failing on no check leaves them alone; a run recorded before `results` existed keeps its checks. |
| `test_apply_stage_hooks.py` (6) | `before_apply`/`after_apply` checks are recorded and audited (`run.check_warned`); an apply without checks keeps the plan's; a failing `after_apply` hook records the outputs, clears the drift and cascades nothing; a failed apply records no outputs and leaves the drift. |
| `test_state_backend.py` (13) | HTTP state backend (moto S3): LOCK/POST/GET/UNLOCK and serial regression; `ro` tokens can't write or unlock, `plan` tokens lock but can't write, proposed runs never lock; env scoping; TTL follows the credential phase; a terminal run's token can't write; UNLOCK and a locked POST need the holder's lock ID; import session (adopt, admin-only, managed state only). |
| `test_oidc.py` (4) | Issuer metadata + JWKS; the plan token's `sub`; apply uses the apply role; AssumeRole against moto STS. |
| `test_scheduler.py` (4) | The advisory lock survives a task's commit (dedicated connection); worker loss fails an executing run, waits the apply window for a mutating command, spares `unconfirmed`/`confirmed` runs. |
| `test_staleness.py` (11) | `stale` on the env API; head polling once per (repo, branch, credentials), surviving a failing repo, respecting the interval, bounded concurrency, never blocking the other scheduler tasks; refresh-head holds no transaction across git and is audited; a branch change resets the head. |
| `test_webhooks.py` (5) | Push updates the head and triggers; bad signature → 401; PR → proposed run; a branch-deletion push clears the head and triggers nothing; an env re-pointed meanwhile is skipped. |
| `test_vcs.py` (6) | GitHub post-back: owner/repo parsing, PR metadata persisted, PR close cancels proposed runs, status + one comment per run, a bad secret fails only its row, resync. |
| `test_drift.py` (6) | Drift runs enqueued only for envs with an applied commit; status flips and approvers (by space membership) are notified once; an apply clears drift; user runs are claimed first. |
| `test_repo_safety.py` (65) | Repo introspection: discovery reads only regular `.tf` files inside the clone (symlinks never escape), `project_root` containment, clone budget, cleanup off the event loop; the repo token percent-encoded in clone URLs and masked in every git error (both forms); git killed and reaped on timeout. |
| `test_notifications.py` (10) | Target CRUD + audit, test delivery, unsupported states, outbox enqueue and dispatch; SSRF: delivery pinned to the vetted IP (DNS rebinding), private IPs refused at create and at delivery, no redirects, body validation does no DNS. |
| `test_inbox.py` (6) | In-app notifications: approval requests to eligible approvers by space membership, terminal notifies the triggerer, replies and mentions, per-user feed, mark read, delete and clear. |
| `test_comments.py` (6) | Plan-review threads: anchors, one level of replies, edit/resolve/delete permissions. |
| `test_metrics_ratelimit.py` (8) | Token buckets (burst, per IP) in a bounded LRU (cap, eviction, concurrent new keys); `/metrics` exposition; `/health` needs auth, `/metrics` doesn't; discovery clone size budget. |
| `test_observability.py` (3) | `X-Request-ID`; `/health`; `/logs` admin-only and structured. |
| `test_worker_diagnostics.py` (2) | Diagnostics round trip over the heartbeat; admin-only. |
| `test_worker_heartbeat.py` (2) | `in_flight` drives busy/idle; a bodyless heartbeat keeps the worker online. |
| `test_worker_register.py` (1) | Re-registering under the same name reuses the worker row. |
| `test_ws_notify.py` (1) | Each `transition()` emits a `LISTEN/NOTIFY` signal on the run channel. |
| `test_seed.py` (1) | The demo seed is idempotent and yields exactly the DEV §7 graph. |

### API — end-to-end (`api/e2e/`, 1 scenario)

**`test_scenario.py::test_full_scenario`** — the DEV §7 narrative against the live stack and a real
worker running OpenTofu, on the `graph` copy (≈2 min). It drives, in order:

1. `bob` (writer) triggers `network/dev` → plan → `unconfirmed`.
2. `bob` is refused at confirm (apply needs approver+); `alice` confirms → apply → `finished`.
3. the cascade plans `app/dev` with the **real** upstream output (`used_mocks=false`).
4. `bob` triggers `app/prod` *before* `network/prod` is applied → plan uses the **mock** (`used_mocks=true`).
5. confirming the mock run is refused (409, `allow_mock_apply=false`); the run is discarded.
6. `bob` triggers `network/prod`, then is refused at confirm (writer can't apply).
7. `alice` confirms `network/prod` (approver, prod tier, 4-eyes vs `bob`) → `finished`.
8. the cascade re-runs `app/prod`, now with the **real** output (`used_mocks=false`).
9. the audit lists `run.confirmed` with `alice@dev.local` as the actor.

Every apply goes through the saved plan in S3, so the scenario needs the provisioned dev Garage.
The tier gate (an approver without `prod`) and staleness stay manual checks (DEV §3, §7).

### Worker (`worker/tests/`)

| File | Covers |
|---|---|
| `test_saved_plan.py` (10) | The plan job uploads the plan it showed, before the `after_plan` hooks; destroy runs plan with `-destroy`; the apply job applies exactly the saved plan, fails closed without a usable one, reports a stale plan clearly; the plan file is never written through a checked-out symlink; the API's refusal reaches the job error. |
| `test_commit_pin.py` (7) | An unpinned clone takes the env branch head; a pinned SHA off the default tip and a pinned ref are checked out exactly; an unknown commit fails at fetch; the plan reports its commit; an apply without a pinned commit fails closed. |
| `test_backend_env.py` (12) | The managed backend reaches tofu only via `TF_HTTP_*` (never `-backend-config`), with no lock address for a read-only job; inherited `TF_HTTP_*` dropped; unmanaged unchanged; repo hooks never get it, in plan, apply and command jobs; tofu's environment carries none of the agent's `STACKD_*` settings, in all three jobs. |
| `test_hook_failures.py` (35) | At every stage and from both sources: a `fail` hook fails the job at its stage, a `warn` one lets it finish and is reported; a failing `before_apply` hook stops the apply; a hook past `STACKD_HOOK_TIMEOUT_SECONDS` is killed and fails by its `on_failure`; a hook's background process is killed when the hook exits, whether it holds the hook's output or not; every check result is reported with its stage, in plan and apply jobs. |
| `test_hook_secrets.py` (8) | What a repo hook can read: no saved plan, raw plan JSON, tfvars, OIDC token file or repo token in the workspace; a masked `plan.json` at `after_plan` (platform hooks get the raw one); symlinks a repo plants never receive a secret; the clone leaves no token under `.git/`; repo hooks get only the allowlisted worker env, platform hooks none of the agent's `STACKD_*` settings; command runs still read the tfvars; the real OpenTofu end to end. |
| `test_exit_codes.py` (17) | A plan succeeds only on exit 0 or 2: an error, the watchdog's kill (124), a signal or any other code fails the job with the reason; an apply or command fails on any non-zero code; a plan without its JSON document (`show -json` failed or printed no JSON object) fails closed. |
| `test_command_run.py` (14) | A command's time budget follows `cred_phase`: apply budget when mutating, default cap when read-only, apply budget when missing (fail closed); an over-budget command fails with a clear message. |
| `test_authed_url.py` (45) | The repo token is percent-encoded into the clone URL (git dials the real host); other URLs unchanged; a failed clone masks both token forms. |
| `test_init_cmd.py` (6) | The `init` command for managed state and for unmanaged partial backends (file, inline map, both). |
| `test_security.py` (6) | Repo-hook env stripped of cloud credentials and sensitive values; the cleartext tripwire on plan and output JSON. |
| `test_masking.py` (8) | Masking (all secrets, longest first); platform hooks before repo hooks; plan summary and first error from tofu's JSON events; the masked plan JSON hides every sensitive value and keeps the rest, and a non-JSON document is masked as text. |
| `test_workspace.py` (6) | `hcl` tfvars written verbatim; token injection into HTTPS clone URLs. |
| `test_concurrency.py` (3) | The in-flight counter; `STACKD_MAX_CONCURRENT_JOBS` default and floor. |
| `test_runner_diag.py` (1) | A tofu diagnostic is expanded to its detail and location. |
| `test_diagnostics.py` (1) | The diagnostics bundle lists env var names, never values. |

### Front (`front/`)

- **vitest** (`pnpm test`): `StateBadge.test.tsx` (label alongside color, mocked/fallback
  modifiers), `ProvenanceBadge.test.ts` (provenance parsing) and `ProvenanceBadge.render.test.tsx`.
- **Playwright** (`pnpm e2e`, `e2e/happy-path.spec.ts`): one smoke test — dev login reaches the
  Stacks view — against a running dev stack (`PW_BASE_URL`, default `http://localhost:5173`). Not
  run by CI yet.
- **Ladle stories** (`pnpm ladle` / `pnpm ladle:build`) pin the identity components visually (the
  DESIGN §8 contract): `StateBadge`, `PhaseRail`, `ProvenanceBadge`, plus `BrandMark` and the base
  UI kit.

---

## 5. Counts

Roughly **279 API tests** (38 files), **179 worker tests** (15 files), **1 e2e scenario** and
**9 front unit tests**, parametrized cases included. The numbers move with every change; count them
instead of trusting this page:

```bash
cd api && uv run pytest --collect-only -q | tail -1      # unit + integration
cd worker && uv run pytest --collect-only -q | tail -1
```

---

## 6. Adding a test

- **Touching runs / permissions / cascade?** Add an integration test in `api/tests/` using the
  `client` fixture and the `conftest_phase2` helpers, and make sure `task e2e` still passes (it's
  the core contract — CLAUDE §6).
- **A new mutating endpoint?** Assert it writes an `audit_event` in the same transaction
  (invariant #2) — pattern it after `test_audit` / the CRUD tests.
- **Pure logic (no I/O)?** A plain unit test (see `test_permissions.py`, `worker/tests/test_masking.py`).
- **Don't mock the DB.** Use the real testcontainer; if you need AWS, use moto (see
  `test_state_backend.py`, `test_oidc.py`).
- **Isolation:** the `client` fixture wipes `runs` before each test; if your test seeds rows that
  the global `/graph` or cross-space queries would surface, clean them up in a `finally` (see
  `test_seed.py`).
