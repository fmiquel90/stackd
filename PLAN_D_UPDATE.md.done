# PLAN_D_UPDATE.md — Phase D: HCL-syntax variables

> Status: **todo** · Prio P2 · Effort S · Risk L. Spec: `SPECS_D_UPDATE.md`. Index: `PLAN_UPDATE.md`.
> When shipped, rename to `PLAN_D_UPDATE.md.done` (+ `SPECS_D_UPDATE.md.done`).

**Goal**: support real HCL values (`{ a = "b" }`, expressions) for `hcl` variables, not just JSON.

- **In**: for `hcl` terraform vars, the worker writes a generated **`.auto.tfvars` (HCL)** file
  (`name = <raw value>`) and **excludes them from the JSON tfvars** (else double-defined); non-hcl
  stays JSON. Supersedes the shipped `_tfvar_value` JSON-parse for hcl vars; payload must carry
  per-var hcl-ness.
- **Out**: server-side HCL validation (terraform validates at plan).
- **Touches**: `workers/claim.py` (carry hcl flag per var), `worker/agent/{main,workspace}.py`.
- **Acceptance**: an `hcl` var `{ a = "b" }` reaches terraform as an object; `["a","b"]` as a list;
  the var is **not** defined twice.
