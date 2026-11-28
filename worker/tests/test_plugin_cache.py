"""The worker's provider plugin cache (`STACKD_PLUGIN_CACHE_DIR`, SPECS §7.4): `init` installs into
it, one `init` at a time on the host, and never on the cache's word alone; the other commands read
it (the `docker` runner's mounts are in test_docker_runner)."""

from __future__ import annotations

import fcntl
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import agent.main as main
from agent import cancel
from agent.docker_runner import DockerLimits
from agent.runner import LOCAL
from agent.workspace import Workspace


class _Streamer:
    def emit(self, *args: Any, **kw: Any) -> None:
        pass


def _init(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tf_env: dict[str, str]) -> dict:
    """Run `_init` with tofu faked; returns what the fake saw: its env, its runner, and whether the
    cache's lock was held while it ran."""
    seen: dict[str, Any] = {}

    def fake_run_command(cmd: list[str], cwd: Path, env: dict, **kw: Any) -> int:
        seen.update(cmd=cmd, env=env, runner=kw["runner"])
        lock = tmp_path / "cache" / "init.lock"
        if lock.exists():
            with open(lock) as f:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    seen["locked"] = False
                except BlockingIOError:
                    seen["locked"] = True
        return 0

    monkeypatch.setattr(main, "run_command", fake_run_command)
    ws = Workspace(str(tmp_path / "ws"), "job")
    job = {"job_id": "job", "environment": {"repo_url": "file:///repo"}}
    rc = main._init(
        ws, ws.path, job, "tofu", tf_env, phase="planning", streamer=_Streamer(), runner=LOCAL
    )  # type: ignore[arg-type]
    assert rc == 0
    return seen


def test_init_installs_into_the_cache_under_its_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STACKD_PLUGIN_CACHE_DIR", str(tmp_path / "cache"))
    # A run variable can't make tofu take a cached package on trust: the flag is dropped.
    seen = _init(tmp_path, monkeypatch, {"TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE": "1"})
    cache = (tmp_path / "cache" / "providers").resolve()
    assert cache.is_dir() and seen["env"]["TF_PLUGIN_CACHE_DIR"] == str(cache)
    assert "TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE" not in seen["env"]
    assert seen["locked"] is True
    assert seen["runner"] is LOCAL.for_init()


def test_no_cache_unless_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STACKD_PLUGIN_CACHE_DIR", raising=False)
    seen = _init(tmp_path, monkeypatch, {"TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE": "1"})
    assert "TF_PLUGIN_CACHE_DIR" not in seen["env"] and "locked" not in seen
    assert seen["env"]["TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE"] == "1"  # as before


def _hold(cache_root: Path) -> Any:
    cache_root.mkdir(parents=True, exist_ok=True)
    f = open(cache_root / "init.lock", "a")
    fcntl.flock(f, fcntl.LOCK_EX)
    return f


def test_a_second_init_waits_for_the_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "_CACHE_LOCK_POLL_SECONDS", 0.01)
    held = _hold(tmp_path / "cache")
    order: list[str] = []

    def release() -> None:
        order.append("released")
        held.close()

    threading.Timer(0.2, release).start()
    with main._plugin_cache_lock(tmp_path / "cache" / "providers"):
        order.append("acquired")
    assert order == ["released", "acquired"]


def test_a_stop_request_ends_the_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "_CACHE_LOCK_POLL_SECONDS", 0.01)
    held = _hold(tmp_path / "cache")
    token = cancel.CancelToken()
    cancel.bind(token)
    threading.Timer(0.1, token.request).start()
    try:
        with pytest.raises(cancel.JobCanceled):
            with main._plugin_cache_lock(tmp_path / "cache" / "providers"):
                pytest.fail("the lock is held elsewhere")
    finally:
        cancel.bind(None)
        held.close()


def test_the_docker_runner_gets_the_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STACKD_PLUGIN_CACHE_DIR", str(tmp_path / "cache"))
    made: dict[str, Any] = {}

    class Engine:
        def __init__(self, docker_host: str | None = None) -> None:
            pass

        def image_exists(self, ref: str) -> bool:
            return True

    monkeypatch.setattr(main, "DockerEngine", Engine)
    monkeypatch.setattr(main, "DockerRunner", lambda *a, **kw: made.update(kw))
    settings = SimpleNamespace(
        runner="docker",
        runner_images={"opentofu:1.12.2": "img@sha256:" + "0" * 64},
        available_tools=lambda: {"opentofu": ["1.12.2"]},
        docker_host=None,
        docker_limits=DockerLimits(),
    )
    job = {"job_id": "job", "environment": {"tool": "opentofu", "tool_version": "1.12.2"}}
    main._make_runner(settings, job, Workspace(str(tmp_path / "ws"), "job"))  # type: ignore[arg-type]
    assert made["plugin_cache"] == (tmp_path / "cache" / "providers").resolve()
