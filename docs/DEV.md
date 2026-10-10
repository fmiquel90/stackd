# DEV.md — Local development mode

> Goal: `git clone` → `task dev` → test a complete **plan → confirm → apply → cascade** cycle in under 5 minutes, without a Google account, without AWS, without a GitHub repo.

---

## 1. Principle: everything is real, except the external boundaries

Dev mode runs the **real components** (API, scheduler, state machine, worker, state backend, audit) and replaces only the three external dependencies:

| Dependency | In prod | In local dev |
|---|---|---|
| Google auth | OIDC accounts.google.com | **Dev login** flag-gated (§3) |
| Git repos | GitHub/GitLab | **Local fixture repos** `file://` + optional Gitea (§4) |
| AWS cloud | Real STS/providers | **Cloud-less fixtures** (built-in `terraform_data`, no provider) + optional LocalStack (§6) |

Everything else (Postgres, Garage, the worker protocol, hooks, mocks, staleness) works identically to prod. This is intentional: dev mode tests the platform, not a simulation of the platform.

---

## 2. Docker compose stack

```yaml
# deploy/docker-compose.dev.yml
services:
  postgres:        # 18, port 5432, named volume
  garage:          # local S3 (Garage), S3 API on :3900, admin CLI `garage` ; layout, key and stackd bucket provisioned by `task dev`
  api:             # uv run uvicorn --reload, mounts ./api, port 8000
  front:           # vite dev server, mounts ./front, port 5173, proxy /api → api
  worker:          # agent in --reload mode (watchfiles), runner=local (§5)
  # optional profiles:
  gitea:           # profile "git"  : real webhooks locally (§4)
  localstack:      # profile "aws"  : simulated S3/RDS/IAM (§6)
```

```bash
task dev            # S3 bootstrap + compose up (base services) + migrations + seed
task dev-git        # + Gitea (webhook testing)
task dev-aws        # + LocalStack
task seed           # (re)creates the demo data, idempotent
task s3             # provision the dev S3 (Garage layout, key into .env, bucket), idempotent; part of dev
task s3-ls          # list the dev bucket (managed states, saved plans)
task reset          # down -v, drop .dev/pool.token, then dev : start from scratch
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
    desc: Complete local stack (compose + S3 bootstrap + migrations + seed)
    deps: [env]
    cmds:
      - task: s3                    # before `up`: compose reads the S3 key from .env at creation
      - "{{.COMPOSE}} up -d --wait"
      - task: migrate
      - task: seed-fixtures
      - task: seed

  s3:
    desc: Provision the dev S3 (Garage layout, access key into .env, bucket; idempotent)
    cmds: [bash scripts/bootstrap-s3.sh]

  env:
    desc: Generates .env and dev keys on first launch
    cmds: [bash scripts/bootstrap-env.sh]
    status: [test -f .env]          # idempotent: does nothing if .env exists

  seed:
    desc: Demo data (idempotent)
    cmds: ["{{.COMPOSE}} exec -T api uv run python -m app.seed"]

  reset:
    desc: Tear everything down and start fresh
    cmds:
      - "{{.COMPOSE}} down -v"
      - rm -f .dev/pool.token       # belongs to the wiped DB: the worker waits for the new seed's
      - task: dev

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
    cmds: ["{{.COMPOSE}} exec -T api uv run pytest e2e -v"]
```

`.env.example` → `.env` copied automatically by `task dev` (`task env`, `scripts/bootstrap-env.sh`, only when `.env` is missing): dev keys (encryption key, JWT secret, OIDC keypair) **generated on first launch** and stored in `.dev/` (gitignored), the first two injected into `.env`. No secret value committed, no manual entry.

**Dev S3 (Garage)**: the API keeps managed states and every tracked/destroy run's saved plan (encrypted; deleted when the run ends) in S3, so a plan with changes needs a provisioned Garage. `task s3` (`scripts/bootstrap-s3.sh`, run by every `dev*` task) brings `garage` up and, checking before each step:

1. assigns the single-node layout (zone `dc1`) and applies it;
2. writes a generated access key into `.env` (`S3_ACCESS_KEY` / `S3_SECRET_KEY`) **only when both are empty**, then imports it into Garage if Garage lacks it;
3. creates the `S3_BUCKET` bucket (`stackd`) and grants the key read/write/owner on it.

