from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    api_url: str
    pool_token: str
    worker_name: str
    runner: str  # "local" (dev) | "docker" (prod)
    heartbeat_interval: int
    poll_wait: int
    workspace_root: str

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
        )
