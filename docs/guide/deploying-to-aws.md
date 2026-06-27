# Deploying to AWS

Stackd ships production-ready Terraform/OpenTofu in `deploy/terraform/`. One `tofu apply` creates
the full AWS stack; a six-step bootstrap sequence gets you to a running instance.

## Architecture

```
Browser ──HTTPS──▶ CloudFront ──/api/* /worker/* /.well-known/*──▶ VPC Origin ──▶ internal ALB ──▶ ECS API
                               ──/* (SPA)                         ──▶ S3 (OAC)
```

| Component | AWS service | Notes |
|---|---|---|
| API | ECS Fargate | FastAPI, stateless, scales horizontally |
| Workers | ECS Fargate | Pull-based, **local runner** (no Docker-in-Docker on Fargate) |
| Database | RDS PostgreSQL | Encrypted, deletion-protected, optional Multi-AZ |
| Object storage | S3 | `tfstate`, logs, artifacts + SPA assets (separate bucket) |
| Secrets | Secrets Manager + KMS | All sensitive config; never in task env vars |
| TLS | ACM + CloudFront | Certificate in `us-east-1` (CloudFront requirement) |
| Container registry | ECR | Two repos: `api` and `worker` |

The ALB is **internal** — it has no public IP. CloudFront reaches it through a **VPC Origin**
(CloudFront creates ENIs in your private subnets). Nothing in the architecture requires a public
load balancer or static cloud credentials.

!!! warning "Fargate runner"
    Fargate has no Docker daemon, so workers run with `STACKD_RUNNER=local`. The tools
    (`tofu`/`terraform`, `tfsec`, `infracost`, `jq`) must be bundled in your worker image.
    If you need full container isolation per run, use an ECS cluster on EC2 with Docker available
    and set `STACKD_RUNNER=docker` in the worker environment variables.

## Prerequisites

- Terraform or OpenTofu ≥ 1.10
- AWS CLI configured (`aws configure` or environment variables)
- An AWS account with permissions to create IAM roles, ECS, RDS, S3, CloudFront, Secrets Manager
- *(Recommended)* A custom domain with a Route 53 hosted zone

## Quick start

```bash
cd deploy/terraform/examples/minimal
cp terraform.tfvars.example terraform.tfvars
# set aws_region at minimum — see variable reference below
tofu init
tofu apply
```

The first apply creates all infrastructure. ECS tasks will fail to start because no images are
pushed yet — that is expected. Proceed with the bootstrap sequence below.

## Bootstrap sequence

### 1 — Push container images

After `tofu apply`, two ECR repositories are ready:

```bash
# Authenticate to ECR
aws ecr get-login-password --region $REGION | \
  docker login --username AWS --password-stdin \
  $(tofu output -raw ecr_api_repository_url | cut -d/ -f1)

# Build and push
docker build -t $(tofu output -raw ecr_api_repository_url):latest api/
docker push $(tofu output -raw ecr_api_repository_url):latest

docker build -t $(tofu output -raw ecr_worker_repository_url):latest worker/
docker push $(tofu output -raw ecr_worker_repository_url):latest

# Re-apply to update the ECS task definitions
tofu apply \
  -var "api_image=$(tofu output -raw ecr_api_repository_url):latest" \
  -var "worker_image=$(tofu output -raw ecr_worker_repository_url):latest"
```

### 2 — Set up Google OIDC (recommended)

!!! note "Skip for now"
    You can complete the bootstrap without Google OIDC and add it later. When both
    `google_client_id` and `google_client_secret` are empty, only the dev login is available —
    **do not leave dev login enabled on a production instance**.

