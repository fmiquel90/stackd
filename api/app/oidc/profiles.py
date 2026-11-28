"""Named AWS profiles of a cloud integration (SPECS §10.6).

Terraform written for humans names its identities (`provider "aws" { profile = "…" }`, a backend
config's `profile`). A profile maps such a name to a plan and an apply role; the worker writes them
into a generated AWS config file, each assumed with the job's one workload token. One model serves
the route and the configuration document (§22): the same rules everywhere.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from app.enums import JobPhase

MAX_PROFILES = 50
# Every value below lands in an INI file the worker generates: no character that could end a line
# or a section header may get through.
_PROFILE_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
_ROLE_ARN = r"^arn:aws[a-z-]*:iam::\d{12}:role/[A-Za-z0-9+=,.@_/-]{1,512}$"
_REGION = r"^[a-z]{2}(-[a-z]+)+-\d+$"
# The generated file's [default] section mirrors the integration's default role.
RESERVED_PROFILE_NAMES = frozenset({"default"})

RoleArn = Annotated[str, StringConstraints(pattern=_ROLE_ARN)]


def _not_reserved(name: str) -> str:
    if name in RESERVED_PROFILE_NAMES:
        raise ValueError(f"`{name}` is reserved: it is the integration's default role")
    return name


ProfileName = Annotated[
    str, StringConstraints(pattern=_PROFILE_NAME), AfterValidator(_not_reserved)
]


class CloudProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_role_arn: RoleArn
    apply_role_arn: RoleArn
    region: Annotated[str, StringConstraints(pattern=_REGION)] | None = None


CloudProfiles = Annotated[dict[ProfileName, CloudProfile], Field(max_length=MAX_PROFILES)]


def stored(profiles: dict[str, CloudProfile]) -> dict[str, dict[str, str | None]]:
    """The JSONB value of `cloud_integrations.profiles` (JSONB keeps no key order: readers sort)."""
    return {name: p.model_dump() for name, p in profiles.items()}


def for_phase(
    profiles: dict[str, dict[str, str | None]], phase: JobPhase
) -> dict[str, dict[str, str | None]]:
    """The claim payload's profiles (§7.2): each with the role of the job's credential phase —
    the same rule as the default role (§10.2)."""
    key = "plan_role_arn" if phase == JobPhase.plan else "apply_role_arn"
    return {name: {"role_arn": p[key], "region": p.get("region")} for name, p in profiles.items()}
