# Variables & secrets

Stackd composes the configuration of every run from layers. The same variable name can be defined in several places; the most specific layer wins, and every resolved value carries its **provenance** so you can see where it came from.

## The two kinds

Each variable has a `kind` that decides how the worker injects it:

| Kind | Injection |
|---|---|
| `terraform` | exposed as `TF_VAR_<name>` (and written to a tfvars file) |
| `environment` | exposed as a plain process environment variable for the run |

## Where a variable can live

- **Stack variable** — common to all environments of a stack (`environment_id` is null).
- **Environment variable** — scoped to a single environment.
- **Variable set** — a named, reusable bundle (e.g. `common-aws`, `datadog`, `region-eu`) that belongs to one space and that you attach to tiers, stacks or envs of that space. Set members are never scoped to an env directly; the *attachment* carries the targeting.

## Resolution order (6 layers)

At claim time the value of each variable is resolved from weakest to strongest. The strongest layer that defines a given `(name, kind)` wins.

```mermaid
flowchart LR
  A["1· sets by rule<br/>(auto_attach, selector)"] --> T["2· sets attached to tier"]
  T --> B["3· sets attached to stack"]
  B --> C["4· sets attached to env"]
  C --> D["5· stack variables"]
  D --> E["6· environment variables<br/>(always wins)"]
```

| Layer | Source | Ordered by |
|---|---|---|
| 1 | variable sets that apply by rule: `auto_attach`, or a `selector` matching the env's labels | set name |
| 2 | variable sets attached to the env's **tier** | increasing `priority` |
| 3 | variable sets attached to the stack | increasing `priority` |
| 4 | variable sets attached to the env | increasing `priority` |
| 5 | stack variables | — |
| 6 | environment variables | — (always wins) |

Only sets of the environment's own space take part. A `selector` (`{"label": "value", …}`) matches an env whose effective labels (stack labels + env labels, env wins) contain every pair. Two more sources are merged in at claim time but sit outside this order: upstream outputs (`dependency:`) and mocks (`mock`) — see [Concepts](../CONCEPTS.md) and [SPECS](../SPECS.md) §9.

## Inspecting resolved values

`GET /environments/{id}/resolved-variables` returns the fully resolved set, each entry tagged with its `injected_name` and `provenance`:

```bash
curl -s localhost:8000/api/v1/environments/$ENV/resolved-variables \
  -H "Authorization: Bearer $TOKEN" | jq
```

```json
[
  { "name": "region", "injected_name": "TF_VAR_region", "value": "us-east-1", "provenance": "set:region-us" },
  { "name": "cidr",   "injected_name": "TF_VAR_cidr",   "value": "10.9.0.0/16", "provenance": "env" }
]
```

The provenance snapshot is frozen onto the run (`variable_provenance`) for audit, and the UI uses it to show "inherited from `common-aws`", "overridden here" or `MOCK`.

## Variable sets

Create a set, add members, then attach it. `auto_attach: true` attaches it to every stack in the space; otherwise attach explicitly to a `tier`, `stack` or `environment`, with a `priority`. A set is created in the `default` space unless you pass `space_id`.

```bash
SET=$(curl -s -X POST localhost:8000/api/v1/variable-sets \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"name":"common-aws","auto_attach":true}' | jq -r .id)

curl -s -X POST localhost:8000/api/v1/variable-sets/$SET/attachments \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"target_kind":"environment","target_id":"'"$ENV"'","priority":10}'
```

### Attachments stay inside the set's space

A variable set only ever reaches its own space:

- A **stack** or **environment** attachment requires the target to live in the set's space, and you
  need `writer` on it. Otherwise the attach is refused: `400 Cross-space attachment` (or `403` when
  you can't write the target).
- A **tier** attachment (`target_id` = the tier's id from `GET /api/v1/tiers`) applies to that
  tier's environments **inside the set's space only**. Tiers are a global catalog; attaching
  `prod-credentials` to the `prod` tier never injects it into another space's prod environments.
- At resolution, an attachment from a set of another space is ignored, even one created before
  this rule existed.

!!! note
    Detach a set before deleting it. Deleting an attached set returns **409** with the list of attachments — explicit detachment is required. Deleting a stack or an environment removes the attachments that target it for you (audited `variable_set.detached` with the reason).

## Sensitive values

Mark a variable `sensitive: true` to make it write-only:

```bash
curl -s -X POST localhost:8000/api/v1/stacks/$STACK/variables \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"kind":"environment","name":"DD_API_KEY","value":"super-secret","sensitive":true}'
```

The value is **AES-256-GCM encrypted at rest**, decrypted only to build the worker claim payload, and the agent masks the literal value in logs.

!!! warning
    A sensitive value is **never returned in clear**. A later `GET` shows `"value": "•••"`; the plaintext is not readable again through the API.

## Values by reference (external secret sources)

Instead of storing a secret in Stackd, a variable can point at an entry in an external secrets manager (Proton Pass today). The value is fetched **live at run time** and never persisted — see [External secret sources](external-secrets.md).

## See also

- [Stacks & environments](stacks-and-environments.md)
- [External secret sources](external-secrets.md)
- [Concepts](../CONCEPTS.md)
- [SPECS](../SPECS.md) §3.4, §15
