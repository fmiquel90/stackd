# Cloud credentials via OIDC

Stackd hands each run **short-lived, per-phase cloud credentials** instead of static
access keys. Stackd is itself an OIDC issuer: at claim time it signs a workload token
scoped to the run, and AWS exchanges that token for an IAM role via
`AssumeRoleWithWebIdentity`. There are **no static AWS keys** stored anywhere.

The token subject is the basis of every trust policy:

```
sub = run:<tier>:<stack>:<phase>      e.g.  run:prod:core-network:apply
```

!!! note "Plan and apply assume different roles"
    A `plan` run and an `apply` run get tokens with different `phase` segments and
    therefore assume **different IAM roles**: `plan` is read-only, `apply` is write.
    A PR plan physically cannot modify infrastructure.

## How the issuer works

Stackd exposes a standard OIDC discovery surface, RS256-signed:

```
GET /.well-known/openid-configuration   → issuer, jwks_uri, alg RS256
GET /oidc/jwks                          → public keys (kid, with rotation overlap)
```

AWS STS fetches the JWKS from these endpoints to validate the workload token.

!!! warning "The issuer must be publicly reachable over HTTPS"
    AWS STS calls `/.well-known/openid-configuration` and `/oidc/jwks` directly. The
    Stackd issuer URL must be reachable from AWS over HTTPS — a private-only deployment
    will fail every `AssumeRoleWithWebIdentity`. Expose it publicly, or use the provided
    Terraform module that registers the identity provider with its thumbprint.

## 1. Configure the cloud provider (AWS)

On the AWS account, create an **IAM OIDC identity provider** trusting the Stackd issuer,
then the roles each run will assume. A provided Terraform module sets this up.

Use a separate role per phase:

- `plan_role_arn` → a **ReadOnly** role (plus access to the state backend).
- `apply_role_arn` → a **scoped write** role.

Each role's trust policy filters on the `sub` claim. The wildcard covers **only the
`stack` segment** — `tier` and `phase` are always pinned:

```hcl
# Trust policy of the prod APPLY role — refuses everything else
condition {
  test     = "StringLike"
  variable = "stackd.example.com:sub"
  values   = ["run:prod:*:apply"]   # any stack, but tier=prod AND phase=apply
}
```

!!! warning "Never wildcard `tier` or `phase`"
    A trust policy that left the tier or phase open (`run:*:*:*`) would cancel the
    double lock. The AWS-side `tier` filter is the second half of the apply guard: even
    if the API were bypassed, STS refuses to mint prod-write credentials for a non-prod
    token. Keep `tier` and `phase` fixed in every condition.

## 2. Attach a cloud integration to the environment

A `cloud-integration` is configured **per environment** (so each region can point at a
different role or even a different AWS account). The resource is CRUD over:

```
GET|PUT|DELETE /api/v1/environments/{id}/cloud-integration
```

```bash
curl -s -X PUT localhost:8000/api/v1/environments/$PROD/cloud-integration -H "$AUTH" -d '{
  "provider": "aws",
  "plan_role_arn":  "arn:aws:iam::123:role/stackd-prod-plan",
  "apply_role_arn": "arn:aws:iam::123:role/stackd-prod-apply",
  "region": "eu-west-1",
  "session_duration": 3600
}'
```

AWS is the MVP target; GCP and Azure come later.

## 3. Test it

A built-in **AssumeRole test** verifies the trust chain without launching a run:

```bash
curl -s -X POST localhost:8000/api/v1/environments/$PROD/cloud-integration/test -H "$AUTH"
```

It signs a probe token and attempts the `AssumeRoleWithWebIdentity` end to end — a fast
way to catch a missing identity provider, a too-narrow `sub` condition, or an
unreachable issuer.

## What the agent injects

When a `cloud_integration` is set, the worker writes the signed token to a file and
exports, **to the terraform process only**:

```
AWS_WEB_IDENTITY_TOKEN_FILE = <path to the token file>
AWS_ROLE_ARN                = <plan_role_arn | apply_role_arn for this phase>
AWS_ROLE_SESSION_NAME       = stackd-<run_id>
```

The AWS SDK inside the providers performs the AssumeRoleWithWebIdentity natively. The
session name ties every CloudTrail entry back to the run — and thus to the human who
confirmed the apply.

These variables are **never** exported to repo hooks (a `.stackd.yml` pushed via PR must
not be able to assume the apply role and exfiltrate). Platform hooks can opt in. See
[Hooks](hooks.md).

## Fallback and coexistence

With no `cloud_integration`, Stackd falls back to classic static variables (the
`aws-credentials` variable set). If both an integration **and** static `AWS_*` variables
are resolved, the OIDC credentials win and a configuration warning is shown.

## See also

- [Runs & approvals](runs-and-approvals.md)
- [Hooks](hooks.md)
- [SPECS §10 — Dynamic cloud credentials](../SPECS.md)
