from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict

from app.enums import VariableKind
from app.models.variable import Variable
from app.variables.resolution import ResolvedVariable
from app.variables.values import MASK


class VariableCreate(BaseModel):
    kind: VariableKind
    name: str
    value: str
    sensitive: bool = False
    hcl: bool = False


class VariableUpdate(BaseModel):
    # value omitted → secret/value unchanged (write-only semantics for sensitive vars).
    value: str | None = None
    sensitive: bool | None = None
    hcl: bool | None = None


class VariableOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: VariableKind
    name: str
    sensitive: bool
    hcl: bool
    value: str | None  # masked for sensitive (never the ciphertext, never the plaintext)

    @classmethod
    def of(cls, var: Variable) -> VariableOut:
        return cls(
            id=var.id,
            kind=var.kind,
            name=var.name,
            sensitive=var.sensitive,
            hcl=var.hcl,
            value=MASK if var.sensitive else var.value,
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
