from __future__ import annotations

import os
from dataclasses import dataclass, field

from agent.docker_runner import DockerLimits
from agent.drain import DEFAULT_DRAIN_TIMEOUT_SECONDS
from agent.registry_auth import load_auths
from agent.tools import parse_runner_images, probe_local, tools_from_images


RUNNERS = frozenset({"local", "docker"})
_SIZE_UNITS = {"k": 1024, "m": 1024**2, "g": 1024**3}


def _bytes(value: str) -> int:
    """`512m`, `2g`, `1073741824` → bytes."""
    v = value.strip().lower()
    if v and v[-1] in _SIZE_UNITS:
        return int(float(v[:-1]) * _SIZE_UNITS[v[-1]])
    return int(v)


def parse_labels(value: str) -> dict[str, str]:
    """`STACKD_WORKER_LABELS`: comma-separated `key=value` pairs (`tier=prod,region=eu-west-3`),
    sent at registration (§7.1) and matched against environments' labels by the claim (§7.2).
    Blanks around keys and values are trimmed; empty → no labels. A malformed entry (no `=`, an
    empty key) or a repeated key refuses to start: a typo must not silently route nothing."""
    labels: dict[str, str] = {}
    for entry in value.split(","):
        if not entry.strip():
            continue
        key, sep, val = entry.partition("=")
        key, val = key.strip(), val.strip()
        if not sep or not key:
            raise ValueError(f"STACKD_WORKER_LABELS: expected key=value, got {entry.strip()!r}")
        if key in labels:
            raise ValueError(f"STACKD_WORKER_LABELS: label {key!r} is set twice")
        labels[key] = val
    return labels


def _docker_limits() -> DockerLimits:
    env = os.environ.get
    cpus = env("STACKD_DOCKER_CPUS")
    memory = env("STACKD_DOCKER_MEMORY")
    return DockerLimits(
        network=env("STACKD_DOCKER_NETWORK") or "bridge",
        memory_bytes=_bytes(memory) if memory else None,
        nano_cpus=int(float(cpus) * 1e9) if cpus else None,
        pids_limit=int(env("STACKD_DOCKER_PIDS_LIMIT") or 1024),
        runtime=env("STACKD_DOCKER_RUNTIME") or None,
        tmp_size=env("STACKD_DOCKER_TMP_SIZE") or "1g",
    )


@dataclass(frozen=True)
class Settings:
    api_url: str
    pool_token: str
    worker_name: str
    runner: str  # "local" (dev) | "docker" (prod)
    heartbeat_interval: int
    poll_wait: int
    workspace_root: str
    leak_action: str  # "warn" (default) | "fail" — cleartext-tripwire policy (§5.1, Phase C)
    max_concurrent_jobs: int  # in-flight jobs across environments (§7, Phase E; default 1)
    # After a stop request's SIGINT, how long the tool gets to stop before SIGKILL (§4.1)
    cancel_grace_seconds: int = 120
    # On SIGTERM/SIGINT, how long running jobs get to finish before they are stopped (§7.1 "Drain")
    drain_timeout_seconds: int = DEFAULT_DRAIN_TIMEOUT_SECONDS
    # STACKD_WORKER_LABELS (§7.1): this worker's own labels; the API adds its pool's under them
    labels: dict[str, str] = field(default_factory=dict)
    # docker runner (§7.4): {"opentofu:1.12.2": "<repo>@sha256:<digest>"}, from STACKD_RUNNER_IMAGES
    runner_images: dict[str, str] = field(default_factory=dict)
    docker_host: str | None = None  # STACKD_DOCKER_HOST: unix socket of the Docker Engine
    docker_limits: DockerLimits = field(default_factory=DockerLimits)
    # STACKD_REGISTRY_AUTH_FILE (§7.4): a Docker config.json whose `auths` authenticate the pull of
    # a runner image from a private registry. Re-read at each pull (rotated credentials apply).
    registry_auth_file: str | None = None

    def __post_init__(self) -> None:
        # A typo must not fall back to `local` silently: that runner has no isolation (§7.4).
        if self.runner not in RUNNERS:
            raise ValueError(f"STACKD_RUNNER must be one of {sorted(RUNNERS)}, got {self.runner!r}")
        if self.runner == "docker" and not self.runner_images:
            # Without images the worker could run nothing: refuse to start rather than register.
            raise ValueError("STACKD_RUNNER=docker needs STACKD_RUNNER_IMAGES (pinned by digest)")
        if self.registry_auth_file is not None:
            # A wrong path or a broken file refuses to start, rather than surfacing as a failed
            # pull on the first run that needs a missing image.
            load_auths(self.registry_auth_file)

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.environ.get("STACKD_POOL_TOKEN", "")
        token_file = os.environ.get("STACKD_POOL_TOKEN_FILE")
        if not token and token_file and os.path.exists(token_file):
            token = open(token_file).read().strip()
        return cls(
            api_url=os.environ.get("STACKD_API_URL", "http://api:8000").rstrip("/"),
            pool_token=token,
            worker_name=os.environ.get("STACKD_WORKER_NAME", os.uname().nodename),
            runner=os.environ.get("STACKD_RUNNER", "local"),
            heartbeat_interval=int(os.environ.get("STACKD_HEARTBEAT_INTERVAL", "20")),
            poll_wait=int(os.environ.get("STACKD_POLL_WAIT", "25")),
            workspace_root=os.environ.get("STACKD_WORKSPACE_ROOT", "/tmp/stackd-ws"),
            leak_action="fail"
            if os.environ.get("STACKD_LEAK_TRIPWIRE", "warn") == "fail"
            else "warn",
            max_concurrent_jobs=max(1, int(os.environ.get("STACKD_MAX_CONCURRENT_JOBS", "1"))),
            cancel_grace_seconds=max(1, int(os.environ.get("STACKD_CANCEL_GRACE_SECONDS", "120"))),
            drain_timeout_seconds=max(
                0,
                int(
                    os.environ.get("STACKD_DRAIN_TIMEOUT_SECONDS") or DEFAULT_DRAIN_TIMEOUT_SECONDS
                ),
            ),
            labels=parse_labels(os.environ.get("STACKD_WORKER_LABELS", "")),
            runner_images=parse_runner_images(os.environ.get("STACKD_RUNNER_IMAGES", "")),
            docker_host=os.environ.get("STACKD_DOCKER_HOST") or None,
            docker_limits=_docker_limits(),
            registry_auth_file=os.environ.get("STACKD_REGISTRY_AUTH_FILE") or None,
        )

    def available_tools(self) -> dict[str, list[str]]:
        """The tool versions this worker can run (§7.4): its runner images' on the docker runner,
        the binaries on PATH on the local one."""
        if self.runner == "docker":
            return tools_from_images(self.runner_images)
        return probe_local()
