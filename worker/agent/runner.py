from __future__ import annotations

import subprocess
import time
from pathlib import Path

from agent.client import ApiClient
from agent.masking import Masker


class LogStreamer:
    """Buffers and ships log lines per phase, masked, with a strictly increasing seq (§5.1)."""

    def __init__(self, client: ApiClient, job_id: str, masker: Masker) -> None:
        self._client = client
        self._job_id = job_id
        self._masker = masker
        self._seq: dict[str, int] = {}

    def emit(self, phase: str, lines: list[str], section: str | None = None) -> None:
        if not lines:
            return
        seq = self._seq.get(phase, 0)
        self._seq[phase] = seq + 1
        payload = [
            {"t": time.strftime("%H:%M:%S"), "msg": self._masker.mask(line)} for line in lines
        ]
        self._client.logs(self._job_id, phase, seq, payload, section=section)


def run_command(
    cmd: list[str],
    cwd: Path,
    env: dict[str, str],
    *,
    phase: str,
    section: str | None,
    streamer: LogStreamer,
) -> int:
    """Run a command, streaming masked stdout/stderr to the API. Returns the exit code."""
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    buffer: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        buffer.append(line.rstrip("\n"))
        if len(buffer) >= 20:
            streamer.emit(phase, buffer, section=section)
            buffer = []
    streamer.emit(phase, buffer, section=section)
    return proc.wait()


def run_hooks(
    hooks: list[dict],
    cwd: Path,
    *,
    platform_env: dict[str, str],
    repo_env: dict[str, str],
    phase: str,
    streamer: LogStreamer,
) -> tuple[list[dict], bool]:
    """Run a stage's hooks. Returns (check_results, aborted). A `fail` hook aborts; `warn` continues
    but is surfaced and will force manual confirmation (§8.3). Repo hooks get `repo_env` (no
    secrets / cloud creds); platform hooks get `platform_env`."""
    results: list[dict] = []
    for hook in hooks:
        section = f"hook:{hook['name']}"
        env = repo_env if hook.get("source") == "repo" else platform_env
        code = run_command(
            ["sh", "-c", hook["command"]], cwd, env, phase=phase, section=section, streamer=streamer
        )
        if code == 0:
            results.append({"name": hook["name"], "status": "ok", "detail": None})
            continue
        on_failure = hook.get("on_failure", "fail")
        status = "fail" if on_failure == "fail" else "warn"
        results.append({"name": hook["name"], "status": status, "detail": f"exit {code}"})
        if status == "fail":
            return results, True
    return results, False
