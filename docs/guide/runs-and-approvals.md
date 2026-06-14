# Runs & approvals

A **run** is one execution against one environment. It always pauses for a human between `plan` and
`apply` — that approval gate, and who's allowed through it, is the heart of Stackd.

## The lifecycle

```
queued → preparing → planning → [checking] → unconfirmed → confirmed → applying → finished
```

A worker claims a `queued` run, clones the repo at the pinned commit, runs hooks + `plan`, and
reports back. With a non-empty diff the run stops at **`unconfirmed`** and waits for a human. After
**confirm**, a worker claims the apply and drives it to **`finished`**. An empty diff goes straight
to `finished`; a failure to `failed`; a human can `discard` or `cancel`.

!!! note
    State only ever changes through one function (`transition()`), which records a `run_event` and
    — for human/terminal actions — an `audit_event` in the **same transaction**. Nothing changes a
    run's state out of band.

## Who can approve — `can_apply`

Confirming an apply requires **both**:

1. **Role** ∈ `{approver, admin}` — a `writer` may trigger a plan but never approve.
2. **`max_apply_tier` ≥ the environment's `tier`** (`dev < staging < prod`). An approver capped at
   `staging` can apply dev/staging, not prod.

A `destroy` run additionally requires the `can_destroy` permission.

| Persona | Can trigger a plan | Can approve dev/staging | Can approve prod |
|---|---|---|---|
| writer (`bob`) | ✅ | ❌ (needs approver) | ❌ |
| approver capped staging | ✅ | ✅ | ❌ (tier) |
| approver/admin, prod ceiling (`alice`) | ✅ | ✅ | ✅ |

## Four-eyes on prod

On a `prod` environment (or any env with `require_second_pair_of_eyes`), the person who **triggered**
the run cannot be the one who **confirms** it — a second approver is required.

## Friction proportional to risk

An authorized non-prod apply is one click. Confirming on **`tier=prod`** or any **`destroy`** run
opens a confirmation popover: type the **environment name** + review a `+a ~c −d` plan summary. This
is a misclick safeguard only — it never bypasses `can_apply` (still evaluated server-side).

## Autodeploy & checks

An environment with `autodeploy` auto-confirms when the plan is clean — **but a `warn` check forces
`unconfirmed`** so a human still sees it. A `protected` environment can never autodeploy.

## Mock block

A run that consumed a mock upstream output (`used_mocks=true`) **cannot be applied** unless the env
sets `allow_mock_apply=true` — see [Dependencies & mocks](dependencies-and-mocks.md).

## Proposed runs (from a PR)

A pull request triggers a **proposed** run: plan-only, read-only state, no secrets — a safe preview
that is never applied. See [the Git integration in SPECS](../SPECS.md).

## Triggering over the API

```bash
curl -s -X POST localhost:8000/api/v1/environments/$ENV_ID/runs -H "$AUTH" -d '{}'   # trigger plan
curl -s -X POST localhost:8000/api/v1/runs/$RUN_ID/confirm -H "$AUTH"                 # approve → apply
curl -s -X POST localhost:8000/api/v1/runs/$RUN_ID/discard -H "$AUTH"                 # discard
```

## See also

- [Variables & secrets](variables.md) · [Hooks & `.stackd.yml`](hooks.md) · [Notifications](notifications.md)
- [Concepts](../CONCEPTS.md) · [SPECS](../SPECS.md)
