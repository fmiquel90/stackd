# API

Stackd exposes two REST surfaces under versioned prefixes:

- **`/api/v1`** — the human/automation API (auth, stacks, environments, variables, runs, hooks,
  dependencies, notifications, audit, state management, workers, health, logs).
- **`/worker/v1`** — the worker (agent) API: register, heartbeat, claim, events, logs, artifacts.
  Workers authenticate with a pool/worker token; humans never call this.

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

- **Swagger UI** — `/docs`
- **ReDoc** — `/redoc`
- **OpenAPI JSON** — `/openapi.json`

## The main surface

A curated map (the [Specification](SPECS.md) §12 is the exhaustive list):

| Area | Examples |
|---|---|
| Auth | `POST /auth/dev/login`, `POST /auth/refresh`, `POST /auth/logout`, `GET /auth/me` |
| Stacks | `GET/POST /stacks`, `POST /stacks/{id}/check-repo` |
| Environments | `POST /stacks/{id}/environments`, `GET /environments/{id}/resolved-variables`, `POST /environments/{id}/refresh-head` |
| Variables & sets | variables CRUD (stack/env/set), variable sets + attachments |
| Runs | `POST /environments/{id}/runs`, `GET /runs/{id}`, `POST /runs/{id}/confirm`/`discard`/`cancel`, `GET /runs/{id}/logs`/`plan`/`checks` |
| Hooks | `GET/POST/PATCH/DELETE /stacks/{id}/hooks` and `/environments/{id}/hooks` |
| Dependencies | dependencies CRUD, `link-by-name`, `/outputs`, `/graph` |
| State | `GET .../state/versions`, `POST .../state/import-session`, `DELETE .../state/lock` (force-unlock) |
| Cloud | `cloud-integration` CRUD + AssumeRole test |
| Notifications | `GET/POST/PATCH/DELETE /stacks/{id}/notifications` and `/environments/{id}/notifications` |
| Audit | `GET /audit` (filters), `GET /audit/export` (CSV, admin) |
| Webhooks | `POST /webhooks/github` |
| OIDC issuer | `GET /.well-known/openid-configuration`, `GET /oidc/jwks` |
| Observability | `GET /health`, `GET /logs` (admin), `GET /queue`, worker diagnostics |
| WebSocket | `GET /api/v1/ws` — live run/environment updates |

## See also

- [Runs & approvals](guide/runs-and-approvals.md) · [Workers & scaling](guide/workers-and-scaling.md)
- [SPECS](SPECS.md)
