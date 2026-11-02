from __future__ import annotations

import os
from dataclasses import dataclass, field

from agent.tools import parse_runner_images, probe_local, tools_from_images


RUNNERS = frozenset({"local", "docker"})


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
    # docker runner (§7.4): {"opentofu:1.12.2": "<repo>@sha256:<digest>"}, from STACKD_RUNNER_IMAGES
    runner_images: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # A typo must not fall back to `local` silently: that runner has no isolation (§7.4).
        if self.runner not in RUNNERS:
            raise ValueError(f"STACKD_RUNNER must be one of {sorted(RUNNERS)}, got {self.runner!r}")

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
            runner_images=parse_runner_images(os.environ.get("STACKD_RUNNER_IMAGES", "")),
        )

    def available_tools(self) -> dict[str, list[str]]:
        """The tool versions this worker can run (§7.4): its runner images' on the docker runner,
        the binaries on PATH on the local one."""
        if self.runner == "docker":
            return tools_from_images(self.runner_images)
        return probe_local()
