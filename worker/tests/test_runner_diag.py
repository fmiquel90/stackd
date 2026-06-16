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
