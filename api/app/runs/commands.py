from __future__ import annotations

# Allowlisted tofu/terraform subcommands that can run as a `command` run (RunType.command).
# This is NOT arbitrary shell: the worker runs `<tool> <command> <args...>` with the command taken
# verbatim from this set. Read-only commands need only `writer`; mutating ones are gated by
# `can_apply` (same as an apply) because they change real state.
#
# force-unlock is intentionally absent: it has its own dedicated endpoint
# (DELETE /api/v1/environments/{id}/state/lock).

READ_ONLY_COMMANDS: frozenset[str] = frozenset(
    {"output", "show", "state list", "state show", "validate", "providers"}
)
MUTATING_COMMANDS: frozenset[str] = frozenset(
    {"import", "state rm", "state mv", "taint", "untaint", "refresh"}
)
ALLOWED_COMMANDS: frozenset[str] = READ_ONLY_COMMANDS | MUTATING_COMMANDS


def is_mutating(command: str) -> bool:
    return command in MUTATING_COMMANDS
