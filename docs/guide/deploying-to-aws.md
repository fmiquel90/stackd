# Deploying to AWS

`deploy/terraform/` is an OpenTofu/Terraform module that deploys Stackd to one AWS account and
region. The first apply builds the infrastructure. You then push the images, set two secrets by
hand and apply again with the image digests.

!!! warning "Not applied to a real AWS account yet"
    The module passes `tofu fmt`, `tofu validate`, and a full apply / re-plan / destroy against
    MiniStack, a local AWS emulator ([Testing without AWS](#testing-without-aws)). The images,
    the worker agent's drain and the API's TLS and proxy-header handling were tested locally. What
    only real AWS shows (IAM and KMS policy evaluation, CloudFront VPC origin routing, EC2 user
    data, RDS engine availability) has not been tried: treat a first deployment as a trial run.

## Architecture

```
browser ──HTTPS──▶ CloudFront ──┬── /* ─────────────────────────────▶ S3: SPA (Origin Access Control)
                                └── /api/* /.well-known/* /oidc/* /healthz
                                      │ HTTPS, VPC origin
worker hosts (EC2 + Docker) ──HTTPS──▶ internal ALB :443 ──HTTPS──▶ API (ECS Fargate) ──▶ RDS PostgreSQL 18
   (docker runner: one container                                          ├──▶ S3: states, saved plans (SSE-KMS)
    per command)                                                          └──▶ KMS: OIDC signing key (kms:Sign)
```

| Component | AWS service | Notes |
|---|---|---|
| API | ECS Fargate | Production image `api/Dockerfile`, which does not contain the dev login. Behind an **internal** ALB |
| Workers | EC2 Auto Scaling group | Amazon Linux 2023 + Docker. The agent runs with the **`docker` runner**, the only one a production API accepts. Fargate cannot run it: it has no Docker daemon |
| Database | RDS PostgreSQL **18** | The migrations use the native `uuidv7()`. Encrypted, TLS enforced (`rds.force_ssl`), optional Multi-AZ |
| Object storage | S3 | `states/`, `artifacts/` (saved plans), `logs/`: SSE-KMS and versioning. The SPA has a separate bucket |
| Workload OIDC | KMS | An asymmetric RSA key. The API signs workload tokens with `kms:Sign`, and the private key never leaves KMS (SPECS §3.11) |
| Secrets | Secrets Manager + KMS | None of them is in the state, the plan or a tfvars file (see [Secrets](#secrets)) |
| TLS | ACM | One certificate for CloudFront (in us-east-1) and one for the internal ALB (in your region) |
| Registry | ECR | Three repositories: `api`, `worker`, `runner`. Tags are immutable and images are deployed **by digest** |

**Every hop uses TLS.** CloudFront reaches the internal ALB through a VPC origin over HTTPS, and
checks the ALB's ACM certificate for `internal_domain_name`. Workers connect to that same name
over HTTPS. The ALB has no plain-HTTP listener. It forwards to the API tasks over HTTPS too:
the image generates a self-signed certificate at startup. The ALB does not verify target
certificates, so this last hop is encrypted but not authenticated. The security groups only let
the ALB reach the tasks.

**Public routes:** `/api/*` (REST, WebSocket, Google callback), `/.well-known/*` and `/oidc/*`
(the workload issuer's discovery document and JWKS, which AWS STS fetches), and `/healthz`.
`/worker/*` and `/state/v1/*` are **not public**. Workers run inside the VPC and use the internal
URL for both. External workers outside the VPC are not supported by this module.

**Client IPs.** The API trusts `X-Forwarded-For` only from the private subnets, where the ALB
nodes and CloudFront's VPC origin interfaces live (uvicorn `--proxy-headers`,
`FORWARDED_ALLOW_IPS`). The audit `ip` field and the rate limiter therefore see the browser's
address. Workers run in their own subnets, so a job container cannot pose as another client by
forging the header.

## Prerequisites

- **OpenTofu ≥ 1.11** (or Terraform ≥ 1.11). The module needs ephemeral resources and write-only
  arguments.
- AWS CLI v2, Docker with `buildx`, `pnpm` (through corepack), and bash.
- A domain you control, for example `stackd.acme.com`. Ideally it is in a public Route 53 zone.
  The domain is **required**: its URL is the OIDC issuer that your cloud accounts trust, so it
  must not change. A `*.cloudfront.net` name changes whenever the distribution is replaced.
- A Google OAuth client (Google Cloud Console → APIs & Services → Credentials → OAuth client ID,
  type "Web application"). Google is the only way to sign in to a production API: the dev login
  is not in the production image.

## Bootstrap

The examples are in `deploy/terraform/examples/` (`minimal`, `production`):

```bash
cd deploy/terraform/examples/minimal
cp terraform.tfvars.example terraform.tfvars   # domain_name, route53_zone_id, google_*
tofu init
```

Set `google_allowed_domains` to your Workspace domain(s). The module refuses an empty list,
because an empty allowlist would admit **any** Google account, and the first account to sign in
becomes admin.

### 1. Infrastructure

```bash
tofu apply
```

Leave `api_image` unset for this apply. It creates the network, RDS, the buckets, KMS, the
secrets, ECR, the ALB, CloudFront and the certificates. It creates no ECS service, no migration
and no worker.

!!! note "Without a Route 53 zone (two steps)"
    The certificates must be validated before the ALB and CloudFront can use them. Run this
    first:

    ```bash
    tofu apply -target=module.stackd.module.certificates
    tofu output acm_validation_records    # or: aws acm describe-certificate
    ```

    Create the CNAME records it lists with your DNS provider, then run the full `tofu apply`. It
    waits until the certificates are issued. Afterwards, create the two records listed in
    `tofu output dns_records`:

    - `domain_name` → the CloudFront domain
    - `internal_domain_name` → the internal ALB, which has private addresses

    You can also skip ACM here and pass your own validated certificates through
    `certificate_arn` (us-east-1) and `internal_certificate_arn` (your region).

### 2. Images

Build the images and push them to the ECR repositories from `tofu output ecr_repository_urls`:

```bash
REGISTRY=$(tofu output -json ecr_repository_urls | jq -r .api | cut -d/ -f1)
aws ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY"

API=$(tofu output -json ecr_repository_urls | jq -r .api)
WORKER=$(tofu output -json ecr_repository_urls | jq -r .worker)
RUNNER=$(tofu output -json ecr_repository_urls | jq -r .runner)
V=$(git rev-parse --short HEAD)

docker buildx build -f api/Dockerfile --platform linux/amd64 -t "$API:$V" --push api
docker buildx build -f worker/Dockerfile --platform linux/amd64 -t "$WORKER:$V" --push worker
docker buildx build -f deploy/images/opentofu.Dockerfile --build-arg OPENTOFU_VERSION=1.12.2 \
  --platform linux/amd64 -t "$RUNNER:opentofu-1.12.2" --push .
```

Then get the digests. The module accepts nothing else, and tags are immutable:

```bash
aws ecr describe-images --repository-name stackd-api --image-ids imageTag=$V \
  --query 'imageDetails[0].imageDigest' --output text
```

### 3. Secrets set by hand

Two values come from outside AWS. You put them into Secrets Manager yourself, so they never
appear in the state or in a tfvars file.

```bash
# Google OAuth client secret. Must be set before the API starts.
aws secretsmanager put-secret-value \
  --secret-id "$(tofu output -raw google_client_secret_arn)" --secret-string 'GOCSPX-…'
```

Register `tofu output google_oauth_redirect_uri` as an authorized redirect URI of the Google
OAuth client.

### 4. API

```hcl
# terraform.tfvars
api_image = "123456789012.dkr.ecr.eu-west-1.amazonaws.com/stackd-api@sha256:…"
```

```bash
tofu apply
```

This apply runs the **migration task** before the service starts. The task runs
`alembic upgrade head`, then `python -m app.seed --base`, which creates the `default` space and
the tier catalog. The task is started with bash and the AWS CLI from the machine that runs
`tofu`, and the apply waits for it to succeed. If you set `run_migrations = false`, run
`tofu output -raw migrate_command` yourself before the service rolls.

Publish the front:

```bash
deploy/terraform/scripts/publish-spa.sh "$(tofu output -raw spa_bucket)" \
  "$(tofu output -raw cloudfront_distribution_id)"
```

Open `tofu output public_url` and sign in with Google. **The first account to sign in becomes
admin**, so sign in yourself before anyone else can.

### 5. Workers

1. In Stackd, open **Workers** and create a pool. Copy the token: it is shown once and is
   43 URL-safe characters. Put it into Secrets Manager:

   ```bash
   aws secretsmanager put-secret-value \
     --secret-id "$(tofu output -raw worker_pool_token_secret_arn)" --secret-string '<token>'
   ```

2. Add the worker images and apply:

   ```hcl
   worker_image  = "…/stackd-worker@sha256:…"
   runner_images = { "opentofu:1.12.2" = "…/stackd-runner@sha256:…" }
   ```

The hosts start, wait for the pool token, pull the images by digest and register. A worker only
takes runs whose stack's `tool_version` has an entry in `runner_images`.

### 6. Check

- `https://<domain>/healthz` returns `{"status":"ok"}`.
- `https://<domain>/.well-known/openid-configuration` lists `jwks_uri = https://<domain>/oidc/jwks`,
  and that URL serves the JWKS (JSON, not the SPA).
- **Workers** shows the hosts online, with the `docker` runner.
- A demo plan → confirm → apply goes through.

## Secrets

| Secret | Created by | Rotation |
|---|---|---|
| `STACKD_ENCRYPTION_KEY` | 32 random bytes, generated as an ephemeral value and written **write-only** | **Never.** Everything encrypted in the database depends on it, and there is no re-encryption tool. The module protects it with `prevent_destroy` |
| `STACKD_JWT_SECRET` | ephemeral random value, write-only | Bump `jwt_secret_version` and apply, then redeploy the API (`aws ecs update-service --force-new-deployment`). Every user is signed out |
| Database password, `DATABASE_URL` | ephemeral random value, written write-only to Secrets Manager. RDS and `DATABASE_URL` (`?ssl=require`) both read it back from there | Bump `db_password_version`, apply, redeploy the API |
| `GOOGLE_CLIENT_SECRET` | you (`put-secret-value`) | Put a new value, redeploy the API |
| Worker pool token | you (`put-secret-value`) | Delete the pool and create a new one in Stackd. Put the new token, then start an instance refresh on the worker group |

The state holds no secret value, but it still maps the whole deployment. Keep it in an
encrypted, versioned bucket with locking, as the production example's backend does
(`encrypt = true`, `use_lockfile = true`).

The ECS execution role reads only the API's four secrets. The worker instance role reads only
the pool token. The API task role can reach the artifacts bucket and the KMS keys (data key
through S3; `kms:Sign` and `kms:GetPublicKey` on the OIDC key), nothing else. RDS-managed master
passwords (`manage_master_user_password`) are not used: they rotate every 7 days by default, and
the API reads `DATABASE_URL` once, at startup.

## Workers on EC2

Each host is an Amazon Linux 2023 instance in the worker subnets. It runs two units:

- `stackd-worker`: the agent container (`worker/Dockerfile`, `STACKD_RUNNER=docker`). It mounts
  the Docker socket and `/var/lib/stackd/ws` at the same path on the host and in the container.
  Each tofu command and each hook runs in its own hardened container (SPECS §7.4).
- `stackd-lifecycle`: on scale-in or replacement, it stops the agent, which drains, then
  completes the Auto Scaling termination hook.

**Containment.** The host keeps what job containers (repo code) can reach to a minimum:

- IMDSv2 only, hop limit 1, and the metadata address is also dropped at the Docker bridge. Job
  containers cannot get the instance role.
- The instance role only pulls the Stackd images, reads the pool token, writes the agent's logs
  and completes its own termination hook. It has no database, S3 or signing access: workers
  reach states and saved plans through the API.
- There is no inbound rule. Shell access through Session Manager is off by default
  (`worker_enable_ssm`).

The Docker socket is root on the host. Treat the worker hosts as dedicated, and use runner images
you trust.

**Scaling and rollouts.** The group has a fixed size, `worker_count`. Change the count and apply
to scale. There is no CPU-based autoscaling, because workers waiting on the claim long-poll use
almost no CPU even when the queue is full (see
[Workers & scaling](workers-and-scaling.md#autoscaling-on-aws)). A new launch template triggers
an instance refresh, new hosts first. That happens on a new worker or runner image digest, a new
AMI or new settings. A host that leaves the group receives SIGTERM. The agent stops claiming and
lets its running jobs finish, for up to `worker_shutdown_timeout_seconds` (default 1800 s,
above the 900 s apply budget). Only then does the instance terminate. Past that budget, the
agent reports the jobs still running as failed and kills them.

## Upgrading

1. Build and push the new images, then take their digests.
2. Set `api_image` (and `worker_image` if it changed) and apply. Migrations run first, then the
   ECS service rolls (with circuit breaker and rollback). The worker group then refreshes, and
   draining hosts finish their jobs.
3. Run `scripts/publish-spa.sh` if the front changed.

Migrations must stay compatible with the API version that is still running during the roll.

## Tearing down

`db_deletion_protection` and `alb_deletion_protection` default to `true`, and the master key is
`prevent_destroy`. To destroy a deployment on purpose:

1. Set both deletion protections to `false` and apply.
2. Remove the master key from the state:

   ```bash
   tofu state rm 'module.stackd.module.secrets.aws_secretsmanager_secret_version.encryption_key' \
     'module.stackd.module.secrets.aws_secretsmanager_secret.encryption_key'
   ```

3. Run `tofu destroy`.
4. Delete the encryption key secret yourself (`aws secretsmanager delete-secret`) once you no
   longer need the database snapshot.

## Testing without AWS

`deploy/terraform/examples/ministack` applies the whole module to
[MiniStack](https://ministack.org), an MIT-licensed local AWS emulator, with no account and no
cost:

```bash
task tf:check      # tofu fmt + validate: module and every example
task tf:sim        # start MiniStack, apply, check the state, re-plan, destroy
task tf:sim:down
```

`task tf:sim` fails on a refused resource or argument, a generated secret in the state, a
non-empty second plan (bar a short list of attributes MiniStack does not echo back), or anything
left after the destroy, which follows the [teardown](#tearing-down) steps above. CI runs the
same checks. The example's README lists what this proves and what it cannot.

If you create the Route 53 zone in the same configuration as the module, its id is unknown at
plan time: set `manage_dns_records = true` alongside `route53_zone_id` (the MiniStack example
does).

## Known limitations

- **Not applied to a real AWS account yet**: validated statically and against MiniStack only
  (see the note at the top).
- **One region, one account.** Cross-region DR is not covered (RDS snapshots and S3 replication
  are up to you).
- **No WAF** in front of CloudFront. Attach one to the distribution if you need it.
- **The last hop, ALB → API task, is encrypted but not authenticated** (self-signed certificate,
  which the ALB does not verify).
- **External workers** outside the VPC are not supported: the worker protocol and the state
  backend are not routed through CloudFront.
- **The migration runs from the operator's machine** (bash + AWS CLI). Set
  `run_migrations = false` to run `migrate_command` from CI instead.
- The worker AMI follows the latest Amazon Linux 2023 release unless you set `worker_ami_id`. A
  new AMI rolls the hosts on the next apply.

## See also

- [Workers & scaling](workers-and-scaling.md)
- [Cloud credentials (OIDC)](cloud-credentials.md)
- [`deploy/images/`](https://github.com/fmiquel90/stackd/tree/main/deploy/images): the runner images
- [DEV §3](../DEV.md): why the dev login is not in the production image