1. Go to [Google Cloud Console → APIs & Services → Credentials](https://console.cloud.google.com/apis/credentials).
2. Create an **OAuth 2.0 Client ID** → Web application.
3. Add the redirect URI shown by Stackd:
   ```bash
   tofu output google_oauth_redirect_uri
   # e.g. https://stackd.acme.com/api/v1/auth/google/callback
   ```
4. Apply with the credentials:
   ```bash
   tofu apply \
     -var "google_client_id=<client-id>" \
     -var "google_client_secret=<client-secret>" \
     -var "google_allowed_domains=acme.com"
   ```

### 3 — Custom domain (if not set from the start)

If you did not set `domain_name` on the first apply, the instance runs on the CloudFront default
domain (`*.cloudfront.net`). To switch to a custom domain later:

```bash
tofu apply -var "domain_name=stackd.acme.com" -var "route53_zone_id=Z123..."
```

If your DNS is not in Route 53, `tofu output acm_validation_records` gives the CNAME records to
create manually. Apply a second time once the certificate is validated.

### 4 — Log in and create a worker pool

Open `tofu output -raw public_url` in a browser, log in, then navigate to
**Settings → Workers → New pool**. Copy the token — it is shown only once.

### 5 — Register the worker pool token

```bash
tofu apply -var "worker_pool_token=wpt_..."
```

This stores the token in Secrets Manager and injects it into the worker ECS task. Workers will
register with the API automatically within a minute (ECS task replacement).

### 6 — Verify

On the **Workers & health** page, one or more workers should appear as `online`. Create a stack,
trigger a plan, and watch the run page light up.

---

## Variable reference

| Variable | Default | Description |
|---|---|---|
| `name` | `"stackd"` | Name prefix for all resources |
| `aws_region` | — | AWS region |
| `availability_zones` | — | AZ list (min 2) |
| `domain_name` | `null` | Custom domain; null = CloudFront default |
| `route53_zone_id` | `null` | Route 53 zone for automated DNS + cert validation |
| `certificate_arn` | `null` | BYO ACM certificate (us-east-1); overrides `domain_name` |
| `google_client_id` | `""` | Google OAuth Client ID |
| `google_client_secret` | `""` | Google OAuth Client Secret |
| `google_allowed_domains` | `""` | Comma-separated `hd` allowlist (e.g. `"acme.com"`) |
| `worker_pool_token` | `""` | Pool token from step 4 |
| `db_instance_class` | `db.t4g.medium` | RDS instance class |
| `db_multi_az` | `false` | Enable Multi-AZ standby |
| `api_cpu` / `api_memory` | `512` / `1024` | ECS task sizing for the API |
| `worker_cpu` / `worker_memory` | `1024` / `2048` | ECS task sizing per worker |
| `worker_desired_count` | `1` | Initial number of workers |
| `worker_autoscaling_enabled` | `false` | Enable Application Auto Scaling |
| `worker_autoscaling_min_count` | `1` | Autoscaling floor |
| `worker_autoscaling_max_count` | `10` | Autoscaling ceiling |
| `worker_autoscaling_cpu_target` | `70` | CPU % target for the scaling policy |
| `api_image` / `worker_image` | *(ECR latest)* | Full image URI; set after pushing |
| `tags` | `{}` | Extra tags on all resources |

## Examples

Two ready-to-use examples are in `deploy/terraform/examples/`:

**`minimal/`** — single region, two AZs, `db.t4g.micro`, `256` CPU / `512` MB API. For evaluation
and small teams. No autoscaling.

**`production/`** — three AZs, `db.r8g.large` Multi-AZ, `1024` CPU / `2048` MB API × 2,
`2048` CPU / `4096` MB workers with autoscaling (min 2, max 20, CPU target 70%).

## Autoscaling workers

When `worker_autoscaling_enabled = true`, Stackd configures an Application Auto Scaling
TargetTracking policy on the worker ECS service:

- **Metric**: `ECSServiceAverageCPUUtilization`
- **Scale out**: within 60s when CPU exceeds the target — keeps the job queue from building up
- **Scale in**: after 300s below the target — conservative to avoid thrashing

The environment is the unit of parallelism (one active run per environment). Adding workers beyond
the number of environments with queued work does not increase throughput — they will idle. See
[Workers & scaling](workers-and-scaling.md#scale) for the full picture.

## State backend

Stackd's own `tfstate` is in S3. Set up a remote backend before collaborating:

```hcl
# In examples/production/main.tf, uncomment:
backend "s3" {
  bucket = "my-tfstate-bucket"
  key    = "stackd/production/terraform.tfstate"
  region = "eu-west-1"
}
```

Run `tofu init -migrate-state` after adding the backend to move the local state to S3.

## Upgrading

Stackd uses `deletion_protection = true` on the RDS instance. Destructive schema changes require
disabling it temporarily:

```bash
tofu apply -var "..." # normal upgrade — no action needed for most changes
```

For a major Postgres version upgrade, see the [RDS upgrade guide](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_UpgradeDBInstance.PostgreSQL.html).

## See also

- [Workers & scaling](workers-and-scaling.md)
- [Cloud credentials (OIDC)](cloud-credentials.md)
- [Exit & data portability](leaving.md) — how to migrate away
