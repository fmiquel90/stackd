from __future__ import annotations

import re

# Allowlisted tofu/terraform subcommands that can run as a `command` run (RunType.command, §4.3).
# This is NOT arbitrary shell: the worker runs `<tool> <command> <args...>` with the command taken
# verbatim from this set. Read-only commands need only `writer`; mutating ones are gated by
# `can_apply` (same as an apply) because they change real state.

READ_ONLY_COMMANDS: frozenset[str] = frozenset(
    {"output", "show", "state list", "state show", "validate", "providers"}
)
MUTATING_COMMANDS: frozenset[str] = frozenset(
    {"import", "state rm", "state mv", "taint", "untaint", "refresh", "force-unlock"}
)
ALLOWED_COMMANDS: frozenset[str] = READ_ONLY_COMMANDS | MUTATING_COMMANDS

# Instance-admin only, on top of `can_apply` — parity with the managed lock's own endpoint
# (DELETE /api/v1/environments/{id}/state/lock, §11.2). `force-unlock` only ever targets a backend
# Stackd does not manage (`managed_state=false`); on a managed env the platform owns the lock.
ADMIN_ONLY_COMMANDS: frozenset[str] = frozenset({"force-unlock"})

# Per-command option allowlist (§4.3), closed by default: an arg starting with `-` is refused
# unless listed here for that command. `_VALUED` options take `-opt=<resource address>`
# (repeatable), `_FLAGS` are bare switches. Never anything that prints values (`-json`, `-raw`),
# points the tool elsewhere (`-state`, `-chdir`, `-backend*`), skips the lock (`-lock=false`) or
# injects variables (`-var*`).
_VALUED: dict[str, frozenset[str]] = {"refresh": frozenset({"-target"})}
_FLAGS: dict[str, frozenset[str]] = {
    "state rm": frozenset({"-dry-run"}),
    "state mv": frozenset({"-dry-run"}),
}

MAX_ADDRESSES = 50
MAX_ADDRESS_LEN = 512
# A resource address (`aws_s3_bucket.logs`, `module.net["eu"].aws_subnet.a[0]`, `data.x.y`): starts
# with a letter or `_`, then a closed charset — no whitespace, control characters, `=` or shell
# metacharacters, and never a leading `-` (it can't be parsed as an option).
_ADDRESS = re.compile(r'^[A-Za-z_][A-Za-z0-9_.\-\[\]"/:@]*$')
# A backend lock ID (a UUID for S3/DynamoDB/azurerm, a number for GCS, …).
_LOCK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class CommandArgsError(ValueError):
    """A command's arguments are refused (→ 400 problem+json)."""


def is_mutating(command: str) -> bool:
    return command in MUTATING_COMMANDS


def check_address(addr: object, what: str = "address") -> str:
    """Validate one resource address (used by `-target`/`-replace` plan options and `refresh
    -target`). Raises ValueError with a message that never needs escaping."""
    if not isinstance(addr, str) or not addr:
        raise ValueError(f"{what} must be a non-empty string")
    if len(addr) > MAX_ADDRESS_LEN:
        raise ValueError(f"{what} is longer than {MAX_ADDRESS_LEN} characters")
    if not _ADDRESS.match(addr):
        raise ValueError(
            f'{what} {addr[:80]!r} is not a resource address (letters, digits, _ . - [ ] " / : @; '
            "no whitespace, '=' or leading '-')"
        )
    return addr


def check_addresses(addrs: list[str], what: str) -> list[str]:
    """Validate and de-duplicate (first occurrence kept) a list of addresses; at most
    MAX_ADDRESSES."""
    out = list(dict.fromkeys(check_address(a, what) for a in addrs))
    if len(out) > MAX_ADDRESSES:
        raise ValueError(f"at most {MAX_ADDRESSES} entries in {what}")
    return out


def normalize_args(command: str, args: list[str]) -> list[str]:
    """Validate a command's args against its option allowlist and return them in the order the tool
    parses them: options first (Go's flag parsing stops at the first positional), then positionals.
    Raises CommandArgsError."""
    if command == "force-unlock":
        # Exactly the lock ID: the agent adds `-force` itself (non-interactive) — the user never
        # passes a flag.
        if len(args) != 1 or not _LOCK_ID.match(args[0]):
            raise CommandArgsError(
                "force-unlock takes exactly one argument: the lock ID (letters, digits, . _ : -)."
            )
        return list(args)

    valued = _VALUED.get(command, frozenset())
    flags = _FLAGS.get(command, frozenset())
    options: list[str] = []
    positionals: list[str] = []
    targets: list[str] = []
    for arg in args:
        if not arg.startswith("-"):
            positionals.append(arg)
            continue
        name, sep, value = arg.partition("=")
        if name in flags and not sep:
            if arg not in options:
                options.append(arg)
        elif name in valued and sep:
            try:
                check_address(value, f"{name} address")
            except ValueError as exc:
                raise CommandArgsError(str(exc)) from exc
            if value not in targets:
                targets.append(value)
                options.append(f"{name}={value}")
        else:
            allowed = sorted({*(f"{v}=<address>" for v in valued), *flags})
            hint = f" Allowed for '{command}': {', '.join(allowed)}." if allowed else ""
            raise CommandArgsError(f"Option not allowed: {arg[:80]!r}.{hint}")
    if len(targets) > MAX_ADDRESSES:
        raise CommandArgsError(f"At most {MAX_ADDRESSES} -target addresses.")
    return options + positionals
