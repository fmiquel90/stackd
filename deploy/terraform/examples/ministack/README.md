# The AWS module against MiniStack

[MiniStack](https://ministack.org) is an MIT-licensed local AWS emulator (one port, `:4566`,
LocalStack-compatible endpoints). This example applies the **whole** module to it, with no AWS
account and no cost.

```bash
task tf:check      # tofu fmt + validate: module and every example (no emulator needed)
task tf:sim        # start MiniStack, apply, check, re-plan, destroy
KEEP=1 task tf:sim # same, but leave it applied to inspect it
task tf:sim:down   # stop MiniStack
```

`task tf:sim` (`deploy/terraform/scripts/sim.sh`) fails when:

- a resource, an argument or a reference is refused at apply time;
- a generated secret (master key, JWT secret, DB password) shows up in the state — they must only
  go through write-only arguments;
- the second plan is not empty (a perpetual diff);
- something is left in the state after the destroy.

CI runs the same checks (job `terraform`).

## What it does and does not prove

| Proven | Not proven (needs a real account) |
|---|---|
| Every resource and argument is accepted by the API shapes | CloudFront VPC origin routing: control plane only in MiniStack |
| The dependency graph applies and destroys in order | IAM and KMS policy evaluation (who may `kms:Sign`, read which secret) |
| Write-only secrets stay out of the state | EC2 user data on a real host, the Docker runner, the SIGTERM drain hook |
| No perpetual diff | RDS engine availability (PostgreSQL 18 in your region) |
| Outputs and module wiring | ACM DNS validation through real DNS; HTTPS end to end |

Simulation-only settings in `main.tf`: fake image digests (MiniStack has no Docker socket here,
so nothing is pulled or run), a fixed AMI id (no public SSM AMI parameters), a Route 53 zone
created in the emulator, and `run_migrations = false` (the migration task needs a live cluster).

Port `4566` is also used by the dev stack's LocalStack (`task dev-aws`): run one at a time.
