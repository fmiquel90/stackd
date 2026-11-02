"""OIDC over static cloud credentials (SPECS §10.5).

When an environment has a cloud integration, the workload credentials are the only ones its jobs
get: a static AWS credential resolved for the same environment (a variable set `aws-credentials`,
an env override) is dropped at claim time — otherwise it would stay in tofu's environment, where
the AWS SDK's credential chain picks it before the web identity token. The integration endpoint
reports the shadowed names (never their values) as a configuration warning.
"""

from __future__ import annotations

from app.enums import VariableKind
from app.variables.resolution import ResolvedVariable

# Environment variables through which the AWS SDK's credential chain finds a credential other than
# the web identity token: static keys, a session token, a named profile and the files that hold
# them. `AWS_REGION` and other settings are not credentials and stay.
STATIC_AWS_CREDENTIAL_VARS = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_CONFIG_FILE",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    }
)

SHADOWED_WARNING = "static_aws_credentials_shadowed"


def is_shadowed(rv: ResolvedVariable) -> bool:
    """True for an environment-kind variable the AWS SDK would read as a credential."""
    return rv.kind == VariableKind.environment and rv.name.upper() in STATIC_AWS_CREDENTIAL_VARS


def split_shadowed(
    resolved: list[ResolvedVariable],
) -> tuple[list[ResolvedVariable], list[ResolvedVariable]]:
    """(kept, shadowed): the variables a job with OIDC credentials gets, and the ones it loses."""
    kept = [rv for rv in resolved if not is_shadowed(rv)]
    shadowed = [rv for rv in resolved if is_shadowed(rv)]
    return kept, shadowed
