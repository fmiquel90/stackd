# API

Stackd exposes two REST surfaces under versioned prefixes:

- **`/api/v1`** — the human/automation API (auth, spaces, stacks, environments, variables, runs,
  rollouts, hooks, dependencies, notifications, audit, state management, workers, health, logs).
- **`/worker/v1`** — the worker (agent) API: register, heartbeat, claim, events, logs, artifacts
  (including the saved plan the apply job downloads), command results. Workers authenticate with a
  pool/worker token; humans never call this.

Outside these prefixes: the Terraform HTTP state backend (`/state/v1/{env_id}`, used by tofu with
a per-job state token), the OIDC issuer (`/.well-known/openid-configuration`, `/oidc/jwks`), and
two unauthenticated probes, `/healthz` and `/metrics` (Prometheus).

Errors follow **RFC 9457** (`application/problem+json`); validation errors are a 422 problem with
an `errors` list. A few sensitive routes are rate-limited per client IP (in-process, per replica) and
answer **429** when the bucket is empty: the Google login callback and `/auth/refresh` (30/min),
`POST /webhooks/github` (120/min) and `POST /environments/{id}/discover-inputs` (10/min). Every
response carries an `X-Request-ID` header (echoed from the request when it sends one).

## Authentication

Human endpoints take a Bearer **access token** (a 15-minute JWT). In dev, get one via dev login:

```bash
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/dev/login \
  -H 'content-type: application/json' -d '{"persona":"alice"}' | jq -r .access_token)
curl -s localhost:8000/api/v1/stacks -H "Authorization: Bearer $TOKEN"
```

In production, authentication is Google OIDC (PKCE) with a rotating refresh token; mutating calls
on `/refresh` and `/logout` use a CSRF double-submit token.

**Automation** (CI, scripts, scheduled exports) uses a **service account** and its API token
([SPECS §23](SPECS.md)): an instance admin creates the account, grants it a space membership, and
mints an expiring token, shown once — in the UI under **Settings → Service accounts** (the
membership under **Settings → Spaces**), or over the API:

```bash
SA=$(curl -s -X POST $STACKD/api/v1/service-accounts -H "$AUTH" -H 'content-type: application/json' \
  -d '{"name":"ci-github","role":"writer"}' | jq -r .id)
curl -s -X PUT $STACKD/api/v1/spaces/$SPACE_ID/members -H "$AUTH" -H 'content-type: application/json' \
  -d "{\"user_id\":\"$SA\",\"role\":\"writer\",\"allowed_tiers\":[],\"can_destroy\":false}"
TOKEN=$(curl -s -X POST $STACKD/api/v1/service-accounts/$SA/tokens -H "$AUTH" \
  -H 'content-type: application/json' -d '{"name":"github-actions","expires_in_days":90}' | jq -r .token)
curl -s -X POST $STACKD/api/v1/environments/$ENV_ID/runs -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{}'
```

A service account reads and plans — it **never confirms** an apply, a destroy, a mutating command
or a rollout: that stays a named human's decision.

The [`stackd` CLI](guide/cli.md) wraps the same API for scripts and CI (`stackd runs plan
network/prod --wait`).

## Interactive reference

The API is FastAPI, so the full, always-current schema is served by the running instance:

- **Swagger UI** — `/docs` (disabled when `STACKD_ENV=production`; ReDoc is not served)
- **OpenAPI JSON** — `/openapi.json` (served in every mode, production included)

## The main surface

A curated map, paths relative to `/api/v1` unless noted (the [Specification](SPECS.md) §12 is the
exhaustive list):

