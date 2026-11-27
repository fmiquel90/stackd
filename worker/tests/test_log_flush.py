"""The agent's log buffer (SPECS §5.1: 1 s / 32 KB): a command's output is sent once its oldest
unsent line has waited FLUSH_SECONDS or FLUSH_BYTES are waiting, and the rest when it exits — a quiet
command's last line reaches the viewer without waiting for the next one. Shipping is decoupled from
the command: a slow or unreachable API never stalls, kills or fails it."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

import agent.runner as runner
from agent.masking import Masker
from agent.runner import LogStreamer, _LogBatch, run_command

_REQUEST = httpx.Request("POST", "http://api/worker/v1/jobs/job/logs")


class _Client:
    def __init__(self, fail: int = 0, error: Exception | None = None) -> None:
        self.chunks: list[tuple[float, str, int, list[str]]] = []
        self.fail = fail  # how many sends fail before the API takes them
        self.error = error or httpx.ConnectError("API unreachable", request=_REQUEST)
        self.sent = threading.Event()
        self.gate = threading.Event()  # cleared: the API hangs
        self.gate.set()
        self.lock = threading.Lock()

    def logs(self, job_id: str, phase: str, seq: int, lines: list[dict], **kw: Any) -> None:
        self.gate.wait()
        with self.lock:
            if self.fail:
                self.fail -= 1
                raise self.error
            self.chunks.append((time.monotonic(), phase, seq, [ln["msg"] for ln in lines]))
        self.sent.set()

    def lines(self) -> list[str]:
        return [line for c in self.chunks for line in c[3]]


def _streamer(client: _Client) -> LogStreamer:
    return LogStreamer(client, "job", Masker(["s3cr3t"]))  # type: ignore[arg-type]


def test_quiet_command_flushed_on_time(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(runner, "FLUSH_SECONDS", 0.2)
    client = _Client()
    start = time.monotonic()
    rc = run_command(
        ["sh", "-c", "echo a; sleep 1.5; echo b"],
        tmp_path,
        {"PATH": "/usr/bin:/bin"},
        phase="planning",
        section=None,
        streamer=_streamer(client),
    )
    assert rc == 0
    assert [c[3] for c in client.chunks] == [["a"], ["b"]]
    # `a` went out while the command was still sleeping, not with `b` at its end.
    assert client.chunks[0][0] - start < 1.2
    assert [c[2] for c in client.chunks] == [0, 1]  # seq strictly increasing


def test_flushed_on_size(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(runner, "FLUSH_SECONDS", 60.0)  # only the size (or the end) flushes
    monkeypatch.setattr(runner, "FLUSH_BYTES", 10)
    client = _Client()
    run_command(
        ["sh", "-c", "printf 'aaaa\\nbbbb\\ncccc\\n'"],
        tmp_path,
        {"PATH": "/usr/bin:/bin"},
        phase="planning",
        section=None,
        streamer=_streamer(client),
    )
    # 5 bytes a line (newline included): a chunk goes once 10 bytes wait, the rest at the end.
    assert [c[3] for c in client.chunks] == [["aaaa", "bbbb"], ["cccc"]]


def test_batch_timer_sends_without_a_further_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "FLUSH_SECONDS", 0.1)
    client = _Client()
    streamer = _streamer(client)
    batch = _LogBatch(streamer, "applying", "hook:x")
    batch.add(["one", "token s3cr3t"])
    assert client.sent.wait(2)  # the timer sent it: no add, no close
    assert client.chunks[0][3] == ["one", "token ***"]  # masked on the way
    batch.close()
    streamer.close()
    assert len(client.chunks) == 1  # nothing left, nothing re-sent


def test_close_sends_the_rest_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "FLUSH_SECONDS", 60.0)
    client = _Client()
    streamer = _streamer(client)
    batch = _LogBatch(streamer, "planning", None)
    batch.add(["x"])
    batch.add(["y", "z"])
    assert client.chunks == []
    batch.close()
    assert streamer.flush(5)
    assert [c[3] for c in client.chunks] == [["x", "y", "z"]]


def test_a_failing_log_api_never_ends_the_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The API refuses every log POST: the command still runs to its end, nothing kills it, and
    the job carries on once the bounded drain gives up."""
    monkeypatch.setattr(runner, "FLUSH_SECONDS", 0.05)
    monkeypatch.setattr(runner, "FLUSH_BYTES", 16)
    monkeypatch.setattr(runner, "DRAIN_SECONDS", 0.5)
    monkeypatch.setattr(runner, "RESEND_SECONDS", 0.05)
    client = _Client(fail=10**9)
    streamer = _streamer(client)
    done = tmp_path / "done"
    rc = run_command(
        ["sh", "-c", f"for i in 1 2 3 4 5; do echo line $i; sleep 0.1; done; touch {done}"],
        tmp_path,
        {"PATH": "/usr/bin:/bin"},
        phase="applying",
        section=None,
        streamer=streamer,
    )
    assert rc == 0
    assert done.exists()  # it ran to completion: no SIGKILL mid-apply
    started = time.monotonic()
    streamer.close(0.2)
    assert time.monotonic() - started < 2


