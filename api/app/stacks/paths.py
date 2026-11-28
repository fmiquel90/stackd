"""Which repository changes concern a stack (SPECS §3.1, §9.6): its `project_root` plus its
`trigger_paths` globs. Every "does this change touch the stack" decision goes through
`touches_stack`, so the push webhook and the staleness level 2 can never disagree.
"""

from __future__ import annotations

import glob
import re
from collections.abc import Iterable
from functools import lru_cache

MAX_TRIGGER_PATHS = 32
MAX_TRIGGER_PATH_LENGTH = 256


def normalize_trigger_paths(patterns: Iterable[str] | None) -> list[str]:
    """Validate and canonicalize a stack's `trigger_paths` (SPECS §3.1): repo-root-relative POSIX
    globs, trailing `/` dropped, duplicates removed (first occurrence kept). Raises `ValueError`."""
    out: list[str] = []
    for raw in patterns or []:
        pattern = raw.strip().rstrip("/")
        if not pattern:
            raise ValueError("a trigger path must not be empty")
        if len(pattern) > MAX_TRIGGER_PATH_LENGTH:
            raise ValueError(f"a trigger path is at most {MAX_TRIGGER_PATH_LENGTH} characters")
        if pattern.startswith("/"):
            raise ValueError(f"'{pattern}': trigger paths are relative to the repository root")
        if "\\" in pattern or any(ord(c) < 32 for c in pattern):
            raise ValueError(f"'{pattern}': use '/' as separator, no control characters")
        if any(seg in ("", ".", "..") for seg in pattern.split("/")):
            raise ValueError(f"'{pattern}': no empty, '.' or '..' path segment")
        if pattern not in out:
            out.append(pattern)
    if len(out) > MAX_TRIGGER_PATHS:
        raise ValueError(f"at most {MAX_TRIGGER_PATHS} trigger paths")
    return out


@lru_cache(maxsize=1024)
def _compile(pattern: str) -> re.Pattern[str]:
    # `*` / `?` / `[...]` stay within one segment, `**` as a whole segment spans any number of
    # them, and dotfiles are ordinary names (a `.terraform.lock.hcl` change matters).
    return re.compile(glob.translate(pattern, recursive=True, include_hidden=True, seps="/"))


def _ancestors_and_self(path: str) -> Iterable[str]:
    parts = path.split("/")
    for i in range(1, len(parts) + 1):
        yield "/".join(parts[:i])


def matches_trigger_path(pattern: str, path: str) -> bool:
    """A pattern concerns a file when it matches the file's path or one of its parent directories:
    `modules/vpc` covers everything under that folder, like `modules/vpc/**`."""
    rx = _compile(pattern)
    return any(rx.match(p) for p in _ancestors_and_self(path))


def under_project_root(project_root: str, path: str) -> bool:
    root = project_root.strip().removeprefix("./").strip("/")
    if root in ("", "."):
        return True
    return path == root or path.startswith(root + "/")


def touches_stack(paths: Iterable[str], project_root: str, trigger_paths: Iterable[str]) -> bool:
    """Whether at least one changed path (repo-root-relative POSIX; for a rename, give both the old
    and the new path) is under `project_root` or matches one of `trigger_paths`."""
    patterns = list(trigger_paths)
    return any(
        under_project_root(project_root, p) or any(matches_trigger_path(t, p) for t in patterns)
        for p in paths
    )
