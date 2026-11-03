from __future__ import annotations

import re
import stat
from dataclasses import dataclass
from pathlib import Path

import hcl2

# Collection/structural type constructors → the value must be injected as HCL, not a bare string.
_COMPLEX = ("list", "set", "tuple", "map", "object")


@dataclass
class DiscoveredInput:
    name: str
    type_str: str  # declared type, best-effort (e.g. "string", "list(string)")
    sensitive: bool
    required: bool  # true when the variable has no `default`
    hcl: bool  # complex type → injected as HCL


def _unwrap(raw: object) -> str:
    # python-hcl2 wraps expression attributes as "${...}"; plain identifiers are left as-is.
    s = str(raw or "").strip()
    m = re.fullmatch(r"\$\{(.*)\}", s)
    return (m.group(1) if m else s).strip()


def _truthy(raw: object) -> bool:
    return raw is True or (isinstance(raw, str) and _unwrap(raw).lower() == "true")


def tf_files(root: Path, clone_root: Path) -> list[Path]:
    """The root module's *.tf files discovery may open, as fully resolved paths. A symlink is kept
    only when its final target is a regular file INSIDE the clone (a shared `variables.tf` linked
    into each env dir is common, and tofu reads it) — such a target is already counted by the
    clone size budget. Anything else is skipped: a target outside the clone, a device or FIFO
    (a cloned `main.tf -> /dev/zero` would be read until the API runs out of memory), a
    directory, a dangling or looping link. Non-recursive: no subdirectory is walked into."""
    base = clone_root.resolve()
    out: list[Path] = []
    for p in sorted(root.glob("*.tf")):
        try:
            target = p.resolve(strict=True)  # dangling or looping link → OSError
            # Containment, then a stat of the symlink-free target (metadata only, never a read).
            if target.is_relative_to(base) and stat.S_ISREG(target.stat().st_mode):
                out.append(target)
        except OSError:
            continue
    return out


def parse_inputs(root: Path, clone_root: Path | None = None) -> list[DiscoveredInput]:
    """Parse the `variable` blocks of a root module's *.tf files (no terraform run). Last
    declaration of a name wins; unparseable files are skipped rather than aborting discovery.
    Symlinked files must resolve inside `clone_root` (default: `root` itself, the strictest)."""
    out: dict[str, DiscoveredInput] = {}
    for tf in tf_files(root, clone_root or root):
        try:
            with tf.open() as f:
                data = hcl2.load(f)
        except Exception:
            # One unparseable file shouldn't sink the whole discovery.
            continue
        for block in data.get("variable", []):
            for raw_name, raw in block.items():
                attrs = raw if isinstance(raw, dict) else {}
                name = raw_name.strip('"')  # hcl2 v8 keeps the quotes in the block key
                tstr = _unwrap(attrs.get("type")) or "string"
                out[name] = DiscoveredInput(
                    name=name,
                    type_str=tstr,
                    sensitive=_truthy(attrs.get("sensitive")),
                    required="default" not in attrs,
                    hcl=tstr.split("(", 1)[0] in _COMPLEX,
                )
    return list(out.values())


def placeholder(inp: DiscoveredInput) -> str:
    """An empty, type-valid placeholder value the operator then fills in."""
    head = inp.type_str.split("(", 1)[0]
    if head in ("list", "set", "tuple"):
        return "[]"
    if head in ("map", "object"):
        return "{}"
    return ""
