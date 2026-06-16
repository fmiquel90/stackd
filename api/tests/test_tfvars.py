from __future__ import annotations

from app.enums import VariableKind
from app.variables.resolution import ResolvedVariable
from app.workers.claim import _tfvar_value


def _rv(value: str | None, hcl: bool) -> ResolvedVariable:
    return ResolvedVariable(
        name="v",
        kind=VariableKind.terraform,
        sensitive=False,
        hcl=hcl,
        provenance="env",
        value=value,
    )


def test_hcl_list_becomes_real_list():
    assert _tfvar_value(_rv('["a", "b"]', True)) == ["a", "b"]


def test_hcl_map_and_scalars():
    assert _tfvar_value(_rv('{"a": "b"}', True)) == {"a": "b"}
    assert _tfvar_value(_rv("42", True)) == 42
    assert _tfvar_value(_rv("true", True)) is True


def test_non_hcl_keeps_string():
    # Without the hcl flag the value is a plain string, even if it looks like a list.
    assert _tfvar_value(_rv('["a", "b"]', False)) == '["a", "b"]'


def test_hcl_invalid_json_falls_back_to_string():
    # HCL-only syntax that isn't valid JSON is left as-is rather than crashing the claim.
    assert _tfvar_value(_rv("{ a = b }", True)) == "{ a = b }"


def test_none_value_passes_through():
    assert _tfvar_value(_rv(None, True)) is None
