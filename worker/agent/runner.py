from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from agent.client import ApiClient
from agent.masking import Masker

# Upper bound on any single subprocess so a hung tofu/hook can't pin a worker forever (the apply
# phase is the longest legitimate one). Exit 124 mirrors coreutils `timeout`.
_DEFAULT_TIMEOUT_SECONDS = 2 * 60 * 60


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


def _stream_proc(
    cmd: list[str],
    cwd: Path,
    env: dict[str, str],
    *,
    phase: str,
    section: str | None,
    streamer: LogStreamer,
    timeout: int,
    transform: Callable[[str], str | None],
) -> int:
    """Run a command, applying `transform` to each output line before streaming it (return None to
    skip a line). A watchdog kills the process past `timeout` (exit 124, like coreutils `timeout`)."""
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    killed = {"v": False}

    def _kill() -> None:
        killed["v"] = True
        proc.kill()

    timer = threading.Timer(timeout, _kill)
    timer.start()
    try:
        if proc.stdout is None:
            raise RuntimeError("subprocess stdout pipe missing")
        buffer: list[str] = []
        for line in proc.stdout:
            text = transform(line.rstrip("\n"))
            if text is None:
                continue
            buffer.append(text)
            if len(buffer) >= 20:
                streamer.emit(phase, buffer, section=section)
                buffer = []
        streamer.emit(phase, buffer, section=section)
        rc = proc.wait()
    finally:
        timer.cancel()
    # Only report a timeout if the watchdog actually killed a still-running process (negative rc);
    # a process that exited a hair before the timer fired keeps its real exit code.
    if killed["v"] and rc < 0:
        streamer.emit(
            phase, [f"[stackd] timed out after {timeout}s — process killed"], section=section
        )
        return 124
    return rc


def run_command(
    cmd: list[str],
    cwd: Path,
    env: dict[str, str],
    *,
    phase: str,
    section: str | None,
    streamer: LogStreamer,
    timeout: int = _DEFAULT_TIMEOUT_SECONDS,
) -> int:
    """Run a command, streaming masked stdout/stderr to the API. Returns the exit code."""
    return _stream_proc(
        cmd,
        cwd,
        env,
        phase=phase,
        section=section,
        streamer=streamer,
        timeout=timeout,
        transform=lambda line: line,
    )


def stream_json_command(
    cmd: list[str],
    cwd: Path,
    env: dict[str, str],
    *,
    phase: str,
    section: str | None,
    streamer: LogStreamer,
    timeout: int = _DEFAULT_TIMEOUT_SECONDS,
) -> tuple[int, list[dict]]:
    """Run a `-json` tofu/terraform command (plan/apply): stream the human-readable `@message` of
    each event (masked) while collecting the structured events (`change_summary`, `diagnostic`, …)
    for the caller. Non-JSON lines are streamed verbatim. Returns (exit_code, events)."""
    events: list[dict] = []

    def _tx(line: str) -> str | None:
        if not line:
            return None
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            return line  # not a JSON event (e.g. a stray stderr line) — stream as-is
        if isinstance(evt, dict) and "@message" in evt:
            events.append(evt)
            return str(evt["@message"])
        return line

    rc = _stream_proc(
        cmd,
        cwd,
        env,
        phase=phase,
        section=section,
        streamer=streamer,
        timeout=timeout,
        transform=_tx,
    )
    return rc, events


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
