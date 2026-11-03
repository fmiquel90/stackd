"""Tool versions (SPECS §7.4): what the worker declares it can run, and the check before a job."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent.config import Settings
from agent.tools import ToolUnavailable, parse_runner_images, pick, probe_local

DIGEST = "sha256:" + "a" * 64


def test_pick_exact_and_series() -> None:
    tools = {"opentofu": ["1.11.4", "1.12.0", "1.12.2", "1.12.3-rc1"]}
    assert pick(tools, "opentofu", "1.12.0") == "1.12.0"
    assert pick(tools, "opentofu", "1.12") == "1.12.2"  # highest release of the series
    with pytest.raises(ToolUnavailable, match="tool_version_unavailable: opentofu 1.13"):
        pick(tools, "opentofu", "1.13")
    with pytest.raises(ToolUnavailable, match="it has: none"):
        pick(tools, "terraform", "1.9")


def test_runner_images_must_be_pinned_by_digest() -> None:
    ok = parse_runner_images(json.dumps({"opentofu:1.12.2": f"ghcr.io/x/runner@{DIGEST}"}))
    assert ok == {"opentofu:1.12.2": f"ghcr.io/x/runner@{DIGEST}"}
    assert parse_runner_images("") == {}
    for bad in (
        {"opentofu:1.12.2": "ghcr.io/x/runner:1.12.2"},  # a tag can be re-pointed
        {"pulumi:3": f"x@{DIGEST}"},
        {"opentofu": f"x@{DIGEST}"},
    ):
        with pytest.raises(ValueError, match="STACKD_RUNNER_IMAGES"):
            parse_runner_images(json.dumps(bad))


def test_docker_runner_declares_its_images(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STACKD_RUNNER", "docker")
    monkeypatch.setenv(
        "STACKD_RUNNER_IMAGES",
        json.dumps({"opentofu:1.12.2": f"r@{DIGEST}", "terraform:1.9.8": f"t@{DIGEST}"}),
    )
    assert Settings.from_env().available_tools() == {
        "opentofu": ["1.12.2"],
        "terraform": ["1.9.8"],
    }


def test_probe_local_reads_the_binaries_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "tofu"
    fake.write_text('#!/bin/sh\necho \'{"terraform_version": "1.12.2"}\'\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))  # no terraform here: skipped
    assert subprocess.run(["tofu", "version", "-json"], capture_output=True).returncode == 0
    assert probe_local() == {"opentofu": ["1.12.2"]}
