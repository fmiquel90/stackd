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

from agent.cancel import JobCanceled
from agent.cancel import current as current_cancel
from agent.client import ApiClient
from agent.docker_runner import DockerRunner
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
    """Ships log lines per phase, masked, with a strictly increasing seq (§5.1). Thread-safe: a
    command's batch can be flushed by its timer thread (`_LogBatch`)."""

    def __init__(self, client: ApiClient, job_id: str, masker: Masker) -> None:
        self._client = client
        self._job_id = job_id
        self._masker = masker
        self._seq: dict[str, int] = {}
        self._lock = threading.Lock()

    def emit(self, phase: str, lines: list[str], section: str | None = None) -> None:
        if not lines:
            return
        payload = [
            {"t": time.strftime("%H:%M:%S"), "msg": self._masker.mask(line)} for line in lines
        ]
        # A seq and its chunk go out together: chunks reach the API in seq order.
        with self._lock:
            seq = self._seq.get(phase, 0)
            self._seq[phase] = seq + 1
            self._client.logs(self._job_id, phase, seq, payload, section=section)


# The agent's log buffer (§5.1): a command's output goes out once its oldest unsent line has waited
# this long, or once that much of it is waiting, whichever comes first.
FLUSH_SECONDS = 1.0
FLUSH_BYTES = 32 * 1024


class _LogBatch:
    """One command's output on its way to the API (§5.1): sent when the oldest buffered line is
    FLUSH_SECONDS old, or FLUSH_BYTES are buffered, and the rest on `close()`. A quiet command's
    last lines still reach the viewer within a second: a timer thread, asleep on a condition (no
    polling), sends them while the reader is blocked on the command's next line. Lines are sent in
    order, under one lock. A send that fails on the timer thread is raised to the reader, on its
    next `add()` or on `close()` — as a failed send on the reader itself would be."""

    def __init__(self, streamer: LogStreamer, phase: str, section: str | None) -> None:
        self._streamer = streamer
        self._phase = phase
        self._section = section
        self._cond = threading.Condition()
        self._lines: list[str] = []
        self._size = 0
        self._since: float | None = None  # when the oldest buffered line arrived
        self._closed = False
        self._error: Exception | None = None
        self._timer = threading.Thread(target=self._run, daemon=True)
        self._timer.start()

    def _flush(self) -> None:
        # Caller holds the lock.
        lines, self._lines, self._size, self._since = self._lines, [], 0, None
        self._streamer.emit(self._phase, lines, section=self._section)

    def _raise_failed(self) -> None:
        if self._error is not None:
            error, self._error = self._error, None
            raise error

    def add(self, lines: list[str]) -> None:
        if not lines:
            return
        with self._cond:
            self._raise_failed()
            if self._since is None:
                self._since = time.monotonic()
                self._cond.notify()  # wake the timer: a deadline now runs
            self._lines.extend(lines)
            self._size += sum(len(line.encode()) + 1 for line in lines)
            if self._size >= FLUSH_BYTES:
                self._flush()

    def _run(self) -> None:
        with self._cond:
            while not self._closed:
                if self._since is None:
                    self._cond.wait()  # nothing buffered: sleep until a line (or close) arrives
                    continue
                left = self._since + FLUSH_SECONDS - time.monotonic()
                if left > 0:
                    self._cond.wait(left)
                    continue
                try:
                    self._flush()
                except Exception as exc:  # noqa: BLE001 — handed to the reader thread
                    self._error = exc
                    return

    def close(self, *, flush: bool = True) -> None:
        """Stop the timer, then send what is left (unless the command's streaming failed)."""
        with self._cond:
            self._closed = True
            self._cond.notify()
        self._timer.join()
        with self._cond:
            if not flush:
                return
            self._raise_failed()
            if self._lines:
                self._flush()


