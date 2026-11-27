"""What an agent that died without cleaning up leaves behind (SPECS §7.4): its job containers and
workspaces are reaped at the next start — and only those. A live job, of this agent or of another
sharing the Docker host and the workspace root, holds its workspace's lock and is never touched."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import agent.main as main
from agent import orphans
from agent.docker_engine import DockerEngine, DockerError
from agent.docker_runner import DockerLimits, DockerRunner


def _job_dir(root: Path) -> Path:
    path = root / str(uuid.uuid4())
    (path / "repo").mkdir(parents=True)
    (path / "repo" / "stackd.auto.tfvars.json").write_text('{"secret": "x"}')
    return path


# --- workspaces ---------------------------------------------------------------------------------


def test_stale_workspaces_are_removed_live_ones_kept(tmp_path: Path) -> None:
    stale = _job_dir(tmp_path)
    kept_dirs = [tmp_path / ".errored-state", tmp_path / "plugin-cache"]  # not job workspaces
    for d in kept_dirs:
        d.mkdir()
    with orphans.hold_workspace(str(tmp_path), str(uuid.uuid4())) as live:
        (live / "repo").mkdir()
        assert orphans.reap_workspaces(str(tmp_path)) == 1
        assert not stale.exists()
        assert (live / "repo").is_dir()  # held by a running job
    assert not live.exists()  # removed once the job is over
    assert all(d.is_dir() for d in kept_dirs)


def test_a_killed_agents_workspace_is_reaped(tmp_path: Path) -> None:
    """SIGKILL: no cleanup ran, but the kernel released the lock."""
    job = str(uuid.uuid4())
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time; from agent import orphans\n"
            "with orphans.hold_workspace(sys.argv[1], sys.argv[2]):\n"
            "    print('held', flush=True); time.sleep(60)",
            str(tmp_path),
            job,
        ],
        stdout=subprocess.PIPE,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        assert orphans.reap_workspaces(str(tmp_path)) == 0  # another agent's live job
        assert (tmp_path / job).is_dir()
    finally:
        holder.send_signal(signal.SIGKILL)
        holder.wait()
    assert (tmp_path / job).is_dir()  # left behind
    assert orphans.reap_workspaces(str(tmp_path)) == 1
    assert not (tmp_path / job).exists()


def test_a_job_waits_for_its_workspace_lock(tmp_path: Path) -> None:
    """The apply job of a run claimed on this host while its plan job's cleanup still holds the
    same directory: it starts on a directory of its own, locked."""
    job = str(uuid.uuid4())
    first = orphans.hold_workspace(str(tmp_path), job)
    first.__enter__()
    entered = threading.Event()

    def second() -> None:
        with orphans.hold_workspace(str(tmp_path), job) as path:
            entered.set()
            assert path.is_dir()
            assert orphans.reap_workspaces(str(tmp_path)) == 0

    t = threading.Thread(target=second)
    t.start()
    time.sleep(0.2)
    assert not entered.is_set()
    first.__exit__(None, None, None)  # removes the directory, then unlocks
    t.join(5)
    assert entered.is_set()


# --- containers ---------------------------------------------------------------------------------


class _Engine:
    def __init__(self, containers: list[dict[str, Any]]) -> None:
        self._containers = containers
        self.removed: list[str] = []

    def containers(self, label: str) -> list[dict[str, Any]]:
        assert label == orphans.JOB_LABEL
        return self._containers

    def remove(self, cid: str) -> None:
        self.removed.append(cid)


def test_orphan_containers_are_removed_only_when_their_workspace_is_free(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    stale = _job_dir(root)
    gone = root / str(uuid.uuid4())  # its workspace already removed
    other_root = tmp_path / "other" / str(uuid.uuid4())

    def c(cid: str, workspace: Path | None) -> dict[str, Any]:
        labels = {orphans.JOB_LABEL: "run"}
        if workspace is not None:
            labels[orphans.WORKSPACE_LABEL] = str(workspace)
        return {"Id": cid, "Labels": labels}

    with orphans.hold_workspace(str(root), str(uuid.uuid4())) as live:
        engine = _Engine(
            [
                c("stale", stale),
                c("gone", gone),
                c("live", live),
                c("another-agents-root", other_root),
                c("unlabelled", None),
            ]
        )
        assert orphans.reap_containers(engine, str(root)) == 2
    assert engine.removed == ["stale", "gone"]


def test_job_containers_carry_their_workspace(tmp_path: Path) -> None:
    ws = tmp_path / str(uuid.uuid4())
    (ws / "repo").mkdir(parents=True)
    runner = DockerRunner(object(), "img@sha256:x", ws, "run-1", DockerLimits())  # type: ignore[arg-type]
    for r in (runner, runner.for_repo()):
        labels = r.spec(["true"], ws / "repo", {}, "1:1")["Labels"]
        assert labels[orphans.JOB_LABEL] == "run-1"
        assert labels[orphans.WORKSPACE_LABEL] == str(ws.resolve())


def test_engine_lists_containers_by_label() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[{"Id": "abc", "Labels": {}}])

    engine = DockerEngine()
    engine._http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://docker")
    assert engine.containers("io.stackd.job") == [{"Id": "abc", "Labels": {}}]
    assert seen[0].url.params["all"] == "1"
    assert seen[0].url.params["filters"] == '{"label": ["io.stackd.job"]}'


def test_the_agent_reaps_at_start(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stale = _job_dir(tmp_path)
    engine = _Engine([{"Id": "x", "Labels": {orphans.WORKSPACE_LABEL: str(stale.resolve())}}])
    monkeypatch.setattr(main, "DockerEngine", lambda host=None: engine)
    settings = SimpleNamespace(runner="docker", docker_host=None, workspace_root=str(tmp_path))
    main._reap_orphans(settings)  # type: ignore[arg-type]
    assert engine.removed == ["x"] and not stale.exists()

    class Down:
        def containers(self, label: str) -> list:
            raise DockerError("daemon unreachable")

    monkeypatch.setattr(main, "DockerEngine", lambda host=None: Down())
    main._reap_orphans(settings)  # type: ignore[arg-type]  # logged, never fatal


@pytest.mark.docker
def test_a_running_orphan_container_is_killed_and_removed(tmp_path: Path) -> None:
    """A real daemon: a job container left running by a dead agent goes; a live job's stays."""
    image = os.environ.get("STACKD_TEST_DOCKER_IMAGE", "alpine:3.20")
    engine = DockerEngine()
    try:
        engine.ping()
    except (httpx.HTTPError, OSError) as exc:
        pytest.skip(f"no Docker daemon: {exc}")
    if not engine.image_exists(image):
        pytest.skip(f"test image {image} not present (docker pull {image})")

    def start(ws: Path) -> str:
        runner = DockerRunner(engine, image, ws, ws.name, DockerLimits())
        spec = runner.spec(["sleep", "300"], ws, {}, f"{os.getuid()}:{os.getgid()}")
        cid = engine.create(f"stackd-test-{ws.name}", spec)
        engine.start(cid)
        return cid

    stale = _job_dir(tmp_path)
    orphan = start(stale)
    with orphans.hold_workspace(str(tmp_path), str(uuid.uuid4())) as live_ws:
        live = start(live_ws)
        try:
            assert orphans.reap_containers(engine, str(tmp_path)) == 1
            ids = {c["Id"] for c in engine.containers(orphans.JOB_LABEL)}
            assert orphan not in ids and live in ids
        finally:
            engine.remove(live)
            engine.remove(orphan)


def test_workspace_dirs_stay_private(tmp_path: Path) -> None:
    with orphans.hold_workspace(str(tmp_path), str(uuid.uuid4())) as path:
        assert os.stat(path).st_mode & 0o777 == 0o700
