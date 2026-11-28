"""Fixtures shared by the agent tests: a stand-in `ssh`, so a clone over SSH with a deploy key needs
no SSH server."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# `ssh [options] host command`: log the identity file git handed over (its mode and first line,
# while ssh runs), then run the remote command (`git-upload-pack '<path>'`) locally.
_FAKE_SSH = """#!/bin/sh
key=none
while [ $# -gt 0 ]; do
  case "$1" in
    -i) key="$2"; shift 2;;
    -o|-p|-l) shift 2;;
    -*) shift;;
    *) break;;
  esac
done
host="$1"; shift
mode=$(stat -c %a "$key" 2>/dev/null || stat -f %Lp "$key" 2>/dev/null || echo gone)
echo "host=$host key=$key mode=$mode first=$(head -n 1 "$key" 2>/dev/null)" >> "$FAKE_SSH_LOG"
exec sh -c "$*"
"""


@pytest.fixture
def fake_ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The stand-in `ssh`, first on PATH. Returns its log."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ssh").write_text(_FAKE_SSH)
    (bin_dir / "ssh").chmod(0o755)
    log = tmp_path / "ssh.log"
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))
    return log
