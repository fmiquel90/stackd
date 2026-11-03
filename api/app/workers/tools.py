"""Which tool versions a worker can run (SPECS §7.4).

A stack's `tool_version` is an exact version (`1.12.0`) or a minor series (`1.12`, any patch). The
agent declares the versions it can run at registration; the claim hands a worker only the runs
whose stack it can run, and the agent re-checks the binary before running anything.
"""

from __future__ import annotations

import re

# `MAJOR.MINOR` (a series) or `MAJOR.MINOR.PATCH` with an optional pre-release (`1.12.0-rc1`).
TOOL_VERSION_RE = re.compile(r"^\d+\.\d+(?:\.\d+(?:-[0-9A-Za-z.]+)?)?$")
_SERIES_RE = re.compile(r"^\d+\.\d+$")

# SQL twin of `version_matches` over a worker's `tools` and the candidate's stack `s` (claim).
# A worker with no declared tools (an agent predating it) is not filtered here: the agent's own
# check still refuses a version it does not have, and a production API refuses such an agent.
SQL_WORKER_CAN_RUN = """
      AND (
        CAST(:tools AS jsonb) IS NULL
        OR EXISTS (
          SELECT 1
          FROM jsonb_array_elements_text(CAST(:tools AS jsonb) -> s.tool::text) AS v(version)
          WHERE v.version = s.tool_version
             OR (s.tool_version ~ '^[0-9]+\\.[0-9]+$'
                 AND v.version LIKE s.tool_version || '.%'
                 AND v.version NOT LIKE '%-%')
        )
      )
"""


def valid_tool_version(value: str) -> bool:
    return TOOL_VERSION_RE.match(value) is not None


def version_matches(available: str, wanted: str) -> bool:
    """True when the `available` binary satisfies the stack's `wanted` version."""
    if available == wanted:
        return True
    # A series means its releases, never a pre-release.
    return (
        _SERIES_RE.match(wanted) is not None
        and available.startswith(wanted + ".")
        and "-" not in available
    )


def can_run(tools: dict[str, list[str]] | None, tool: str, wanted: str) -> bool:
    if tools is None:
        return True
    return any(version_matches(v, wanted) for v in tools.get(tool, []))
