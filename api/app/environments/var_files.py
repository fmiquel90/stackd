"""An environment's repo variable files (SPECS §3.4): the repo's own tfvars files, relative to the
stack's project_root, passed in order as `-var-file=` to the tofu commands that read variables."""

from __future__ import annotations

import posixpath

from app.errors import ProblemException

MAX_VAR_FILES = 16
MAX_VAR_FILE_LENGTH = 256


def check_var_files(value: list[str] | None) -> list[str]:
    """The list's shape, whatever the stack: bounded, each path relative, printable, listed once.
    Raises ValueError (a 422 through Pydantic). `None` (an explicit PATCH null) clears the list."""
    files = value or []
    if len(files) > MAX_VAR_FILES:
        raise ValueError(f"at most {MAX_VAR_FILES} var files")
    seen: set[str] = set()
    for path in files:
        if not path or len(path) > MAX_VAR_FILE_LENGTH:
            raise ValueError(f"a var file path is 1 to {MAX_VAR_FILE_LENGTH} characters")
        if any(c < " " or c == "\x7f" for c in path):
            raise ValueError(f"var file {path!r} contains a control character")
        if path.startswith("/"):
            raise ValueError(
                f"var file {path!r} is absolute: give it relative to the stack's project root"
            )
        if path in seen:
            raise ValueError(f"var file {path!r} is listed twice")
        seen.add(path)
    return files


def require_inside_checkout(project_root: str, files: list[str]) -> None:
    """Each path, joined to the project root and normalized, must stay inside the repository:
    `../shared/prod.tfvars` is fine from `stacks/app`, not from `.`. Lexical only — the agent
    re-checks on the checkout, symlinks resolved, before using them."""
    for path in files:
        joined = posixpath.normpath(posixpath.join(project_root or ".", path))
        if joined == ".." or joined.startswith(("../", "/")):
            raise ProblemException(
                422,
                "Invalid var file",
                f"'{path}' resolves outside the repository from the project root '{project_root}'.",
            )