A re-run changes nothing. Compose reads `.env` when it creates a container, which is why `task dev` runs the bootstrap before `up`; a stack started before the key existed is recreated by that `up`, since its `.env` values changed. Garage binds IPv4 (`0.0.0.0`, `deploy/garage.toml`), so it also starts on hosts whose kernel has IPv6 disabled. The CI e2e workflow runs the same script.

**Only the `api` holds platform secrets.** The S3 key, `DATABASE_URL`, `STACKD_ENCRYPTION_KEY`, `STACKD_JWT_SECRET` and `GOOGLE_CLIENT_SECRET` come from the shared `.env`, and compose blanks all of them in the `worker`'s environment: the worker reaches plans and states through the API (SPECS §11.1) and needs none of them, and tofu and platform hooks inherit the worker's environment (minus its own `STACKD_*` settings) — repo code runs inside tofu. Repo hooks only get an allowlist of it (SPECS §8.3).

**`task reset`** runs `down -v` (Postgres **and** Garage volumes), deletes `.dev/pool.token`, then `task dev`. `.env` and `.dev/` keys are kept, so the S3 bootstrap imports the same key into the fresh Garage. The pool token belongs to the wiped database: without it the worker waits (re-reading the file) for the new seed's token instead of registering with a stale one before the migrations have run.

---

## 3. Auth in dev: the dev login

Running the real Google flow locally is possible (OAuth client with redirect `http://localhost:8000`) but cumbersome for quick testing. Dev mode therefore adds an **explicitly gated** bypass:

```
STACKD_DEV_AUTH=true        # rejected if STACKD_ENV=production (assert at startup)
```

- The login page displays, below the Google button, a "Dev login" panel with **three personas** covering the permissions in SPECS §2.4: `admin@dev.local` (admin, tiers dev/staging/prod, destroy), `alice@dev.local` (approver, tiers dev/staging/prod), `bob@dev.local` (writer, tiers dev/staging — a writer **cannot confirm any apply**). One click = session.
- Three personas: essential for testing the role gate (bob triggers, but is refused at confirm everywhere), prod 4-eyes (bob triggers, alice confirms) and audit ("who applied what" only makes sense with several people). No persona is an approver *without* prod, so the "apply everywhere except prod" tier gate needs a space membership (`PUT /api/v1/spaces/{id}/members`) giving an approver `{dev, staging}`.
- Sessions, roles and audit events are strictly the same as with Google — only the `google_sub` is synthetic (`dev:admin`).
- The real Google flow remains testable in dev by setting `GOOGLE_CLIENT_ID/SECRET` (the two coexist).

Safeguard: the prod build of the API image **removes the dev_auth module** (not just the flag) — a configuration oversight cannot expose the bypass.

---

## 4. Git fixture repos

`task seed-fixtures` (`scripts/seed-fixtures.sh`, run on the host by every `dev*` task, idempotent) creates in `.dev/repos/` two **local and committable** Git repositories (branch `main`). They use only the built-in `terraform_data` resource: no provider to download, `tofu plan/apply` runs fully offline.

```
.dev/repos/demo-network/      # upstream stack
  main.tf                     #   variable org, terraform_data "network", output network_name
.dev/repos/demo-app/          # downstream stack
  main.tf                     #   variables org + network_name, terraform_data "app", output app_target
```

- The seed stacks point to them with `repo_url: file:///stackd-dev/repos/demo-network` (`.dev/` is mounted at `/stackd-dev` in the API and the worker; `STACKD_SEED_REPO_BASE` overrides the base). `repo_auth_kind: none`. The fixtures carry no `.stackd.yml`.
- **Simulate "a PR is merged"** without GitHub: `task push-change` commits a change in demo-network → staleness polling (reduced to **15 s** in dev via `STACKD_HEAD_POLL_INTERVAL`) turns `stale` to `true` (`GET /api/v1/environments/{id}`) on an env that already has a finished tracked run. The `↑1` chip of DESIGN §5.1 is not in the UI yet. Perfect for testing the "apply at 9am, merge at 9:15am" scenario.
- **Real webhooks**: `task dev-git` launches Gitea (profile `git`, port 3000). Pushing the fixtures to it and configuring its webhook to the API are manual for now (no `seed-gitea` or `webhook` task yet).

