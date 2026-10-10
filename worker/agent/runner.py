from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path

from agent.client import ApiClient
from agent.masking import Masker

# Upper bound on any single subprocess so a hung tofu/hook can't pin a worker forever (the apply
# phase is the longest legitimate one). Exit 124 mirrors coreutils `timeout`.
_DEFAULT_TIMEOUT_SECONDS = 2 * 60 * 60
# A hook's own cap (§8.3): 10 min, which the worker's operator may change. Past it the hook is
# killed and fails, by its `on_failure`.
_HOOK_TIMEOUT_SECONDS = 10 * 60


def _hook_timeout() -> int:
    return int(os.environ.get("STACKD_HOOK_TIMEOUT_SECONDS") or _HOOK_TIMEOUT_SECONDS)


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
    transform: Callable[[str], str | list[str] | None],
) -> int:
    """Run a command, applying `transform` to each output line before streaming it (return None to
    skip a line, or a list to expand one line into several). The command runs in its own process
    group, and nothing of that group outlives it: once the command itself exits, whatever it left
    running there is killed (§8.3) — a hook's background process (`cmd &`, detached from its output
    or not), a program repo code started inside tofu. Such a process would keep the job's
    environment (`/proc/<pid>/environ`, readable by any later hook running as the same user) and see
    what the next steps put on disk: the tfvars and the OIDC token file while tofu runs, the raw
    plan JSON while a platform hook does. A watchdog also kills the group past `timeout` (exit 124,
    like coreutils `timeout`). A process that leaves the group (`setsid`) is not caught: only the
    `docker` runner can contain it (§7.4)."""
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    killed = {"v": False}

    def _kill_group() -> None:
        # The group's id is the command's pid. Only ever called before `proc.wait()` reaps it: until
        # then the pid can't be reused, so this reaches this command's group and nothing else.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def _kill() -> None:
        killed["v"] = True
        _kill_group()

    def _end_group() -> None:
        # Wait for the command to exit WITHOUT reaping it (WNOWAIT), then kill what it left behind.
        # This also closes the output pipe a background child still holds, ending the read below.
        try:
            os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)
        except ChildProcessError:
            return
        _kill_group()

    ender = threading.Thread(target=_end_group, daemon=True)
    ender.start()
    timer = threading.Timer(timeout, _kill)
    timer.start()
    try:
        if proc.stdout is None:
            raise RuntimeError("subprocess stdout pipe missing")
        buffer: list[str] = []
        for line in proc.stdout:
            out = transform(line.rstrip("\n"))
            if out is None:
                continue
            buffer.extend([out] if isinstance(out, str) else out)
            if len(buffer) >= 20:
                streamer.emit(phase, buffer, section=section)
                buffer = []
        streamer.emit(phase, buffer, section=section)
        ender.join()  # the command has exited, its group is killed; nothing has reaped it yet
    finally:
        if ender.is_alive():
            _kill_group()  # streaming failed (e.g. the API is unreachable): leave nothing running
            ender.join()
        timer.cancel()
        timer.join()  # a watchdog kill under way ends before the reap below frees the pid
        rc = proc.wait()
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
    for the caller. Diagnostics (errors/warnings) are expanded to their full detail + location —
    the bare `@message` is just the headline ("Error: Invalid value for input variable"). Non-JSON
    lines are streamed verbatim. Returns (exit_code, events)."""
    events: list[dict] = []

    def _tx(line: str) -> str | list[str] | None:
        if not line:
            return None
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            return line  # not a JSON event (e.g. a stray stderr line) — stream as-is
        if not isinstance(evt, dict) or "@message" not in evt:
            return line
        events.append(evt)
        diag = evt.get("diagnostic")
        if not isinstance(diag, dict):
            return str(evt["@message"])
        # Reconstruct what `terraform plan` prints for a diagnostic: headline, location, detail.
        out = [str(evt["@message"])]
        rng = diag.get("range") or {}
        if rng.get("filename"):
            start = (rng.get("start") or {}).get("line")
            out.append(f"  on {rng['filename']}" + (f", line {start}" if start else "") + ":")
        if diag.get("detail"):
            out += [f"  {dl}" for dl in str(diag["detail"]).splitlines()]
        return out

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
    platform_files: Callable[[], AbstractContextManager[object]] = nullcontext,
) -> tuple[list[dict], bool]:
    """Run a stage's hooks. Returns (check_results, aborted). A `fail` hook aborts; `warn` continues
    but is surfaced and will force manual confirmation (§8.3). A hook running past its timeout
    (`STACKD_HOOK_TIMEOUT_SECONDS`, 10 min) is killed and fails the same way. Repo hooks get `repo_env` (no
    secrets / cloud creds); platform hooks get `platform_env`, and run inside `platform_files()`
    (the secret files only they may read, on disk only while one of them runs)."""
    results: list[dict] = []
    for hook in hooks:
        section = f"hook:{hook['name']}"
        repo = hook.get("source") == "repo"
        timeout = _hook_timeout()
        with nullcontext() if repo else platform_files():
            code = run_command(
                ["sh", "-c", hook["command"]],
                cwd,
                repo_env if repo else platform_env,
                phase=phase,
                section=section,
                streamer=streamer,
                timeout=timeout,
            )
        if code == 0:
            results.append({"name": hook["name"], "status": "ok", "detail": None})
            continue
        on_failure = hook.get("on_failure", "fail")
        status = "fail" if on_failure == "fail" else "warn"
        detail = f"timed out after {timeout}s" if code == 124 else f"exit {code}"
        results.append({"name": hook["name"], "status": status, "detail": detail})
        if status == "fail":
            return results, True
    return results, False
