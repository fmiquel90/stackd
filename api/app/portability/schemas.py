"""The configuration document (SPECS §22): a space's configuration, by names, never its secrets.

Every object is identified by its natural key (a stack by name, an environment by stack + name, a
variable by kind + name…), never by a database id, so a document moves between instances and reads
well in a Git diff. `extra="forbid"` everywhere: a document is often edited by hand, and a typo'd
field must fail the import, not be silently dropped.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.enums import (
    CloudProvider,
    HookOnFailure,
    HookStage,
    NotificationKind,
    RepoAuthKind,
    SecretFallback,
    SecretProvider,
    Tool,
    TriggerPolicy,
    VariableKind,
)
from app.notifications.dispatcher import check_url_syntax
from app.notifications.schemas import ALLOWED_STATES
from app.stacks.schemas import _check_tool_version

FORMAT: Literal["stackd.config"] = "stackd.config"
FORMAT_VERSION: Literal[1] = 1

_TIER_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")  # same rule as the tier catalog (§2.4)


class _Doc(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SecretRefDoc(_Doc):
    """An external secret reference (§15): where the value lives, never the value."""

    source: str  # a secret source of the same space, by name
    ref: str = Field(min_length=1)
    fallback_mode: SecretFallback = SecretFallback.error


class VariableDoc(_Doc):
    kind: VariableKind
    name: str = Field(min_length=1)
    hcl: bool = False
    sensitive: bool = False
    # A plain variable's value. An export never writes a sensitive value (CLAUDE.md invariant 3):
    # a sensitive variable is exported with `value: null`. An operator may fill one in by hand
    # before importing — it is then stored encrypted, like a write through the API.
    value: str | None = None
    secret: SecretRefDoc | None = None

    @model_validator(mode="after")
    def _one_source(self) -> VariableDoc:
        if self.secret is not None:
            if self.value is not None:
                raise ValueError("a secret reference carries no `value`")
            self.sensitive = True  # a reference is always sensitive (§15.1)
        elif self.value is None and not self.sensitive:
            raise ValueError("a non-sensitive variable needs a `value`")
        return self


class HookDoc(_Doc):
    stage: HookStage
    name: str = Field(min_length=1)
    command: str = Field(min_length=1)
    on_failure: HookOnFailure = HookOnFailure.fail
    position: int = 0


class NotificationTargetDoc(_Doc):
    name: str = Field(min_length=1)
    kind: NotificationKind
    url: str
    on_states: list[str]
    enabled: bool = True

    @field_validator("on_states")
    @classmethod
    def _states(cls, v: list[str]) -> list[str]:
        bad = set(v) - ALLOWED_STATES
        if bad or not v:
            raise ValueError(f"on_states must be a non-empty subset of {sorted(ALLOWED_STATES)}")
        return v

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        check_url_syntax(v)  # the DNS half (`check_url_safe`) is awaited by the importer
        return v


class CloudIntegrationDoc(_Doc):
    provider: CloudProvider = CloudProvider.aws
    plan_role_arn: str = Field(min_length=1)
    apply_role_arn: str = Field(min_length=1)
    region: str | None = None
    session_duration: int = 3600


class EnvironmentDoc(_Doc):
    id: uuid.UUID | None = None  # the source instance's id: informational, feeds `id_map`
    name: str = Field(min_length=1)
    tier: str
    branch: str = Field(min_length=1)
    autodeploy: bool = False
    protected: bool = False
    require_second_pair_of_eyes: bool = False
    managed_state: bool = True
    allow_mock_apply: bool = False
    allow_fallback_apply: bool = False
    drift_check_enabled: bool = True
    backend_config_file: str | None = None
    backend_config: dict[str, Any] | None = None
    labels: dict[str, Any] | None = None
    position: int = 0
    variables: list[VariableDoc] = []
    hooks: list[HookDoc] = []
    notification_targets: list[NotificationTargetDoc] = []
    cloud_integration: CloudIntegrationDoc | None = None


class StackDoc(_Doc):
    id: uuid.UUID | None = None  # the source instance's id: informational, feeds `id_map`
    name: str = Field(min_length=1)
    description: str | None = None
    repo_url: str = Field(min_length=1)
    repo_auth_kind: RepoAuthKind = RepoAuthKind.none
    # Whether the source had these write-only secrets (never their values): an import reports
    # them as missing when the target has none.
    has_repo_secret: bool = False
    has_webhook_secret: bool = False
    project_root: str = "."
    tool: Tool = Tool.opentofu
    tool_version: str
    labels: dict[str, Any] | None = None
    variables: list[VariableDoc] = []
    hooks: list[HookDoc] = []
    notification_targets: list[NotificationTargetDoc] = []
    environments: list[EnvironmentDoc] = []

    @field_validator("tool_version")
    @classmethod
    def _tool_version(cls, v: str) -> str:
        _check_tool_version(v)
        return v


class AttachmentDoc(_Doc):
    """A variable-set attachment target, by name: a stack, an environment, or a tier."""

    target: Literal["stack", "environment", "tier"]
    stack: str | None = None
    environment: str | None = None
    tier: str | None = None
    priority: int = 0

    @model_validator(mode="after")
    def _target_fields(self) -> AttachmentDoc:
        wanted = {
            "stack": {"stack"},
            "environment": {"stack", "environment"},
            "tier": {"tier"},
        }[self.target]
        given = {f for f in ("stack", "environment", "tier") if getattr(self, f) is not None}
        if given != wanted:
            raise ValueError(f"a '{self.target}' attachment names exactly {sorted(wanted)}")
        return self


class VariableSetDoc(_Doc):
    name: str = Field(min_length=1)
    description: str | None = None
    auto_attach: bool = False
    selector: dict[str, Any] | None = None
    variables: list[VariableDoc] = []
    attachments: list[AttachmentDoc] = []


class EnvRefDoc(_Doc):
    """An environment by name. `space` is set only for an environment of another space (a
    cross-space upstream, §3.8); omitted, it is the space the document is imported into."""

    space: str | None = None
    stack: str
    environment: str


class OutputReferenceDoc(_Doc):
    output_name: str = Field(min_length=1)
    input_name: str = Field(min_length=1)
    mock_value: Any = None


class DependencyDoc(_Doc):
    upstream: EnvRefDoc
    downstream: EnvRefDoc
    trigger_policy: TriggerPolicy = TriggerPolicy.on_output_change
    references: list[OutputReferenceDoc] = []

    @field_validator("downstream")
    @classmethod
    def _downstream_here(cls, v: EnvRefDoc) -> EnvRefDoc:
        # An edge belongs to its downstream's space: the document of that space carries it.
        if v.space is not None:
            raise ValueError("the downstream environment is always in the imported space")
        return v


class TierDoc(_Doc):
    name: str
    requires_four_eyes: bool = False
    repo_hooks_at_apply: bool = False
    position: int = 0

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _TIER_RE.match(v):
            raise ValueError("tier name must match ^[a-z0-9][a-z0-9-]*$")
        return v


class SecretSourceDoc(_Doc):
    name: str = Field(min_length=1)
    provider: SecretProvider
    config: dict[str, Any] = {}  # non-sensitive provider settings; the credential is never in it


class WorkerPoolDoc(_Doc):
    name: str = Field(min_length=1)
    labels: dict[str, Any] | None = None


class SourceDoc(_Doc):
    space: str
    description: str | None = None
    url: str | None = None  # the exporting instance's public URL


class ConfigDocument(_Doc):
    format: Literal["stackd.config"] = FORMAT
    format_version: Literal[1] = FORMAT_VERSION
    exported_at: datetime | None = None
    source: SourceDoc | None = None
    tiers: list[TierDoc] = []
    secret_sources: list[SecretSourceDoc] = []
    worker_pools: list[WorkerPoolDoc] = []
    variable_sets: list[VariableSetDoc] = []
    stacks: list[StackDoc] = []
    dependencies: list[DependencyDoc] = []
    # Written by the export for the reader (what it left out); ignored by the import, which
    # computes its own list against the target.
    missing_secrets: list[dict[str, Any]] = []

    @model_validator(mode="after")
    def _unique_keys(self) -> ConfigDocument:
        """Natural keys must be unique: two entries for one object would make the import's
        outcome depend on their order."""
        problems: list[str] = []

        def dup(what: str, keys: list[Any]) -> None:
            seen: set[Any] = set()
            for k in keys:
                if k in seen:
                    problems.append(f"duplicate {what} {k!r}")
                seen.add(k)

        dup("tier", [t.name for t in self.tiers])
        dup("secret source", [s.name for s in self.secret_sources])
        dup("worker pool", [p.name for p in self.worker_pools])
        dup("variable set", [s.name for s in self.variable_sets])
        dup("stack", [s.name for s in self.stacks])
        for vs in self.variable_sets:
            dup(f"variable in set {vs.name}", [(v.kind.value, v.name) for v in vs.variables])
        for st in self.stacks:
            dup(f"environment in stack {st.name}", [e.name for e in st.environments])
            dup(f"variable in stack {st.name}", [(v.kind.value, v.name) for v in st.variables])
            dup(f"hook in stack {st.name}", [(h.stage.value, h.name) for h in st.hooks])
            dup(
                f"notification target in stack {st.name}", [n.name for n in st.notification_targets]
            )
            for env in st.environments:
                where = f"{st.name}/{env.name}"
                dup(f"variable in {where}", [(v.kind.value, v.name) for v in env.variables])
                dup(f"hook in {where}", [(h.stage.value, h.name) for h in env.hooks])
                dup(f"notification target in {where}", [n.name for n in env.notification_targets])
        dup(
            "dependency",
            [
                (
                    d.upstream.space,
                    d.upstream.stack,
                    d.upstream.environment,
                    d.downstream.stack,
                    d.downstream.environment,
                )
                for d in self.dependencies
            ],
        )
        if problems:
            raise ValueError("; ".join(problems))
        return self
