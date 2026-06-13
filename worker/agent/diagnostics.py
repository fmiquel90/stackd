from __future__ import annotations

import os
import platform
import shutil
import subprocess

from agent.logging import recent_logs

# A strictly read-only debug bundle. NEVER includes secret values — only env var NAMES, never
# their values (names like AWS_SECRET_ACCESS_KEY are fine; the value is what's secret).


def _cmd(args: list[str]) -> str:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return (out.stdout or out.stderr).strip()[:2000]
    except Exception as exc:  # noqa: BLE001
        return f"<error: {exc}>"


def collect(api_url: str, runner: str) -> dict:
    usage = shutil.disk_usage("/")
    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "runner": runner,
        "api_url": api_url,
        "tools": {
            "tofu": _cmd(["tofu", "version"]),
            "terraform": _cmd(["terraform", "version"]),
            "git": _cmd(["git", "--version"]),
        },
        "disk": {
            "total_gb": round(usage.total / 1e9, 1),
            "free_gb": round(usage.free / 1e9, 1),
            "used_pct": round(100 * usage.used / usage.total, 1),
        },
        "docker_ps": _cmd(["docker", "ps", "--format", "{{.Names}} {{.Status}}"])
        if runner == "docker"
        else "n/a (local runner)",
        # Names only — values are never exposed.
        "env_var_names": sorted(os.environ.keys()),
        "recent_logs": recent_logs()[-50:],
    }
