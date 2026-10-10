from __future__ import annotations

import json
from typing import Any

MASK = "***"


class Masker:
    """Replaces every known secret value with *** before logs leave the worker (SPECS §5.1).

    Limitation (documented in §5.1): a transformed secret (base64, substring) escapes value-based
    masking — do not put exploitable secrets in non-sensitive tfvars.
    """

    def __init__(self, secrets: list[str]) -> None:
        # Longest first so overlapping secrets are fully masked.
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def mask(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, "***")
        return text

    def scan(self, text: str) -> bool:
        """Cleartext tripwire (§5.1): True if any known secret appears verbatim in `text`. Used to
        flag a run when a sensitive value leaks into a place terraform should have redacted (a
        non-sensitive output, plan.json). Narrows — does not eliminate — the leak surface: a
        transformed secret (base64/substring) still escapes value-based detection."""
        return any(secret in text for secret in self._secrets)


def _redact_marked(value: Any, marks: Any) -> Any:
    """`value` with what tofu marks sensitive masked. `marks` mirrors its shape (a plan JSON's
    `sensitive_values` / `before_sensitive` / `after_sensitive`): `true` marks the whole (sub)value."""
    if marks is True:
        return value if value is None else MASK
    if isinstance(marks, dict) and isinstance(value, dict):
        return {k: _redact_marked(v, marks.get(k)) for k, v in value.items()}
    if isinstance(marks, list) and isinstance(value, list):
        return [
            _redact_marked(v, marks[i] if i < len(marks) else None) for i, v in enumerate(value)
        ]
    return value


def _redact_module(module: Any) -> None:
    if not isinstance(module, dict):
        return
    for res in module.get("resources") or []:
        if isinstance(res, dict) and "values" in res:
            res["values"] = _redact_marked(res["values"], res.get("sensitive_values"))
    for child in module.get("child_modules") or []:
        _redact_module(child)


def _redact_state_values(values: Any) -> None:
    """`planned_values` / `prior_state.values`: sensitive outputs, then every module's resources."""
    if not isinstance(values, dict):
        return
    for out in (values.get("outputs") or {}).values():
        if isinstance(out, dict) and out.get("sensitive") and "value" in out:
            out["value"] = _redact_marked(out["value"], True)
    _redact_module(values.get("root_module"))


def _redact_change(change: Any) -> None:
    if isinstance(change, dict):
        for side in ("before", "after"):
            if side in change:
                change[side] = _redact_marked(change[side], change.get(f"{side}_sensitive"))


def _mask_leaves(node: Any, masker: Masker) -> Any:
    """The log masker over every key and string of a parsed document: parsed, so a secret the JSON
    escapes (tofu writes `<` as `\\u003c`) is still found. A number that matches is masked whole."""
    if isinstance(node, dict):
        return {masker.mask(k): _mask_leaves(v, masker) for k, v in node.items()}
    if isinstance(node, list):
        return [_mask_leaves(v, masker) for v in node]
    if isinstance(node, str):
        return masker.mask(node)
    if isinstance(node, int | float) and not isinstance(node, bool):
        return MASK if masker.scan(json.dumps(node)) else node
    return node


def mask_plan_json(doc: str, masker: Masker) -> str:
    """The plan JSON (`tofu show -json`) a repo hook may read (§8.3): every value tofu marks
    sensitive (input variables declared `sensitive`, resource attributes, outputs) and every value
    the log masker knows (§5.1) replaced with ***. The rest is kept, as valid JSON. A document that
    is not JSON is masked as text, like a log line."""
    try:
        plan = json.loads(doc)
    except ValueError:
        return masker.mask(doc)
    if isinstance(plan, dict):
        config = (plan.get("configuration") or {}).get("root_module") or {}
        declared = config.get("variables") or {}
        for name, var in (plan.get("variables") or {}).items():
            if (
                isinstance(var, dict)
                and "value" in var
                and (declared.get(name) or {}).get("sensitive")
            ):
                var["value"] = _redact_marked(var["value"], True)
        _redact_state_values(plan.get("planned_values"))
        _redact_state_values((plan.get("prior_state") or {}).get("values"))
        for rc in [*(plan.get("resource_changes") or []), *(plan.get("resource_drift") or [])]:
            if isinstance(rc, dict):
                _redact_change(rc.get("change"))
        for oc in (plan.get("output_changes") or {}).values():
            _redact_change(oc)
    return json.dumps(_mask_leaves(plan, masker))
