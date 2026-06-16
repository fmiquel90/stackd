from __future__ import annotations

import re
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


def parse_inputs(root: Path) -> list[DiscoveredInput]:
    """Parse the `variable` blocks of a root module's *.tf files (no terraform run). Last
    declaration of a name wins; unparseable files are skipped rather than aborting discovery."""
    out: dict[str, DiscoveredInput] = {}
    for tf in sorted(root.glob("*.tf")):
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