def test_a_hung_api_does_not_stall_the_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The API hangs on the first chunk: the reader keeps draining the pipe — far more than a pipe
    buffer of output — so the command is never blocked on its stdout."""
    monkeypatch.setattr(runner, "DRAIN_SECONDS", 0.2)
    client = _Client()
    client.gate.clear()
    streamer = _streamer(client)
    done = tmp_path / "done"
    started = time.monotonic()
    rc = run_command(
        [
            "sh",
            "-c",
            f"i=0; while [ $i -lt 20000 ]; do echo line-$i; i=$((i+1)); done; touch {done}",
        ],
        tmp_path,
        {"PATH": "/usr/bin:/bin"},
        phase="applying",
        section=None,
        streamer=streamer,
    )
    assert rc == 0 and done.exists()
    assert time.monotonic() - started < 10
    client.gate.set()
    assert streamer.flush(5)
    lines = client.lines()
    assert lines[0] == "line-0" and lines[-1] == "line-19999" and len(lines) == 20000


def test_a_chunk_is_sent_again_until_the_api_takes_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "RESEND_SECONDS", 0.01)
    client = _Client(fail=3)
    streamer = _streamer(client)
    streamer.emit("planning", ["a"])
    streamer.emit("planning", ["b"])
    streamer.emit("checking", ["c"])
    assert streamer.flush(5)
    # In order, each phase's seq strictly increasing, nothing lost or skipped.
    assert [(c[1], c[2], c[3]) for c in client.chunks] == [
        ("planning", 0, ["a"]),
        ("planning", 1, ["b"]),
        ("checking", 0, ["c"]),
    ]


def test_a_chunk_refused_for_good_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    refused = httpx.HTTPStatusError(
        "422", request=_REQUEST, response=httpx.Response(422, request=_REQUEST)
    )
    client = _Client(fail=1, error=refused)
    streamer = _streamer(client)
    streamer.emit("planning", ["bad"])
    streamer.emit("planning", ["good"])
    assert streamer.flush(5)
    assert [(c[2], c[3]) for c in client.chunks] == [(1, ["good"])]


def test_a_full_buffer_drops_lines_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "BUFFER_BYTES", 20)
    client = _Client()
    client.gate.clear()
    streamer = _streamer(client)
    streamer.emit("applying", ["0123456789"])  # 11 bytes: queued
    streamer.emit("applying", ["abcdefghij", "klmnopqrst"])  # over the bound: dropped
    streamer.emit("applying", ["uvwxyz0123"])  # still over: dropped
    client.gate.set()
    assert streamer.flush(5)
    streamer.emit("applying", ["after"])
    assert streamer.flush(5)
    assert [(c[2], c[3]) for c in client.chunks] == [
        (0, ["0123456789"]),
        (1, ["[stackd] 3 log line(s) dropped: the API did not take them in time"]),
        (2, ["after"]),
    ]


def test_close_gives_up_on_an_unreachable_api_in_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "RESEND_SECONDS", 0.01)
    client = _Client(fail=10**9)
    streamer = _streamer(client)
    streamer.emit("applying", ["lost"])
    started = time.monotonic()
    streamer.close(0.3)
    assert time.monotonic() - started < 2
    streamer.emit("applying", ["late"])  # after close: ignored, nothing raised
    assert client.chunks == []


def test_a_byte_that_is_not_utf8_does_not_end_the_command(tmp_path: Path) -> None:
    client = _Client()
    rc = run_command(
        ["sh", "-c", "printf 'caf\\351\\n'; echo after"],
        tmp_path,
        {"PATH": "/usr/bin:/bin"},
        phase="planning",
        section=None,
        streamer=_streamer(client),
    )
    assert rc == 0
    assert client.lines() == ["caf�", "after"]
    out = runner.LOCAL.capture(
        ["sh", "-c", "printf 'caf\\351'"], tmp_path, {"PATH": "/usr/bin:/bin"}
    )
    assert out.returncode == 0 and out.stdout == "caf�"