---

## 5. Worker in dev

- Launched by compose with `STACKD_RUNNER=local`: terraform commands run directly inside the worker container (image including OpenTofu + jq + git). **No Docker-in-Docker** — the `docker` runner is not implemented in the agent yet (SPECS §7.4), so there is nothing to exercise in dev.
- Tool binaries: **OpenTofu and Terraform** pre-installed in the dev image (no download on first run). A stack's `tool` picks which one runs — no separate "terraform worker", any dev worker handles both.
- Agent hot reload (watchfiles): modifying `worker/` cleanly restarts the poll loop (the current job finishes).
- Multi-workers to test concurrency and affinity: `docker compose -f deploy/docker-compose.dev.yml up -d --scale worker=3`, or `STACKD_MAX_CONCURRENT_JOBS` for several jobs per worker.
- The worker reads its pool token from `.dev/pool.token` (`STACKD_POOL_TOKEN_FILE`), written by the seed: it waits for the file and re-reads it on a failed registration.

---

## 6. Terraform without AWS (and with, if needed)

**By default**: the fixtures use only the built-in `terraform_data` resource — zero providers, zero credentials, zero cost, execution in seconds. Enough to test **the entire platform** (the platform orchestrates terraform, it does not depend on what terraform creates).

**`aws` profile (LocalStack)**: to test AWS-realistic stacks (S3, SQS, IAM...), `task dev-aws` brings up LocalStack on `:4566`; the seed creates no LocalStack variable set, so add the provider endpoint overrides yourself (e.g. in a variable set). Known limitations: LocalStack does not cover everything and **does not validate workload OIDC** (STS AssumeRoleWithWebIdentity is superficial there).

**Your own AWS profiles (real `plan` against real AWS)**: to run a real `terraform plan` against your AWS account using your **named profiles** — without setting up the OIDC/STS integration — bring the stack up with:

```
task dev-aws-creds      # = docker compose -f docker-compose.dev.yml -f docker-compose.dev.aws.yml up
```

Use it **instead of `task dev`** whenever you want AWS creds in the worker — a plain `task dev` (or any `docker compose up` without the overlay) recreates the worker **without** the mount. It mounts `~/.aws` **read-only** into the worker. The provider's `profile = "..."` then resolves from your `~/.aws/{config,credentials}` (the worker runs terraform with the container env, minus its own `STACKD_*` settings; `_cloud_env` only injects OIDC vars for environments that have a cloud-integration, so a plain dev env falls back to these creds). For SSO profiles, run `aws sso login` first (the cached token under `~/.aws` is mounted too); for a default profile, add `AWS_PROFILE=<name>` to `.env`. **Dev-only, never in prod or CI**; this tests your Terraform, not the platform's OIDC exchange.

**Workload OIDC in dev**: the issuer runs (JWKS on `localhost:8000/oidc/jwks`, tokens signed per claim) — we test the **generation and claims** of the tokens (unit tests, `api/tests/test_oidc.py`). The real STS exchange is tested against a real AWS sandbox account, not locally: documented as out of scope for dev mode.

---

## 7. Seed and demo scenario

`task seed` creates (idempotent):

```
tiers: dev, staging, prod (prod requires four-eyes)      ← catalog, global
space "default"                                          ← bootstrap space, empty
space "demo"
├── variable set common        (TF_VAR_org=demo, auto_attach)
├── variable set region-local  (TF_VAR_region=local-1, attached to both stacks)
├── stack demo-network  → envs: dev (tier dev), prod (tier prod, protected, 4-eyes)
├── stack demo-app      → envs: dev (tier dev), prod (tier prod, protected, 4-eyes)
│     every env: branch main, managed_state off (local state, not kept between runs)
├── dependencies : network/dev → app/dev, network/prod → app/prod
│     output_references: network_name → TF_VAR_network_name
│                        (mock: "mock-network")
└── worker pool "local" (token written to .dev/pool.token, consumed by the compose worker)
```

`task e2e` replays the complete journey via the API (`api/e2e/test_scenario.py`, the non-regression test):

