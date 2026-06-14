# TESTING.md — how Stackd is tested

> The test suite **is** the functional contract. This document explains the testing philosophy,
> how to run everything, and what every test asserts. It complements `DEV.md` (environment) and
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
  backend, STS for `AssumeRoleWithWebIdentity`). Everything else is real.
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
task test     # unit + integration (testcontainers) + front vitest
task e2e      # live end-to-end scenario (brings up the stack, seeds, runs api/e2e)
task lint     # ruff check + ruff format --check
```

Under the hood:

| Task | Command | Needs |
|---|---|---|
| `task test` | `docker compose exec -T api uv run pytest` then `cd front && pnpm test --run` | compose `api` up |
| `task e2e` | `docker compose exec -T api uv run pytest e2e -v` (deps: `dev`) | full stack **+ a live worker** |

Run a subset directly inside the api container:

```bash
docker compose -f deploy/docker-compose.dev.yml exec -T api uv run pytest tests/test_runs.py -v
docker compose -f deploy/docker-compose.dev.yml exec -T api uv run pytest tests/test_notifications.py::test_dispatcher_delivers_to_matching_targets
```

Worker tests are standalone (no DB):

```bash
cd worker && uv run pytest
```

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
| `_seed` | session, autouse | Calls `app.seed.seed()` to ensure the `default`/`demo` spaces exist before any test. |
| `client` | function | An `httpx.AsyncClient` over `ASGITransport(app)` (in-process, no socket). **Deletes all `runs` before each test** so the global claim queue starts clean. Enters the app lifespan context. |

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
| `envs` | Resolves the seeded demo env ids by `<stack>/<env>` name **straight from the DB** (`SessionLocal`), because the demo stacks live in the `demo` space which the public API doesn't list. Asserts the demo graph was seeded. |

### `worker/tests/` — no conftest

Worker tests import agent modules directly (`agent.diagnostics`, `agent.masking`, `agent.main`);
no DB, no network.

---

## 4. Test map (what each test asserts)

### API — unit & integration (`api/tests/`, 61 tests)

**`test_auth.py` (5)** — auth flow & session security
- `test_dev_login_personas` — persona login returns the user (role/tier) + a usable access token.
- `test_refresh_requires_csrf` — `/refresh` rejects a request missing the `X-CSRF-Token` (double-submit).
- `test_refresh_rotation_and_reuse_detection` — each refresh rotates the token; replaying an old one → 401 and the whole family is revoked.
- `test_onboarding_flag_persists` — `POST /auth/me/onboarded` persists `onboarded=true`.
- `test_unauthenticated_me` — `GET /auth/me` unauthenticated → 401 problem+json.

**`test_audit.py` (2)** — the who-did-what journal
- `test_audit_filter_and_export` — mutations land in `/audit`; filtering by action works; CSV export has the right columns.
- `test_audit_export_admin_only` — a non-admin gets 403 on export.

**`test_permissions.py` (5)** — `can_apply` logic (pure, no HTTP)
- `test_approver_prod_can_apply_prod` — approver with `max_apply_tier=prod` may apply prod.
- `test_writer_cannot_confirm` — a writer is refused (apply needs approver+); the reason mentions the role.
- `test_apply_everywhere_except_prod` — an approver capped at `staging` may apply dev/staging, not prod.
- `test_no_tier_cannot_apply` — `max_apply_tier=None` → denied.
- `test_destroy_requires_can_destroy` — destroy needs `can_destroy` on top of `can_apply`.

**`test_runs.py` (7)** — the run lifecycle & its gates
- `test_full_run_lifecycle` — trigger → claim → plan events → unconfirmed → confirm → apply → finished.
- `test_one_active_run_per_env_under_concurrency` — two workers claim the same env → exactly one wins, the loser is netted by `23505`.
- `test_confirm_blocked_by_tier` — confirming above one's `max_apply_tier` → 403.
- `test_four_eyes_on_prod` — the triggerer can't confirm their own prod run; a second person can.
- `test_autodeploy_and_warn_forces_unconfirmed` — autodeploy auto-confirms, but a `warn` check forces `unconfirmed`.
- `test_confirm_rejected_when_not_unconfirmed` — confirming a non-`unconfirmed` run → 409.
- `test_mock_block` — a run with `used_mocks=true` can't be confirmed (reason mentions the mock).

**`test_commands.py` (4)** — ad-hoc command runs (§4.3)
- `test_command_allowlist_rejected` — a non-allowlisted command (e.g. `apply`) → 400.
- `test_readonly_command_allowed_for_writer` — a writer may run a read-only command (`output`) → 201, `type=command`.
- `test_mutating_command_requires_can_apply` — a writer is refused `state rm` (403); an admin is allowed.
- `test_command_run_lifecycle` — claim carries `phase=command` + the subcommand; `running → finished`; audited (`run.command_triggered`/`run.command_executed`).

**`test_dependencies.py` (5)** — cross-env outputs, mocks, cascade
- `test_mock_used_and_blocks_apply` — no real upstream output → the mock is injected, `used_mocks=true`, apply blocked.
- `test_real_value_beats_mock` — a real output overrides the mock.
- `test_missing_upstream_without_mock_fails_run` — no output and no mock → the run fails (`missing_upstream_output`).
- `test_anti_cycle` — creating a dependency cycle → 422.
- `test_cascade_triggers_downstream` — an upstream apply cascades a downstream run carrying the real output.

**`test_hooks.py` (2)** — platform hooks
- `test_platform_hook_crud_and_appears_in_claim` — a created hook shows up in the claim payload tagged platform; PATCH/DELETE work.
- `test_hooks_require_writer` — writer may manage hooks (reader is gated by the permission model).

**`test_state_backend.py` (6)** — Terraform HTTP state backend + import (moto S3)
- `test_state_lock_post_get_unlock` — LOCK→409-on-second→POST→serial-regression-409→GET→UNLOCK.
- `test_readonly_token_cannot_write` — a `ro` state token (proposed runs) can't POST state → 403.
- `test_state_token_scoped_to_env` — a token scoped to env X can't touch env Y → 403.
- `test_import_session_adopts_existing_state` — an admin import session mints a backend config; the LOCK/POST/UNLOCK migration stores a `state_version` with no originating run (§11.4).
- `test_import_session_requires_admin` — a non-admin can't mint an import session → 403.
- `test_import_session_requires_managed_state` — importing into a `managed_state=false` env → 409.

**`test_oidc.py` (4)** — workload credentials (§10)
- `test_issuer_metadata_and_jwks` — discovery doc + JWKS endpoints serve RSA keys.
- `test_claim_payload_signs_plan_token` — a plan claim carries a signed token with `sub=run:<tier>:<stack>:plan`, verifiable against JWKS.
- `test_apply_uses_apply_role` — apply uses the apply role ARN, not the plan one.
- `test_assume_role_against_moto` — the cloud-integration test endpoint assumes a role under moto STS.

**`test_webhooks.py` (3)** — GitHub ingestion (§5)
- `test_push_updates_head_and_triggers` — a valid-HMAC push advances `head_sha` and triggers a tracked run.
- `test_invalid_signature_rejected` — a bad signature → 401.
- `test_pull_request_creates_proposed_run` — a PR opens a proposed (plan-only) run at the PR head sha.

**`test_users.py` (2)** — user administration
- `test_non_admin_cannot_list_users` — listing users is admin-only.
- `test_admin_updates_tier_is_audited` — changing tier/destroy emits the matching audit actions.

**`test_variables_api.py` (4)** — 5-layer resolution & secrets
- `test_five_layer_resolution_and_provenance` — env > env-attached set > stack > auto-attach set, each with the right provenance and `TF_VAR_` injection name.
- `test_sensitive_variable_is_write_only` — sensitive values are masked everywhere in API responses.
- `test_attached_set_delete_requires_detach` — deleting an attached set → 409 until detached.
- `test_protected_env_forces_no_autodeploy` — `protected=true` forces `autodeploy=false`.

**`test_notifications.py` (4)** — outbound notifications
- `test_notification_target_crud` — create/list/patch/delete a target (default `on_states=[unconfirmed, failed]`); each mutation audited.
- `test_rejects_unsupported_state` — an `on_states` value that never fires → 422.
- `test_outbox_enqueued_on_transition` — a run reaching `unconfirmed` enqueues exactly one outbox row.
- `test_dispatcher_delivers_to_matching_targets` — only targets whose `on_states` match are delivered; the row is marked sent (second drain is a no-op).

**`test_observability.py` (3)** — health & logs
- `test_request_id_header` — every response carries `x-request-id`.
- `test_health` — `/health` reports db + worker + run counts.
- `test_logs_admin_only_and_structured` — `/logs` is admin-only and returns structured records.

**`test_worker_diagnostics.py` (2)** — the downward command channel
- `test_diagnostics_command_roundtrip` — an admin queues diagnostics; the worker picks it up on heartbeat, posts a read-only bundle (tools, disk, env var *names*), the admin reads the result.
- `test_diagnostics_admin_only` — non-admins can't request diagnostics.

**`test_scheduler.py` (1)** — background reconciliation
- `test_worker_lost_fails_active_run` — a run on a worker whose heartbeat lapsed is transitioned to `failed (worker_lost)`.

**`test_ws_notify.py` (1)** — the live-update bridge
- `test_transition_emits_listen_notify` — each `transition()` emits a `LISTEN/NOTIFY` signal on the run channel with the new state.

**`test_seed.py` (1)** — demo data
- `test_seed_demo_is_idempotent` — `seed_demo()` run twice yields exactly the DEV §7 graph (2 stacks, 4 envs, 2 deps, the protected/4-eyes prod env, the `local` pool) with no duplicates; cleans up after itself so `/graph` stays unpolluted for other tests.

### API — end-to-end (`api/e2e/`, 1 scenario)

**`test_scenario.py::test_full_scenario`** — the DEV §7 narrative against the live stack + real
worker running OpenTofu (≈2 min). It drives, in order:

1. `bob` (writer) triggers `network/dev` → plan → `unconfirmed`.
2. `bob` is refused at confirm (apply needs approver+); `alice` confirms → apply → `finished`.
3. the cascade plans `app/dev` with the **real** upstream output (`used_mocks=false`).
4. `bob` triggers `app/prod` *before* `network/prod` is applied → plan uses the **mock** (`used_mocks=true`).
5. confirming the mock run is refused (409, `allow_mock_apply=false`); the run is discarded.
6. `bob` triggers `network/prod`, then is refused at confirm (writer can't apply).
7. `alice` confirms `network/prod` (approver, prod tier, 4-eyes vs `bob`) → `finished`.
8. the cascade re-runs `app/prod`, now with the **real** output (`used_mocks=false`).
9. the audit lists `run.confirmed` with `alice@dev.local` as the actor.

> **Note (spec drift):** DEV §7 narrates *bob* confirming and a *tier* refusal. The implemented
> permission model (CLAUDE §4 #4) gates confirm on role ∈ {approver, admin}, so a writer is refused
> for **role**, and no seeded persona is an approver-with-sub-prod-tier. The e2e is faithful to the
> implementation. Reconcile DEV.md, or add a 4th persona, to demo the tier gate.

### Worker (`worker/tests/`, 5 tests)

**`test_diagnostics.py` (1)**
- `test_diagnostics_exposes_env_names_not_values` — the bundle lists env var **names** but never their values.

**`test_masking.py` (4)**
- `test_masker_replaces_all_secrets` — every known secret in a string is replaced.
- `test_masker_longest_first` — overlapping secrets are masked longest-first (no partial leaks).
- `test_merge_hooks_platform_before_repo` — platform hooks are merged ahead of repo hooks (non-bypassable).
- `test_plan_summary_counts_actions` — `plan.json` actions are counted into add/change/destroy.

### Front (`front/`)

`pnpm test --run` (vitest) is wired but there are currently no component test files. The
identity components are instead pinned by **Ladle stories** (the DESIGN §8 visual contract):
`pnpm ladle` / `pnpm ladle:build` — `StateBadge`, `PhaseRail`, `ProvenanceBadge`.

---

## 5. Counts

| Location | Files | Tests |
|---|---|---|
| `api/tests` | 18 | 61 |
| `api/e2e` | 1 | 1 (multi-step scenario) |
| `worker/tests` | 2 | 5 |
| **Total** | **21** | **67** |

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
