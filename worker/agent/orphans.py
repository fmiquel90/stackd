"""What a job leaves behind when its agent dies without cleaning up — OOM-killed, SIGKILLed, or
exiting at once on an abandoned drain (§7.1): job containers still running tofu with live
credentials, workspaces holding the checkout, the tfvars, a saved plan. The next start reaps them
(SPECS §7.4).

Ownership is a kernel lock, not a name: a running job holds an exclusive `flock` on its workspace
directory, which the kernel releases however the agent ends. A workspace whose lock can be taken
is stale; a job container is an orphan when the workspace it mounts is. Several agents sharing a
Docker host and a workspace root (`docker compose --scale`) never reap each other's live jobs, and
an agent whose name changed (a recreated container) still reaps what its predecessor left."""

from __future__ import annotations

import fcntl
import os
import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

# Labels on every job container (docker_runner): the run it belongs to, and the workspace it mounts.
JOB_LABEL = "io.stackd.job"
WORKSPACE_LABEL = "io.stackd.workspace"


class _Engine(Protocol):
    def containers(self, label: str) -> list[dict]: ...

    def remove(self, cid: str) -> None: ...


def _is_job_dir(path: Path) -> bool:
    """A workspace is named after its job id (a UUID): nothing else under the root is touched."""
    try:
        return str(uuid.UUID(path.name)) == path.name and path.is_dir() and not path.is_symlink()
    except ValueError:
        return False


@contextmanager
def _try_lock(path: Path) -> Iterator[bool]:
    """Yields True while holding `path`'s lock (the directory is free: stale, or gone), False when
    a live job holds it."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        yield True
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


@contextmanager
def hold_workspace(root: str, job_id: str) -> Iterator[Path]:
    """Create the job's workspace directory and hold its lock while the job runs; remove it after
    (the job's own cleanup has normally done so already)."""
    path = Path(root) / job_id
    while True:
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            same = os.stat(path, follow_symlinks=False).st_ino == os.fstat(fd).st_ino
        except FileNotFoundError:
            same = False
        if same:
            break
        os.close(fd)  # removed (reaped, or a previous job's cleanup) while we waited: lock anew
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
        os.close(fd)


def reap_containers(engine: _Engine, root: str) -> int:
    """Kill and remove the job containers whose workspace, under `root`, no live job holds."""
    base = Path(root).resolve()
    reaped = 0
    for container in engine.containers(JOB_LABEL):
        workspace = (container.get("Labels") or {}).get(WORKSPACE_LABEL)
        if not workspace or Path(workspace).parent != base:
            continue  # another agent's root on this Docker host, or no workspace to tell by
        with _try_lock(Path(workspace)) as free:
            if free:
                engine.remove(str(container["Id"]))  # forced: killed first if running
                reaped += 1
    return reaped


def reap_workspaces(root: str) -> int:
    """Remove the workspaces under `root` that no live job holds."""
    base = Path(root)
    if not base.is_dir():
        return 0
    reaped = 0
    for path in base.iterdir():
        if not _is_job_dir(path):
            continue
        with _try_lock(path) as free:
            if free:
                shutil.rmtree(path, ignore_errors=True)
                reaped += 1
    return reaped
