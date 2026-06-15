# DEV.md — Local development mode

> Goal: `git clone` → `task dev` → test a complete **plan → confirm → apply → cascade** cycle in under 5 minutes, without a Google account, without AWS, without a GitHub repo.

---

## 1. Principle: everything is real, except the external boundaries

Dev mode runs the **real components** (API, scheduler, state machine, worker, state backend, audit) and replaces only the three external dependencies:

| Dependency | In prod | In local dev |
|---|---|---|
| Google auth | OIDC accounts.google.com | **Dev login** flag-gated (§3) |
| Git repos | GitHub/GitLab | **Local fixture repos** `file://` + optional Gitea (§4) |
| AWS cloud | Real STS/providers | **Cloud-less providers** (`local_file`, `random`, `null`) + optional LocalStack (§6) |

Everything else (Postgres, Garage, the worker protocol, hooks, mocks, staleness) works identically to prod. This is intentional: dev mode tests the platform, not a simulation of the platform.

---

## 2. Docker compose stack

```yaml
# deploy/docker-compose.dev.yml
services:
  postgres:        # 18, port 5432, named volume
  garage:          # local S3 (Garage), S3 API on :3900, admin CLI `garage` ; stackd bucket auto-created at seed
  api:             # uv run uvicorn --reload, mounts ./api, port 8000
  front:           # vite dev server, mounts ./front, port 5173, proxy /api → api
  worker:          # agent in --reload mode (watchfiles), runner=local (§5)
  # optional profiles:
  gitea:           # profile "git"  : real webhooks locally (§4)
  localstack:      # profile "aws"  : simulated S3/RDS/IAM (§6)
```

```bash
task dev            # compose up (base services) + migrations + seed
task dev-git        # + Gitea (webhook testing)
task dev-aws        # + LocalStack
task seed           # (re)creates the demo data, idempotent
task reset          # down -v + dev : start from scratch
task logs           # aggregated colored logs (api, worker)
task test           # pytest + vitest
task e2e            # complete automated scenario (§7)
```

