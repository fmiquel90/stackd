from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path

import httpx

from agent.cancel import JobCanceled
from agent.cancel import current as current_cancel
from agent.client import ApiClient, transient
from agent.docker_runner import DockerRunner
from agent.logging import get_logger
from agent.masking import Masker

log = get_logger()

# Upper bound on any single subprocess so a hung tofu/hook can't pin a worker forever (the apply
# phase is the longest legitimate one). Exit 124 mirrors coreutils `timeout`.
_DEFAULT_TIMEOUT_SECONDS = 2 * 60 * 60
# A hook's own cap (§8.3): 10 min, which the worker's operator may change. Past it the hook is
# killed and fails, by its `on_failure`.
_HOOK_TIMEOUT_SECONDS = 10 * 60


def _hook_timeout() -> int:
    return int(os.environ.get("STACKD_HOOK_TIMEOUT_SECONDS") or _HOOK_TIMEOUT_SECONDS)


# The agent's log buffer (§5.1): a command's output goes out once its oldest unsent line has waited
# this long, or once that much of it is waiting, whichever comes first.
FLUSH_SECONDS = 1.0
FLUSH_BYTES = 32 * 1024
# What a job holds of its output the API has not taken yet. Past it, new lines are dropped (and
# counted) rather than stall the reader: tofu would block on a full stdout pipe.
BUFFER_BYTES = 8 * 1024 * 1024
# How long a command's end (and the job's) waits for its output to reach the API.
DRAIN_SECONDS = 30.0
# A chunk the API did not take is sent again after this pause, for as long as the job runs; each
# send retries on its own (the client's backoff) for SEND_RETRY_SECONDS.
RESEND_SECONDS = 2.0
SEND_RETRY_SECONDS = 10.0


@dataclass
class _Chunk:
    phase: str
    section: str | None
    seq: int
    lines: list[dict]
    size: int


class LogStreamer:
    """Ships a job's log lines to the API (§5.1), masked, with a strictly increasing seq per phase.
    `emit` never touches the network and never raises: it masks the lines and queues them. One
    sender thread ships the queue in order — so each phase's chunks in seq order — and sends a
    chunk the API did not take again (the API is idempotent on (run, phase, seq)) for as long as the
    job runs. A log failure never ends, kills or stalls the command. The queue is bounded
    (BUFFER_BYTES): past it lines are dropped, and a `[stackd] N log line(s) dropped` line takes
    their place."""

    def __init__(self, client: ApiClient, job_id: str, masker: Masker) -> None:
        self._client = client
        self._job_id = job_id
        self._masker = masker
        self._seq: dict[str, int] = {}
        self._cond = threading.Condition()
        self._queue: deque[_Chunk] = deque()
        self._size = 0
        self._dropped: dict[tuple[str, str | None], int] = {}
        self._closed = False
        self._abandoned = False
        self._sender: threading.Thread | None = None
        self._failing = False  # the sender's own: an outage is logged once, not per chunk

    def emit(self, phase: str, lines: list[str], section: str | None = None) -> None:
        if not lines:
            return
        t = time.strftime("%H:%M:%S")
        payload = [{"t": t, "msg": self._masker.mask(line)} for line in lines]
        size = sum(len(m["msg"].encode()) + 1 for m in payload)
        with self._cond:
            if self._closed:
                return
            if self._size + size > BUFFER_BYTES:
                key = (phase, section)
                self._dropped[key] = self._dropped.get(key, 0) + len(lines)
                return
            self._mark_dropped()
            self._push(phase, section, payload, size)

    def _push(self, phase: str, section: str | None, payload: list[dict], size: int) -> None:
        # Caller holds the lock. A chunk takes its seq as it is queued: the queue's order is the
        # order the API receives each phase's chunks in.
        seq = self._seq.get(phase, 0)
        self._seq[phase] = seq + 1
        self._queue.append(_Chunk(phase, section, seq, payload, size))
        self._size += size
        if self._sender is None:
            self._sender = threading.Thread(target=self._run, name="log-sender", daemon=True)
            self._sender.start()
        self._cond.notify_all()

    def _mark_dropped(self) -> None:
        # Caller holds the lock. A marker is a few bytes: it goes in even over the bound.
        for (phase, section), n in self._dropped.items():
            msg = f"[stackd] {n} log line(s) dropped: the API did not take them in time"
            self._push(phase, section, [{"t": time.strftime("%H:%M:%S"), "msg": msg}], len(msg))
        self._dropped.clear()

    def _ship(self, chunk: _Chunk) -> bool:
        """True once the chunk is done with: taken, or refused for good (a 4xx — sending it again
        would not change the answer). False: try again later."""
        try:
            self._client.logs(
                self._job_id,
                chunk.phase,
                chunk.seq,
                chunk.lines,
                section=chunk.section,
                retry_for=SEND_RETRY_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 — the sender never dies on a failed send
            if isinstance(exc, httpx.HTTPStatusError) and not transient(exc):
                log.warning(
                    "log chunk refused",
                    extra={
                        "event": "agent.log_refused",
                        "run_id": self._job_id,
                        "phase": chunk.phase,
                        "seq": chunk.seq,
                        "status": exc.response.status_code,
                    },
                )
                return True
            if not self._failing:
                self._failing = True
                log.warning(
                    "log shipping failing, holding the lines",
                    extra={
                        "event": "agent.log_retry",
                        "run_id": self._job_id,
                        "error": type(exc).__name__,
                    },
                )
            return False
        if self._failing:
            self._failing = False
            log.info(
                "log shipping resumed", extra={"event": "agent.log_resumed", "run_id": self._job_id}
            )
        return True

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._closed:
                    self._cond.wait()
                if not self._queue or self._abandoned:
                    return
                chunk = self._queue[0]
            done = self._ship(chunk)
            with self._cond:
                if done:
                    self._queue.popleft()
                    self._size -= chunk.size
                    self._cond.notify_all()
                elif not self._abandoned:
                    self._cond.wait(RESEND_SECONDS)

    def flush(self, timeout: float | None = None) -> bool:
        """Wait, up to `timeout` (DRAIN_SECONDS), until every line emitted so far has reached the
        API. False when some has not yet: it stays queued, the sender keeps trying."""
        end = time.monotonic() + (DRAIN_SECONDS if timeout is None else timeout)
        with self._cond:
            self._mark_dropped()
            while self._queue:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)
            return True

    def close(self, timeout: float | None = None) -> None:
        """The job is over: send what is left, for up to `timeout` (DRAIN_SECONDS), then stop. What
        the API has still not taken by then is given up — the job's end is not held for it."""
        delivered = self.flush(timeout)
        with self._cond:
            self._closed = True
            if not delivered:
                self._abandoned = True
                lost = sum(len(c.lines) for c in self._queue)
                log.warning(
                    "log lines not delivered",
                    extra={"event": "agent.log_lost", "run_id": self._job_id, "lines": lost},
                )
            self._cond.notify_all()
            sender = self._sender
        if sender is not None and delivered:
            sender.join(timeout=1)