```
 1. bob (writer) triggers network/dev                  → plan → unconfirmed
 2. bob's confirm is refused (403: writer, not approver); alice confirms → apply → finished
 3. automatic cascade                                  → app/dev plans with the REAL input (no mock)
 4. bob triggers app/prod BEFORE network/prod          → plan with the MOCK (used_mocks=true)
 5. alice's confirm of the mocked run                  → 409 (allow_mock_apply=false); alice discards it
 6. bob triggers network/prod, then confirms           → 403 (a role refusal: bob is a writer)
 7. alice confirms network/prod (tier prod in her set, 4-eyes ok: alice ≠ bob) → apply → finished
 8. automatic cascade                                  → app/prod re-plans with the REAL input
 9. audit                                              → run.confirmed lists alice@dev.local
```

If the 9 steps pass, the core of the product works. It is also the demo script to run through in front of someone. Two checks stay **manual** (not in the automated scenario): staleness (`task push-change`, then `stale: true` within 15 s on an env that has a finished run) and the tier gate (an approver without `prod` in their set, see §3).

Every tracked plan goes through S3 (saved encrypted after the plan, downloaded by the apply, deleted when the run ends), so the scenario needs the provisioned Garage (§2). `task e2e` depends on `task dev`, which provisions it. CI (`.github/workflows/e2e.yml`) runs the same `scripts/bootstrap-s3.sh` before it brings up `postgres garage api worker`.

`task e2e` never runs on the seeded `demo-*` stacks themselves. It first builds a **throwaway copy** of the demo graph through the public API, as admin, in the `demo` space: `e2e-<MMDDhhmmss>-<tag>-network` and `e2e-<MMDDhhmmss>-<tag>-app`, with the same repos, branches, project roots, tiers, settings, variable-set attachments and dependency + mock. Permissions are per space, so bob and alice keep exactly the rights the steps rely on. Every run therefore starts from never-applied environments (step 4 always sees the mock): the scenario is **re-runnable on a used database without `task reset`**, and the seeded graph stays as a demo left it. At the end it ends what is still active through the API (cancels queued runs, discards unconfirmed plans) and deletes the copy. If the scenario failed or a run is still in flight, it keeps the copy for inspection, named in a warning; its unique name never blocks the next run.

---

## 8. Dev comfort

- **API**: OpenAPI/Swagger on `/docs` (disabled in prod), **structured JSON logs by default** (`STACKD_LOG_FORMAT=pretty` for local reading; `STACKD_LOG_LEVEL=DEBUG` to also capture reads/polls/heartbeats), errors with traceback. Buffer browsable via `/api/v1/logs` + **Workers & health** page.
- **Shortened timings** in dev: heartbeat 5 s, offline 15 s, staleness poll 15 s, apply affinity 10 s — asynchronous behaviors are tested without waiting.
- **Front**: the UI runs against the real dev API (Vite proxies `/api` and the WebSocket to `:8000`); there is no mock-backend mode (no MSW `dev:mock` script yet).
- **Ladle**: `cd front && pnpm ladle` — the identity components with stories (`PhaseRail`, `StateBadge`, `ProvenanceBadge`) are developed in isolation.
- **DB**: `task psql` (shell); `task reset` to start from an empty database (there is no separate `db-reset` task). The local S3 storage (Garage) is inspected via `task s3-ls` (lists the bucket with the API's own settings), or the `aws s3 --endpoint-url http://localhost:3900 --region garage` CLI with the key from `.env`; no web console (Garage is administered via its CLI: `docker compose -f deploy/docker-compose.dev.yml exec garage /garage status`).
- **Log data**: the fixtures plan and apply in seconds; to watch streaming and follow-tail on a longer run, add a slow resource (e.g. a `time_sleep`) to a fixture repo and commit it.

---

## 9. What dev mode does not cover (assumed)

| Out of scope for local dev | Where it is tested |
|---|---|
| Real STS exchange (workload OIDC) | AWS sandbox account + provided Terraform module |
| Docker runner (per-container isolation) | not implemented yet (SPECS §7.4) |
| Real GitHub/GitLab webhooks | Gitea (git profile) covers 95%; the rest in staging |
| Load (N workers × M runs) | not covered yet (no load scenario) |
| Light theme / mobile responsive | Ladle stories + manual review |