Orchestration via **[Task](https://taskfile.dev)** (`Taskfile.yml` at the root) rather than Make: readable YAML syntax, cross-platform (single Go binary, no dependency on GNU Make), native `deps`/`status` for idempotence, namespaces per module, and self-documented `task --list`. Excerpt:

```yaml
# Taskfile.yml
version: '3'
dotenv: ['.env']
vars:
  COMPOSE: docker compose -f deploy/docker-compose.dev.yml

tasks:
  dev:
    desc: Complete local stack (compose + migrations + seed)
    deps: [env]
    cmds:
      - "{{.COMPOSE}} up -d --wait"
      - task: migrate
      - task: seed

  env:
    desc: Generates .env and dev keys on first launch
    cmds: [./scripts/bootstrap-env.sh]
    status: [test -f .env]          # idempotent: does nothing if .env exists

  seed:
    desc: Demo data (idempotent)
    cmds: ["{{.COMPOSE}} exec api uv run python -m app.seed"]

  push-change:
    desc: Simulates a merge into demo-network (staleness test)
    dir: .dev/repos/demo-network
    cmds:
      - date >> CHANGELOG.txt
      # Quote the whole command — the ": " in the message is a YAML mapping token otherwise.
      - 'git add -A && git commit -m "feat: simulated merge"'

  e2e:
    desc: Complete §7 scenario (non-regression)
    deps: [dev]
    cmds: [pytest tests/e2e -v]
```

`.env.example` → `.env` copied automatically by `task dev`: dev keys (encryption key, JWT secret, OIDC keypair) **generated on first launch** and stored in `.dev/` (gitignored). No secret value committed, no manual entry.

---

## 3. Auth in dev: the dev login

Running the real Google flow locally is possible (OAuth client with redirect `http://localhost:8000`) but cumbersome for quick testing. Dev mode therefore adds an **explicitly gated** bypass:

```
STACKD_DEV_AUTH=true        # rejected if STACKD_ENV=production (assert at startup)
```

- The login page displays, below the Google button, a "Dev login" panel with **three personas** covering the permissions in §2.4: `admin@dev.local` (admin, prod tier, destroy), `alice@dev.local` (approver, prod tier), `bob@dev.local` (writer, staging tier — **cannot confirm prod**). One click = session.
- Three personas with distinct tiers: essential for testing the "apply everywhere except prod" case (bob confirms dev/staging, rejected on prod), prod 4-eyes (bob triggers, alice confirms) and audit ("who applied what" only makes sense with several people).
- Sessions, roles and audit events are strictly the same as with Google — only the `google_sub` is synthetic (`dev:admin`).
- The real Google flow remains testable in dev by setting `GOOGLE_CLIENT_ID/SECRET` (the two coexist).

Safeguard: the prod build of the API image **removes the dev_auth module** (not just the flag) — a configuration oversight cannot expose the bypass.

---

## 4. Git fixture repos

`task seed` creates in `.dev/repos/` two **local and committable** Git repositories:

```
.dev/repos/demo-network/      # upstream stack
  main.tf                     #   random_pet + local_file, outputs: network_name, cidr
  outputs.tf
  .stackd.yml                 #   example after_plan hook (jq on plan.json, warn mode)
.dev/repos/demo-app/          # downstream stack
  main.tf                     #   local_file that consumes TF_VAR_network_name
  variables.tf
```

- The seed stacks point to them with `repo_url: file:///repos/demo-network` (the volume is mounted in the API and the worker). `repo_auth_kind: none`.
- **Simulate "a PR is merged"** without GitHub: `task push-change` commits a change in demo-network → staleness polling (reduced to **15 s** in dev via `STACKD_HEAD_POLL_INTERVAL`) makes the `↑1` chip appear. Perfect for testing the "apply at 9am, merge at 9:15am" scenario.
- **Real webhooks**: `task dev-git` launches Gitea, `task seed-gitea` pushes the fixtures to it and configures the webhook to the API. Otherwise, `task webhook` sends an HMAC-signed push payload via curl — enough to develop the handler.

---

## 5. Worker in dev

- Launched by compose with `STACKD_RUNNER=local`: terraform commands run directly inside the worker container (image including OpenTofu + jq + git). **No Docker-in-Docker** — the complexity of the `docker` runner is tested separately (`task test-runner-docker`, requires the socket).
- Tool binary: OpenTofu pre-installed in the dev image (no download on first run).
- Agent hot reload (watchfiles): modifying `worker/` cleanly restarts the poll loop (the current job finishes).
- Multi-workers to test concurrency and affinity: `docker compose up --scale worker=3`.

---

## 6. Terraform without AWS (and with, if needed)

**By default**: the fixtures use only `random`, `null`, `local_file` and `time` — an apply creates files in the workspace, zero credentials, zero cost, execution in seconds. Enough to test **the entire platform** (the platform orchestrates terraform, it does not depend on what terraform creates).

**`aws` profile (LocalStack)**: to test AWS-realistic stacks (S3, SQS, IAM...), `task dev-aws` + the seeded `localstack` variable set (endpoint overrides). Known limitations: LocalStack does not cover everything and **does not validate workload OIDC** (STS AssumeRoleWithWebIdentity is superficial there).

**Workload OIDC in dev**: the issuer runs (JWKS on `localhost:8000/oidc/jwks`, tokens signed per claim) — we test the **generation and claims** of the tokens (unit tests + `task show-token` which decodes the JWT of a run). The real STS exchange is tested against a real AWS sandbox account, not locally: documented as out of scope for dev mode.

---

## 7. Seed and demo scenario

`task seed` creates (idempotent):

```
space "demo"
├── variable set common        (TF_VAR_org=demo, auto_attach)
├── variable set region-local  (TF_VAR_region=local-1)
├── stack demo-network  → envs: dev (tier dev), prod (tier prod, protected, 4-eyes)
├── stack demo-app      → envs: dev (tier dev), prod (tier prod, protected)
├── dependencies : network/dev → app/dev, network/prod → app/prod
│     output_references: network_name → TF_VAR_network_name
│                        (mock: "mock-network")
└── worker pool "local" (token written to .dev/, consumed by the compose worker)
```

`task e2e` replays the complete journey via API (and serves as a non-regression test):

```
 1. login bob → trigger network/dev            → plan → unconfirmed
 2. bob confirms (tier dev, ok)                 → apply → finished
 3. automatic cascade                           → app/dev plan (real input injected)
 4. trigger app/prod BEFORE network/prod        → plan with MOCK (purple badge)
 5. attempt to confirm the mocked run           → expected rejection (allow_mock_apply=false)
 6. trigger network/prod (bob) → bob attempts to confirm → rejection (tier staging < prod)
 7. alice confirms network/prod (tier prod, 4-eyes ok) → cascade app/prod
 8. task push-change                            → chip ↑1 within 15 s
 9. verification of audit events                → triggered/confirmed/applied
                                                   with the right actors and tiers
```

If the 9 steps pass, the core of the product works. It is also the demo script to run through in front of someone.

---

## 8. Dev comfort

- **API**: OpenAPI/Swagger on `/docs` (disabled in prod), **structured JSON logs by default** (`STACKD_LOG_FORMAT=pretty` for local reading; `STACKD_LOG_LEVEL=DEBUG` to also capture reads/polls/heartbeats), errors with traceback. Buffer browsable via `/api/v1/logs` + **Workers & health** page.
- **Shortened timings** in dev: heartbeat 5 s, offline 15 s, staleness poll 15 s, apply affinity 10 s — asynchronous behaviors are tested without waiting.
- **Front**: MSW (Mock Service Worker) optional to develop the UI without a backend (`pnpm dev:mock`) — the handlers replay runs/logs fixtures, including a "running" run that streams. Useful for iterating on the viewer and the rail without triggering real runs.
- **Storybook/Ladle**: `pnpm storybook` — the identity components (PhaseRail, StateBadge, LogViewer with ANSI fixture) are developed in isolation.
- **DB**: `task psql` (shell), `task db-reset` (drop + migrations + seed). The local S3 storage (Garage) is inspected via the `aws s3 --endpoint-url http://localhost:3900` CLI or `task s3-ls`; no web console (Garage is administered via CLI).
- **Realistic log data**: the demo-network fixture includes a 10 s `time_sleep` resource — runs last long enough to see streaming, follow-tail and cancellation.

---

## 9. What dev mode does not cover (assumed)

| Out of scope for local dev | Where it is tested |
|---|---|
| Real STS exchange (workload OIDC) | AWS sandbox account + provided Terraform module |
| Docker runner (per-container isolation) | `task test-runner-docker` (socket required) or CI |
| Real GitHub/GitLab webhooks | Gitea (git profile) covers 95%; the rest in staging |
| Load (N workers × M runs) | k6 scenario in CI, not locally |
| Light theme / mobile responsive | Storybook + manual review |
