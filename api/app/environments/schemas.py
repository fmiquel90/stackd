from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from app.environments.var_files import check_var_files
from app.models.environment import Environment


class EnvironmentCreate(BaseModel):
    name: str
    tier: str  # a tiers.name (validated against the catalog at create time)
    branch: str
    autodeploy: bool = False
    protected: bool = False
    require_second_pair_of_eyes: bool = False
    managed_state: bool = True
    allow_mock_apply: bool = False
    allow_fallback_apply: bool = False
    drift_check_enabled: bool = True
    backend_config_file: str | None = None
    backend_config: dict | None = None
    var_files: list[str] = []  # §3.4: repo tfvars files, relative to the project root, in order
    labels: dict | None = None
    position: int = 0

    @field_validator("var_files")
    @classmethod
    def _var_files(cls, v: list[str]) -> list[str]:
        return check_var_files(v)


class EnvironmentUpdate(BaseModel):
    name: str | None = None
    tier: str | None = None
    branch: str | None = None
    autodeploy: bool | None = None
    protected: bool | None = None
    require_second_pair_of_eyes: bool | None = None
    managed_state: bool | None = None
    allow_mock_apply: bool | None = None
    allow_fallback_apply: bool | None = None
    drift_check_enabled: bool | None = None
    backend_config_file: str | None = None
    backend_config: dict | None = None
    var_files: list[str] | None = None  # null clears it, like []
    labels: dict | None = None
    position: int | None = None

    @field_validator("var_files")
    @classmethod
    def _var_files(cls, v: list[str] | None) -> list[str]:
        return check_var_files(v)


class OutOfBandChangeOut(BaseModel):
    """The env's state was written outside Stackd (§19.7): the baseline when it was first seen, and
    the latest differing observation."""

    detected_at: datetime
    run_id: uuid.UUID | None
    expected_serial: int | None
    expected_lineage: str | None
    observed_serial: int | None
    observed_lineage: str | None


class EnvironmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    stack_id: uuid.UUID
    name: str
    tier: str
    branch: str
    autodeploy: bool
    protected: bool
    require_second_pair_of_eyes: bool
    managed_state: bool
    allow_mock_apply: bool
    allow_fallback_apply: bool
    backend_config_file: str | None
    backend_config: dict | None
    var_files: list[str]
    head_sha: str | None
    commits_ahead: int | None
    affects_project_root: bool | None
    stale: bool
    last_applied_sha: str | None = None
    drift_status: str
    last_drift_checked_at: datetime | None
    drift_run_id: uuid.UUID | None
    drift_check_enabled: bool
    state_serial: int | None
    state_lineage: str | None
    state_observed_at: datetime | None
    out_of_band_change: OutOfBandChangeOut | None
    locked: bool
    labels: dict | None
    position: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, e: Environment, *, last_applied_sha: str | None = None) -> EnvironmentOut:
        # stale = head known AND differs from last applied commit (§9.6).
        stale = bool(e.head_sha and last_applied_sha and e.head_sha != last_applied_sha)
        return cls(
            id=e.id,
            stack_id=e.stack_id,
            name=e.name,
            tier=e.tier,
            branch=e.branch,
            autodeploy=e.autodeploy,
            protected=e.protected,
            require_second_pair_of_eyes=e.require_second_pair_of_eyes,
            managed_state=e.managed_state,
            allow_mock_apply=e.allow_mock_apply,
            allow_fallback_apply=e.allow_fallback_apply,
            backend_config_file=e.backend_config_file,
            backend_config=e.backend_config,
            var_files=list(e.var_files or []),
            head_sha=e.head_sha,
            commits_ahead=e.commits_ahead,
            affects_project_root=e.affects_project_root,
            stale=stale,
            last_applied_sha=last_applied_sha,
            drift_status=e.drift_status,
            last_drift_checked_at=e.last_drift_checked_at,
            drift_run_id=e.drift_run_id,
            drift_check_enabled=e.drift_check_enabled,
            state_serial=e.state_serial,
            state_lineage=e.state_lineage,
            state_observed_at=e.state_observed_at,
            out_of_band_change=(
                OutOfBandChangeOut(
                    detected_at=e.out_of_band_detected_at,
                    run_id=e.out_of_band_run_id,
                    expected_serial=e.out_of_band_expected_serial,
                    expected_lineage=e.out_of_band_expected_lineage,
                    observed_serial=e.out_of_band_observed_serial,
                    observed_lineage=e.out_of_band_observed_lineage,
                )
                if e.out_of_band_detected_at is not None
                else None
            ),
            locked=e.locked,
            labels=e.labels,
            position=e.position,
            created_at=e.created_at,
            updated_at=e.updated_at,
        )
