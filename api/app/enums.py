from __future__ import annotations

import enum


class Role(enum.StrEnum):
    reader = "reader"
    writer = "writer"
    approver = "approver"
    admin = "admin"


class Tier(enum.StrEnum):
    dev = "dev"
    staging = "staging"
    prod = "prod"

    @property
    def rank(self) -> int:
        return {"dev": 0, "staging": 1, "prod": 2}[self.value]


class AuditActorKind(enum.StrEnum):
    user = "user"
    worker = "worker"
    system = "system"
    webhook = "webhook"


class RepoAuthKind(enum.StrEnum):
    none = "none"
    token = "token"
    deploy_key = "deploy_key"


class Tool(enum.StrEnum):
    opentofu = "opentofu"
    terraform = "terraform"


class VariableKind(enum.StrEnum):
    terraform = "terraform"  # → TF_VAR_/tfvars
    environment = "environment"  # → process env var


class AttachmentTarget(enum.StrEnum):
    stack = "stack"
    environment = "environment"


class RunType(enum.StrEnum):
    tracked = "tracked"
    proposed = "proposed"
    destroy = "destroy"
    command = "command"  # a single allowlisted tofu/terraform subcommand (import, state rm, …)


class RunState(enum.StrEnum):
    queued = "queued"
    preparing = "preparing"
    planning = "planning"
    checking = "checking"
    unconfirmed = "unconfirmed"
    confirmed = "confirmed"
    applying = "applying"
    running = "running"  # executing a `command` run (no plan/apply phases)
    finished = "finished"
    failed = "failed"
    discarded = "discarded"
    canceled = "canceled"


# States that count as "active" — the one_active_run_per_env partial unique index (§3.5).
ACTIVE_STATES: frozenset[RunState] = frozenset(
    {
        RunState.preparing,
        RunState.planning,
        RunState.checking,
        RunState.unconfirmed,
        RunState.confirmed,
        RunState.applying,
        RunState.running,
    }
)
TERMINAL_STATES: frozenset[RunState] = frozenset(
    {RunState.finished, RunState.failed, RunState.discarded, RunState.canceled}
)


class TriggeredBy(enum.StrEnum):
    manual = "manual"
    webhook = "webhook"
    dependency = "dependency"
    api = "api"


class RunEventActor(enum.StrEnum):
    system = "system"
    user = "user"
    worker = "worker"


class WorkerStatus(enum.StrEnum):
    idle = "idle"
    busy = "busy"
    offline = "offline"


class HookStage(enum.StrEnum):
    before_init = "before_init"
    after_init = "after_init"
    before_plan = "before_plan"
    after_plan = "after_plan"
    before_apply = "before_apply"
    after_apply = "after_apply"


class HookOnFailure(enum.StrEnum):
    fail = "fail"
    warn = "warn"


# Job phase in the claim payload (§7.2) — the EXECUTION type, distinct from the fine run/log phases.
class JobPhase(enum.StrEnum):
    plan = "plan"
    apply = "apply"
    command = "command"  # one-off allowlisted subcommand


class TriggerPolicy(enum.StrEnum):
    on_output_change = "on_output_change"
    always = "always"
    never = "never"


class OidcKeyStatus(enum.StrEnum):
    active = "active"  # signs; exactly one at a time
    retiring = "retiring"  # still in JWKS, no longer signs
    retired = "retired"  # out of JWKS


class CloudProvider(enum.StrEnum):
    aws = "aws"  # gcp/azure in Phase 7


class NotificationKind(enum.StrEnum):
    slack = "slack"  # POST {"text": ...} — Slack/Mattermost incoming webhook
    webhook = "webhook"  # POST a structured JSON envelope
