"""The IaC tool versions this worker can run (SPECS §7.4).

The agent declares them at registration — the API then hands it only runs whose stack's
`tool_version` it has — and re-checks before every job, so a run never executes on a binary other
than the one its stack names. A `tool_version` is an exact version (`1.12.0`) or a minor series
(`1.12`: any patch of it, the highest available wins).

- `local` runner: the versions of the `tofu` / `terraform` binaries on the agent's PATH.
- `docker` runner: the keys of `STACKD_RUNNER_IMAGES`, a JSON map `{"opentofu:1.12.2":
  "<repo>@sha256:<digest>"}` — every image pinned by digest, never a mutable tag.
"""

from __future__ import annotations

import json
import re
import subprocess

BINARIES = {"opentofu": "tofu", "terraform": "terraform"}
_SERIES = re.compile(r"^\d+\.\d+$")
_DIGEST_REF = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")


class ToolUnavailable(RuntimeError):
    """The job's stack names a tool version this worker cannot run: the job fails, nothing runs."""


def version_matches(available: str, wanted: str) -> bool:
    if available == wanted:
        return True
    return (
        _SERIES.match(wanted) is not None
        and available.startswith(wanted + ".")
        and "-" not in available  # a series means its releases, never a pre-release
    )


def _key(version: str) -> tuple:
    core, _, pre = version.partition("-")
    nums = tuple(int(p) for p in core.split(".") if p.isdigit())
    return (nums, pre == "", pre)  # a release sorts above its pre-releases


def pick(tools: dict[str, list[str]], tool: str, wanted: str) -> str:
    """The version to run for `tool`/`wanted`, or ToolUnavailable naming what the worker has."""
    matching = [v for v in tools.get(tool, []) if version_matches(v, wanted)]
    if not matching:
        have = ", ".join(sorted(tools.get(tool, []))) or "none"
        raise ToolUnavailable(
            f"tool_version_unavailable: {tool} {wanted} is not on this worker (it has: {have})"
        )
    return max(matching, key=_key)


def probe_local() -> dict[str, list[str]]:
    """Versions of the binaries on PATH (`<bin> version -json`); a missing binary is skipped."""
    found: dict[str, list[str]] = {}
    for tool, binary in BINARIES.items():
        try:
            out = subprocess.run(
                [binary, "version", "-json"], capture_output=True, text=True, timeout=15
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if out.returncode != 0:
            continue
        try:
            version = json.loads(out.stdout).get("terraform_version")
        except ValueError:
            continue
        if isinstance(version, str) and version:
            found[tool] = [version]
    return found


def parse_runner_images(raw: str) -> dict[str, str]:
    """`STACKD_RUNNER_IMAGES` → {"opentofu:1.12.2": "<repo>@sha256:…"}. Refuses (at startup) a
    malformed key, an unknown tool, or an image not pinned by digest: a tag can be re-pointed."""
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"STACKD_RUNNER_IMAGES is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("STACKD_RUNNER_IMAGES must be a JSON object")
    images: dict[str, str] = {}
    for key, ref in data.items():
        tool, sep, version = str(key).partition(":")
        if not sep or tool not in BINARIES or not version:
            raise ValueError(f"STACKD_RUNNER_IMAGES key {key!r} must be <opentofu|terraform>:<ver>")
        if not isinstance(ref, str) or not _DIGEST_REF.match(ref):
            raise ValueError(
                f"STACKD_RUNNER_IMAGES[{key!r}] must be pinned: <repo>@sha256:<digest>"
            )
        images[f"{tool}:{version}"] = ref
    return images


def tools_from_images(images: dict[str, str]) -> dict[str, list[str]]:
    tools: dict[str, list[str]] = {}
    for key in images:
        tool, _, version = key.partition(":")
        tools.setdefault(tool, []).append(version)
    return tools
