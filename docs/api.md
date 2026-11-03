# API

Stackd exposes two REST surfaces under versioned prefixes:

- **`/api/v1`** — the human/automation API (auth, spaces, stacks, environments, variables, runs,
  hooks, dependencies, notifications, audit, state management, workers, health, logs).
- **`/worker/v1`** — the worker (agent) API: register, heartbeat, claim, events, logs, artifacts
  (including the saved plan the apply job downloads). Workers authenticate with a pool/worker
  token; humans never call this.

Outside these prefixes: the Terraform HTTP state backend (`/state/v1/{env_id}`, used by tofu with
a per-job state token), the OIDC issuer (`/.well-known/openid-configuration`, `/oidc/jwks`), and
two unauthenticated probes, `/healthz` and `/metrics` (Prometheus).

Errors follow **RFC 9457** (`application/problem+json`).

## Authentication

Human endpoints take a Bearer **access token** (a 15-minute JWT). In dev, get one via dev login:

```bash
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/dev/login \
  -H 'content-type: application/json' -d '{"persona":"alice"}' | jq -r .access_token)
curl -s localhost:8000/api/v1/stacks -H "Authorization: Bearer $TOKEN"
```

In production, authentication is Google OIDC (PKCE) with a rotating refresh token; mutating calls
on `/refresh` and `/logout` use a CSRF double-submit token.

## Interactive reference

The API is FastAPI, so the full, always-current schema is served by the running instance:

- **Swagger UI** — `/docs` (disabled when `STACKD_ENV=production`)
- **OpenAPI JSON** — `/openapi.json`

## The main surface

A curated map, paths relative to `/api/v1` unless noted (the [Specification](SPECS.md) §12 is the
exhaustive list):

| Area | Examples |
|---|---|
| Auth | `POST /auth/dev/login` (dev only), `POST /auth/refresh`, `POST /auth/logout`, `GET /auth/me` |
| Spaces | `GET/POST /spaces`, `GET/PUT /spaces/{id}/members`, `DELETE /spaces/{id}/members/{user_id}`, secret sources under `/spaces/{id}/secret-sources` |
| Stacks | `GET/POST /stacks`, `GET/PATCH/DELETE /stacks/{id}` (409 while a run is active), `POST /stacks/{id}/check-repo` |
| Environments | `POST /stacks/{id}/environments`, `GET /environments/{id}/resolved-variables`, `POST /environments/{id}/refresh-head`, `POST /environments/{id}/discover-inputs` |
| Variables & sets | variables CRUD (stack/env/set), variable sets + attachments (tier/stack/env of the set's space) |
| Runs | `POST /environments/{id}/runs`, `/commands`, `/promote`; `GET /runs/{id}`, `POST /runs/{id}/confirm`/`discard`/`cancel`, `GET /runs/{id}/logs`/`plan`/`checks`, `POST /runs/{id}/vcs/resync` |
| Comments | `GET/POST /runs/{id}/comments`, `PATCH/DELETE /runs/{id}/comments/{cid}` |
| Hooks | `GET/POST/PATCH/DELETE /stacks/{id}/hooks` and `/environments/{id}/hooks` |
| Dependencies | dependencies CRUD, `link-by-name`, `/outputs`, `/graph` |
| State | `GET .../state/versions`, `POST .../state/import-session`, `DELETE .../state/lock` (force-unlock) |
| Cloud | `cloud-integration` CRUD + AssumeRole test |
| Notifications | targets: `GET/POST/PATCH/DELETE /stacks/{id}/notifications` and `/environments/{id}/notifications`, plus `…/{tid}/test`; your in-app feed: `GET /notifications`, `POST /notifications/read` |
| Users & tiers | `GET /users` (admin), `PATCH /users/{id}` (admin), `GET /users/mentionable`, `GET/POST /tiers`, `PATCH/DELETE /tiers/{id}` |
| Audit | `GET /audit` (filters), `GET /audit/export` (CSV) — both admin-only |
| Webhooks | `POST /webhooks/github` |
| OIDC issuer | `GET /.well-known/openid-configuration`, `GET /oidc/jwks` |
| Workers | `GET/POST /worker-pools`, `DELETE /worker-pools/{id}` (admin), `GET /workers`, `GET /queue`, worker diagnostics |
| Observability | `GET /health`, `GET /logs` (admin); outside `/api/v1`: `GET /healthz`, `GET /metrics` |
| WebSocket | `GET /api/v1/ws` — live run/environment updates and your own notification feed |

## See also

- [Runs & approvals](guide/runs-and-approvals.md) · [Workers & scaling](guide/workers-and-scaling.md)
- [SPECS](SPECS.md)
