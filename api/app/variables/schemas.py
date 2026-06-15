from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, model_validator

from app.enums import SecretFallback, VariableKind
from app.models.variable import Variable
from app.variables.resolution import ResolvedVariable
from app.variables.values import MASK


class VariableCreate(BaseModel):
    kind: VariableKind
    name: str
    value: str | None = None  # omit when pointing at an external secret source (§15)
    sensitive: bool = False
    hcl: bool = False
    # External secret reference (§15): supply a source + locator instead of a value.
    secret_source_id: uuid.UUID | None = None
    secret_ref: str | None = None
    secret_fallback_mode: SecretFallback = SecretFallback.error
    secret_fallback: str | None = None  # static fallback value (write-only)

    @model_validator(mode="after")
    def _one_source(self) -> VariableCreate:
        is_ref = self.secret_source_id is not None
        if is_ref == (self.value is not None):
            raise ValueError("provide exactly one of `value` or `secret_source_id`")
        if is_ref and not self.secret_ref:
            raise ValueError("`secret_ref` is required for a secret reference")
        if (
            is_ref
            and self.secret_fallback_mode == SecretFallback.static
            and not self.secret_fallback
        ):
            raise ValueError("`secret_fallback` is required when secret_fallback_mode is 'static'")
        return self


class VariableUpdate(BaseModel):
    # value omitted → secret/value unchanged (write-only semantics for sensitive vars).
    value: str | None = None
    sensitive: bool | None = None
    hcl: bool | None = None
    # Re-point or re-tune a reference. `secret_fallback` is write-only.
    secret_source_id: uuid.UUID | None = None
    secret_ref: str | None = None
    secret_fallback_mode: SecretFallback | None = None
    secret_fallback: str | None = None


class VariableOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: VariableKind
    name: str
    sensitive: bool
    hcl: bool
    value: str | None  # masked for sensitive (never the ciphertext, never the plaintext)
    secret_source_id: uuid.UUID | None
    secret_ref: str | None
    secret_fallback_mode: SecretFallback | None

    @classmethod
    def of(cls, var: Variable) -> VariableOut:
        is_ref = var.secret_source_id is not None
        return cls(
            id=var.id,
            kind=var.kind,
            name=var.name,
            sensitive=var.sensitive,
            hcl=var.hcl,
            value=MASK if (var.sensitive or is_ref) else var.value,
            secret_source_id=var.secret_source_id,
            secret_ref=var.secret_ref,
            secret_fallback_mode=var.secret_fallback_mode if is_ref else None,
        )


class ResolvedVariableOut(BaseModel):
    name: str
    injected_name: str
    kind: VariableKind
    sensitive: bool
    hcl: bool
    provenance: str
    value: str | None

    @classmethod
    def of(cls, rv: ResolvedVariable) -> ResolvedVariableOut:
        return cls(
            name=rv.name,
            injected_name=rv.injected_name,
            kind=rv.kind,
            sensitive=rv.sensitive,
            hcl=rv.hcl,
            provenance=rv.provenance,
            value=MASK if rv.sensitive else rv.value,
        )
