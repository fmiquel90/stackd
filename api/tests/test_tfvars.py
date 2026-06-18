from __future__ import annotations

from app.enums import VariableKind
from app.variables.resolution import ResolvedVariable
from app.workers.claim import _is_hcl_tfvar


def _rv(
    value: str | None, hcl: bool, kind: VariableKind = VariableKind.terraform
) -> ResolvedVariable:
    return ResolvedVariable(
        name="v",
        kind=kind,
        sensitive=False,
        hcl=hcl,
        provenance="env",
        value=value,
    )


def test_hcl_var_with_value_routes_to_hcl_file():
    # An hcl var with a value is written verbatim to the HCL tfvars file, not the JSON one.
    assert _is_hcl_tfvar(_rv('{ a = "b" }', True)) is True
    assert _is_hcl_tfvar(_rv('["a", "b"]', True)) is True


def test_non_hcl_var_stays_json():
    assert _is_hcl_tfvar(_rv('["a", "b"]', False)) is False


def test_hcl_var_without_value_stays_json():
    # No raw value → nothing to write verbatim; falls back to the JSON path (null).
    assert _is_hcl_tfvar(_rv(None, True)) is False


def test_env_var_is_never_hcl_tfvar():
    assert _is_hcl_tfvar(_rv("x", True, kind=VariableKind.environment)) is False
