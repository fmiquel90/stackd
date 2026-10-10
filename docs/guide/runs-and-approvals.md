# Runs & approvals

A **run** is one execution against one environment. It always pauses for a human between `plan` and
`apply` — that approval gate, and who's allowed through it, is the heart of Stackd.

## The lifecycle

```
queued → preparing → planning → [checking] → unconfirmed → confirmed → applying → finished
```

A worker claims a `queued` run and executes its **plan job**: it clones the repo, runs hooks +
`plan`, and reports back. With a non-empty diff the run stops at **`unconfirmed`** and waits for a
human. After **confirm**, a worker (not necessarily the same one) claims the **apply job** and
drives the run to **`finished`**. An empty diff goes straight to `finished`; a
failure to `failed`. A human can `discard` an `unconfirmed` run, or `cancel` a `queued` or
`unconfirmed` one (stopping a run a worker is executing is not supported yet).

!!! note
    State only ever changes through one function (`transition()`), which records a `run_event` and
    — for human/terminal actions — an `audit_event` in the **same transaction**. Nothing changes a
    run's state out of band.

## What gets applied is what was reviewed

Two guarantees tie the apply to the plan a human approved:

- **The run is pinned to a commit.** A run triggered without a commit (a click, a cascade) plans
  the head of the environment's branch, and the plan job reports the exact commit it checked out:
  the run is pinned to it. A run triggered on a branch or tag name is pinned to the commit that name
  resolved to; a run triggered on a full SHA must check out exactly that commit, or it fails
  (`commit_mismatch`). The apply clones exactly the pinned commit, so its hooks and `output -json`
  run on the reviewed code too, and [staleness](stacks-and-environments.md#refreshing-the-tracked-head)
  knows what was applied.
- **The apply runs the saved plan.** For a tracked or destroy run with changes, the plan job uploads
  its `plan.tfplan` before any `after_plan` hook runs. The API encrypts it and keeps it in S3 while
  the run can still be applied. The apply job downloads it and runs `tofu apply plan.tfplan` — it
  never re-plans. If the saved plan is missing or unreadable, the run fails before the
  `before_apply` hooks (`saved plan unavailable, not applied`). If the state or an input changed
  since the plan, tofu refuses the stale plan and the run fails with tofu's reason. In both cases,
  **trigger a new plan**. The saved plan is deleted as soon as the run ends, whatever the outcome.

A plan file holds the run's variable values, sensitive ones included: it is encrypted with the
API's key on top of the bucket's encryption, written `0600` on the apply worker, and removed right
after the apply (the `after_apply` hooks never see it).

## Who can approve — `can_apply`

Confirming an apply requires **both**:

1. **Role** ∈ `{approver, admin}` — a `writer` may trigger a plan but never approve.
2. **The environment's `tier` is in your `allowed_tiers`** set. Tiers are a configurable catalog
   (not an ordered `dev<staging<prod`), so a grant can be non-contiguous — e.g. `{dev, prod}`
   confirms in dev and prod but not staging.

A `destroy` run additionally requires the `can_destroy` permission. A tier flagged
`requires_four_eyes` (e.g. `prod`) also forces the triggerer ≠ the confirmer.

These are your **effective** permissions in the environment's space: a space membership can set a
different role, tier set or destroy right than your instance defaults.

| Persona | Can trigger a plan | Can approve dev/staging | Can approve prod |
|---|---|---|---|
| writer (`bob`) | ✅ | ❌ (needs approver) | ❌ |
| approver with `{dev, staging}` | ✅ | ✅ | ❌ (tier) |
| approver/admin with `prod` in their set (`alice`, `admin`) | ✅ | ✅ | ✅ |

## Four-eyes

On an environment whose tier is flagged `requires_four_eyes` (the seeded `prod`), a `protected`
environment, or any env with `require_second_pair_of_eyes`, the person who **triggered** the run
cannot be the one who **confirms** it — another authorized approver must. It only applies to runs
a human triggered: a webhook or cascade run has no triggerer to oppose, so any authorized approver
can confirm it.

## Friction proportional to risk

An authorized apply on an ordinary tier is one click. Confirming on a tier flagged
`requires_four_eyes` or any **`destroy`** run opens a confirmation popover: type the **environment name** + review a `+a ~c −d` plan summary. This
is a misclick safeguard only — it never bypasses `can_apply` (still evaluated server-side).

## Autodeploy & checks

An environment with `autodeploy` auto-confirms when the plan is clean — **but a `warn` check forces
`unconfirmed`** so a human still sees it. A `protected` environment can never autodeploy, and
requires four-eyes.

Every [hook](hooks.md) result is kept on the run with the stage it ran at, from the plan job and
then the apply job: `check_results.results` lists them all (`ok`, `warn`, `fail`),
`check_results.checks` only the ones that warned or failed — what the run page shows. A `fail`
check fails the run at its stage; a failing `before_apply` check means nothing is applied. See
[Hooks — check results](hooks.md#check-results).

## Mock block

A run that consumed a mock upstream output (`used_mocks=true`) **cannot be applied** unless the env
sets `allow_mock_apply=true` — see [Dependencies & mocks](dependencies-and-mocks.md).

## Promotion (trunk-based)

To roll out the **exact commit** running on one environment to another of the same stack
(dev → staging → prod), use **promote** — the env's **Promote** tab, or:

```bash
curl -s -X POST localhost:8000/api/v1/environments/$STAGING_ID/promote -H "$AUTH" \
  -d '{"from_environment_id": "'$DEV_ID'"}'
```

It pins a new tracked run on the target to the source's last applied commit, so you deploy *what
was tested* — not whatever HEAD happens to be. The apply is gated as usual (tier + four-eyes) at
confirm. This is the same-stack counterpart to the cross-stack [cascade](dependencies-and-mocks.md).

## Proposed runs (from a PR)

A pull request triggers a **proposed** run: plan-only, with a read-only state token — a preview
that is never applied and saves no plan file. See [the Git integration in SPECS](../SPECS.md).

A pull request's code runs inside tofu (`data "external"`, provisioners), and nobody has reviewed
it yet: a proposed run gets **none of the environment's secrets**. No sensitive variable, of
either kind, and no [external secret](external-secrets.md) (not even fetched) reaches its job.
What a plan needs stays: the cloud credentials of the **plan role**, read-only by design (see
[Cloud credentials](cloud-credentials.md)), the read-only state token and the repo token to clone.

!!! warning "What a proposed run still reaches"
    A plan that needs a secret gets no value for it: tofu uses the variable's default, or fails
    with `No value for required variable`. Plan with credentials that need no static secret (the
    OIDC plan role) for pull requests to plan. The pull request's code can still read the state
    (secrets a resource keeps there included) and use the plan role: keep that role read-only and
    away from secret stores. A per-environment opt-in giving proposed runs the secrets is specified
    but not implemented. A drift check is a proposed run too, of the last applied commit: it keeps
    the secrets.

## Triggering over the API

```bash
curl -s -X POST localhost:8000/api/v1/environments/$ENV_ID/runs -H "$AUTH" \
  -H 'content-type: application/json' -d '{}'                                         # trigger plan
curl -s -X POST localhost:8000/api/v1/runs/$RUN_ID/confirm -H "$AUTH"                 # approve → apply
curl -s -X POST localhost:8000/api/v1/runs/$RUN_ID/discard -H "$AUTH"                 # discard
```

## See also

- [Variables & secrets](variables.md) · [Hooks & `.stackd.yml`](hooks.md) · [Notifications](notifications.md)
- [Concepts](../CONCEPTS.md) · [SPECS](../SPECS.md)
