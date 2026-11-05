"""The agent's log buffer (SPECS §5.1: 1 s / 32 KB): a command's output is sent once its oldest
unsent line has waited FLUSH_SECONDS or FLUSH_BYTES are waiting, and the rest when it exits — a quiet
command's last line reaches the viewer without waiting for the next one."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

import agent.runner as runner
from agent.masking import Masker
from agent.runner import LogStreamer, _LogBatch, run_command


class _Client:
    def __init__(self, fail: bool = False) -> None:
        self.chunks: list[tuple[float, str, int, list[str]]] = []
        self.fail = fail
        self.sent = threading.Event()

    def logs(self, job_id: str, phase: str, seq: int, lines: list[dict], **kw: Any) -> None:
        if self.fail:
            raise RuntimeError("API unreachable")
        self.chunks.append((time.monotonic(), phase, seq, [ln["msg"] for ln in lines]))
        self.sent.set()


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
    batch = _LogBatch(_streamer(client), "applying", "hook:x")
    batch.add(["one", "token s3cr3t"])
    assert client.sent.wait(2)  # the timer sent it: no add, no close
    assert client.chunks[0][3] == ["one", "token ***"]  # masked on the way
    batch.close()
    assert len(client.chunks) == 1  # nothing left, nothing re-sent


def test_close_sends_the_rest_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "FLUSH_SECONDS", 60.0)
    client = _Client()
    batch = _LogBatch(_streamer(client), "planning", None)
    batch.add(["x"])
    batch.add(["y", "z"])
    assert client.chunks == []
    batch.close()
    assert [c[3] for c in client.chunks] == [["x", "y", "z"]]


def test_timer_send_failure_reaches_the_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "FLUSH_SECONDS", 0.05)
    batch = _LogBatch(_streamer(_Client(fail=True)), "planning", None)
    batch.add(["x"])
    batch._timer.join(2)  # the timer stops on the failed send
    assert not batch._timer.is_alive()
    with pytest.raises(RuntimeError, match="API unreachable"):
        batch.add(["y"])