| Area | Examples |
|---|---|
| Auth | `GET /auth/google/start` + `/callback`, `POST /auth/refresh`, `POST /auth/logout`, `GET /auth/me`, `POST /auth/me/onboarded`; dev only: `GET /auth/dev/personas`, `POST /auth/dev/login` |
| Spaces | `GET/POST /spaces`, `GET/PUT /spaces/{id}/members`, `DELETE /spaces/{id}/members/{user_id}`, secret sources under `/spaces/{id}/secret-sources`, `POST /spaces/{id}/environment-clones`, configuration `GET /spaces/{id}/export` / `POST /spaces/{id}/import[?dry_run=true]` (space admin, [Backup & restore](guide/backup-restore.md)) |
| Stacks | `GET/POST /stacks`, `GET/PATCH/DELETE /stacks/{id}` (409 while a run is active), `POST /stacks/{id}/check-repo` |
| Environments | `POST /stacks/{id}/environments`, `GET /environments/{id}/resolved-variables`, `POST /environments/{id}/refresh-head`, `POST /environments/{id}/discover-inputs` |
| Variables & sets | variables CRUD (stack/env/set), variable sets + attachments (tier/stack/env of the set's space) |
| Runs | `POST /environments/{id}/runs` (optional `targets`/`replace`: a targeted / replace plan), `/commands` (incl. `force-unlock` on an unmanaged backend, admin), `/promote`; `GET /environments/{id}/runs`; `GET /runs/{id}`, `POST /runs/{id}/confirm`/`discard`/`cancel`, `GET /runs/{id}/logs` (+ `/logs/download`)/`plan`/`checks`, `POST /runs/{id}/vcs/resync` |
| Rollouts | `GET/POST /rollouts`, `GET /rollouts/{id}`, `POST /rollouts/{id}/approve`/`retry`/`cancel` |
| Comments | `GET/POST /runs/{id}/comments`, `PATCH/DELETE /runs/{id}/comments/{cid}` |
| Hooks | `GET/POST/PATCH/DELETE /stacks/{id}/hooks` and `/environments/{id}/hooks` |
| Dependencies | `GET/POST /environments/{id}/dependencies`, `DELETE /dependencies/{id}`, `POST /stacks/{id}/dependencies/link-by-name`, `GET /environments/{id}/outputs`, `GET /graph` |
| State | `GET /environments/{id}/state/versions`, `POST …/state/import-session`, `DELETE …/state/lock` (force-unlock) |
| Cloud | `GET/PUT/DELETE /environments/{id}/cloud-integration` + `POST …/cloud-integration/test` (AssumeRole test) |
| Notifications | targets: `GET/POST/PATCH/DELETE /stacks/{id}/notifications` and `/environments/{id}/notifications`, plus `…/{tid}/test`; your in-app feed: `GET /notifications`, `POST /notifications/read`, `DELETE /notifications/{nid}`, `DELETE /notifications` |
| Service accounts | `GET/POST /service-accounts`, `GET/POST /service-accounts/{id}/tokens`, `DELETE /service-accounts/{id}/tokens/{tid}` (instance admin) |
| Users & tiers | `GET /users` (admin), `PATCH /users/{id}` (admin), `GET /users/mentionable`, `GET/POST /tiers`, `PATCH/DELETE /tiers/{id}` |
| Audit | `GET /audit` (filters), `GET /audit/export` (CSV) — both admin-only |
| Webhooks | `POST /webhooks/github` |
| OIDC issuer | outside `/api/v1`: `GET /.well-known/openid-configuration`, `GET /oidc/jwks`; signing keys (admin): `GET /oidc/keys`, `POST /oidc/keys/rotate` |
| Workers | `GET/POST /worker-pools`, `DELETE /worker-pools/{id}` (admin), `GET /workers`, `GET /queue`, `GET/POST /workers/{id}/diagnostics` (admin) |
| Observability | `GET /health`, `GET /logs` (admin); outside `/api/v1`: `GET /healthz`, `GET /metrics` |
| WebSocket | `GET /api/v1/ws` — live run/environment updates and your own notification feed |

## See also

- [Runs & approvals](guide/runs-and-approvals.md) · [Workers & scaling](guide/workers-and-scaling.md)
- [SPECS](SPECS.md)