class _LogBatch:
    """One command's output on its way to the API (§5.1): handed to the streamer when the oldest
    buffered line is FLUSH_SECONDS old, or FLUSH_BYTES are buffered, and the rest on `close()`. A
    quiet command's last lines still reach the viewer within a second: a timer thread, asleep on a
    condition (no polling), hands them over while the reader is blocked on the command's next line.
    Handing over only queues (`LogStreamer.emit`): the reader never waits on the API."""

    def __init__(self, streamer: LogStreamer, phase: str, section: str | None) -> None:
        self._streamer = streamer
        self._phase = phase
        self._section = section
        self._cond = threading.Condition()
        self._lines: list[str] = []
        self._size = 0
        self._since: float | None = None  # when the oldest buffered line arrived
        self._closed = False
        self._timer = threading.Thread(target=self._run, daemon=True)
        self._timer.start()

    def _flush(self) -> None:
        # Caller holds the lock.
        lines, self._lines, self._size, self._since = self._lines, [], 0, None
        self._streamer.emit(self._phase, lines, section=self._section)

    def add(self, lines: list[str]) -> None:
        if not lines:
            return
        with self._cond:
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
                self._flush()

    def close(self) -> None:
        """Stop the timer, then hand over what is left."""
        with self._cond:
            self._closed = True
            self._cond.notify()
        self._timer.join()
        with self._cond:
            if self._lines:
                self._flush()


class LocalRunner:
    """The `local` runner (trusted dev only, §7.4): every command a subprocess of the agent, as its
    own OS user, in its own process group."""

    name = "local"

    def for_repo(self) -> LocalRunner:
        """The runner for an untrusted repo hook — the same one: only the env differs (§8.3)."""
        return self

    def for_init(self) -> LocalRunner:
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
            # A byte that is not UTF-8 (a binary dump, a legacy-encoded file a hook cats) is shown
            # as U+FFFD: it must not end the job.
            encoding="utf-8",
            errors="replace",
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
                _kill_group()  # reading the output failed: leave nothing running
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
        self,
        cmd: list[str],
        cwd: Path,
        env: dict[str, str],
        *,
        timeout: int = _DEFAULT_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        """Run a command to completion, capturing stdout and stderr apart (`tofu show -json`)."""
        return subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
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
    finally:
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
    # The command's output reaches the API before what the job reports next — bounded: an API
    # that does not take it only delays the job, the lines stay queued.
    streamer.flush()
    return 124 if timed_out else rc


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
    each event (masked) while collecting the structured events the caller reads — `change_summary`
    and the error-level ones (diagnostics) — and no other: a large plan's thousands of
    per-resource events are streamed, not kept. Diagnostics (errors/warnings) are expanded to their
    full detail + location — the bare `@message` is just the headline ("Error: Invalid value for
    input variable"). Non-JSON lines are streamed verbatim. Returns (exit_code, events)."""
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
        if evt.get("type") == "change_summary" or evt.get("@level") == "error":
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
    bound: Callable[[int], int] | None = None,
) -> tuple[list[dict], bool]:
    """Run a stage's hooks. Returns (check_results, aborted). A `fail` hook aborts; `warn` continues
    but is surfaced and will force manual confirmation (§8.3). A hook running past its timeout
    (`STACKD_HOOK_TIMEOUT_SECONDS`, 10 min) is killed and fails the same way. Repo hooks get `repo_env` (no
    secrets / cloud creds) and `runner.for_repo()` — on the docker runner, a container that sees the
    checkout only; platform hooks get `platform_env` and the job's runner, and run inside
    `platform_files()` (the secret files only they may read, on disk only while one of them
    runs). `bound` cuts a hook's timeout to the job's credential deadline (agent.deadline)."""
    results: list[dict] = []
    for hook in hooks:
        section = f"hook:{hook['name']}"
        repo = hook.get("source") == "repo"
        timeout = bound(_hook_timeout()) if bound else _hook_timeout()
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