class LocalRunner:
    """The `local` runner (trusted dev only, §7.4): every command a subprocess of the agent, as its
    own OS user, in its own process group."""

    name = "local"

    def for_repo(self) -> LocalRunner:
        """The runner for an untrusted repo hook — the same one: only the env differs (§8.3)."""
        return self

    def stream(
        self,
        cmd: list[str],
        cwd: Path,
        env: dict[str, str],
        *,
        timeout: int,
        on_line: Callable[[str], None],
    ) -> tuple[int, bool]:
        """Run a command, handing each output line (stdout and stderr merged) to `on_line`. Returns
        (exit code, killed by the watchdog). The command runs in its own process group, and
        nothing of that group outlives it: once the command itself exits, whatever it left running
        there is killed (§8.3) — a hook's background process (`cmd &`, detached from its output or
        not), a program repo code started inside tofu. Such a process would keep the job's
        environment (`/proc/<pid>/environ`, readable by any later hook running as the same user)
        and see what the next steps put on disk: the tfvars and the OIDC token file while tofu
        runs, the raw plan JSON while a platform hook does. A watchdog also kills the group past
        `timeout`. A process that leaves the group (`setsid`) is not caught: only the `docker`
        runner contains it (one container per command, §7.4)."""
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
            # The group's id is the command's pid. Only ever called before `proc.wait()` reaps it:
            # until then the pid can't be reused, so this reaches this command's group only.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        def _kill() -> None:
            killed["v"] = True
            _kill_group()

        def _end_group() -> None:
            # Wait for the command to exit WITHOUT reaping it (WNOWAIT), then kill what it left
            # behind. This also closes the output pipe a background child still holds.
            try:
                os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)
            except ChildProcessError:
                return
            _kill_group()

        def _interrupt_group() -> None:
            # SIGINT, as ^C: tofu stops gracefully (finishes what is in flight, releases its lock).
            try:
                os.killpg(proc.pid, signal.SIGINT)
            except ProcessLookupError:
                pass

        token = current_cancel()
        if token is not None:
            token.attach(_interrupt_group, _kill_group)
        ender = threading.Thread(target=_end_group, daemon=True)
        ender.start()
        timer = threading.Timer(timeout, _kill)
        timer.start()
        try:
            if proc.stdout is None:
                raise RuntimeError("subprocess stdout pipe missing")
            for line in proc.stdout:
                on_line(line.rstrip("\n"))
            ender.join()  # the command has exited, its group is killed; nothing has reaped it yet
        finally:
            if ender.is_alive():
                _kill_group()  # streaming failed (e.g. the API is unreachable): leave nothing
                ender.join()
            timer.cancel()
            timer.join()  # a watchdog kill under way ends before the reap below frees the pid
            if token is not None:
                token.detach()  # before the reap: a late signal must not reach a reused pid
            rc = proc.wait()
        # Only a timeout if the watchdog actually killed a still-running process (negative rc); a
        # process that exited a hair before the timer fired keeps its real exit code.
        return rc, killed["v"] and rc < 0

    def capture(
        self, cmd: list[str], cwd: Path, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        """Run a command to completion, capturing stdout and stderr apart (`tofu show -json`)."""
        return subprocess.run(
            cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=_DEFAULT_TIMEOUT_SECONDS
        )


Runner = LocalRunner | DockerRunner
LOCAL = LocalRunner()


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
    runner: Runner = LOCAL,
) -> int:
    """Run a command on `runner`, applying `transform` to each output line before streaming it
    (return None to skip a line, or a list to expand one line into several). Past `timeout` the
    command is killed: exit 124, like coreutils `timeout`."""
    token = current_cancel()
    if token is not None and token.should_stop:
        raise JobCanceled  # asked to stop between two commands: don't start the next one
    batch = _LogBatch(streamer, phase, section)

    def on_line(line: str) -> None:
        out = transform(line)
        if out is not None:
            batch.add([out] if isinstance(out, str) else out)

    try:
        rc, timed_out = runner.stream(cmd, cwd, env, timeout=timeout, on_line=on_line)
    except BaseException:
        batch.close(flush=False)  # streaming failed (e.g. the API is unreachable): don't retry it
        raise
    batch.close()
    # A command that exited 0 completed whatever the signal: the stop raced its end (it reached the
    # process as it exited). An apply that went through must not be reported as interrupted — the
    # job carries on, and a stop still in force ends it before the next command (above). Once
    # disarmed (the apply succeeded), a later failure is a failure, not a stop.
    if token is not None and token.interrupted and token.should_stop and rc != 0:
        streamer.emit(phase, ["[stackd] stopped on request"], section=section)
        raise JobCanceled
    if timed_out:
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
    runner: Runner = LOCAL,
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
        runner=runner,
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
    runner: Runner = LOCAL,
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
        runner=runner,
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
    runner: Runner = LOCAL,
) -> tuple[list[dict], bool]:
    """Run a stage's hooks. Returns (check_results, aborted). A `fail` hook aborts; `warn` continues
    but is surfaced and will force manual confirmation (§8.3). A hook running past its timeout
    (`STACKD_HOOK_TIMEOUT_SECONDS`, 10 min) is killed and fails the same way. Repo hooks get `repo_env` (no
    secrets / cloud creds) and `runner.for_repo()` — on the docker runner, a container that sees the
    checkout only; platform hooks get `platform_env` and the job's runner, and run inside
    `platform_files()` (the secret files only they may read, on disk only while one of them
    runs)."""
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
                runner=runner.for_repo() if repo else runner,
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
