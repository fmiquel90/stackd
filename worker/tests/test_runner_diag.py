from __future__ import annotations

import json
import os
from pathlib import Path

from agent.runner import stream_json_command


class _FakeStreamer:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def emit(self, phase: str, lines: list[str], section: str | None = None) -> None:
        self.lines.extend(lines)

    def flush(self, timeout: float | None = None) -> bool:
        return True


def test_diagnostic_expands_to_detail_and_location(tmp_path: Path) -> None:
    evt = {
        "@level": "error",
        "@message": "Error: Invalid value for input variable",
        "type": "diagnostic",
        "diagnostic": {
            "severity": "error",
            "summary": "Invalid value for input variable",
            "detail": "The region must be eu-west-1.\nGot: us-east-1.",
            "range": {"filename": "variables.tf", "start": {"line": 12}},
        },
    }
    streamer = _FakeStreamer()
    rc, events = stream_json_command(
        ["sh", "-c", f"printf '%s\\n' '{json.dumps(evt)}'"],
        tmp_path,
        {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        phase="planning",
        section=None,
        streamer=streamer,
    )
    assert rc == 0
    joined = "\n".join(streamer.lines)
    assert "Error: Invalid value for input variable" in joined  # the headline
    assert "on variables.tf, line 12:" in joined  # location
    assert "The region must be eu-west-1." in joined and "Got: us-east-1." in joined  # full detail
    assert any(e.get("diagnostic") for e in events)  # still collected as a structured event


def test_only_the_events_callers_read_are_kept(tmp_path: Path) -> None:
    """The counts (`change_summary`) and the errors: a big plan's per-resource events are streamed,
    not held in memory for the whole command."""
    planned = {"@level": "info", "@message": "a.x: Plan to create", "type": "planned_change"}
    warning = {"@level": "warn", "@message": "Warning: deprecated", "type": "diagnostic"}
    summary = {"@level": "info", "@message": "Plan: 1 to add", "type": "change_summary"}
    error = {"@level": "error", "@message": "Error: boom", "type": "diagnostic"}
    lines = [planned] * 500 + [warning, summary, error]
    (tmp_path / "events").write_text("".join(json.dumps(e) + "\n" for e in lines))
    streamer = _FakeStreamer()
    rc, events = stream_json_command(
        ["cat", "events"],
        tmp_path,
        {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        phase="planning",
        section=None,
        streamer=streamer,
    )
    assert rc == 0 and events == [summary, error]
    assert len(streamer.lines) == len(lines)  # every event still reaches the log
