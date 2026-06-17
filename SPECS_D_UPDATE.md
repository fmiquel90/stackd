# SPECS_D_UPDATE.md — HCL-syntax variables (Phase D)

> Plan: `PLAN_D_UPDATE.md`. Folds into `docs/SPECS.md` (§3.4) when shipped. No schema change.

**Supersedes** the shipped `_tfvar_value` JSON-parse *for hcl vars*: an `hcl` var is written to the
HCL tfvars file **only** and **excluded from `stackd.auto.tfvars.json`** — otherwise it would be
defined twice (JSON + HCL) and terraform would error / last-wins non-deterministically.

- The claim payload must carry per-var `hcl`-ness (today `tfvars_json` is a flat name→value dict; add
  an `hcl_tfvars` map, or a `{value, hcl}` shape). Known server-side, just not yet in the payload.
- **Worker**: write `hcl` vars to a generated **`zzz_stackd.auto.tfvars` (HCL)** as `name = <raw
  value>` (verbatim) so real HCL syntax (`{ a = "b" }`, function calls) parses natively; write non-hcl
  vars to `stackd.auto.tfvars.json` as today. Both auto-load.
- Masking still applies to the HCL file content (sensitive hcl values).
- Net: `_tfvar_value` becomes a no-op for hcl vars (they leave the JSON path); keep it only for any
  value that must remain JSON.

## Invariants
Resolution order (§3.4) unchanged — this is purely *how* a resolved value is written to disk.

> Note: dependency **outputs** are already typed (`EnvOutput.value` is JSONB; `capture_outputs`
> stores native values) — they are **not** affected and need no change.
