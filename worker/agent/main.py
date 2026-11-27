from __future__ import annotations

import fcntl
import json
import logging

import httpx
import os
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from agent import cancel
from agent.client import ApiClient
from agent.config import Settings
from agent.drain import APPLY_HOLD_MARGIN_SECONDS, EXIT_CODES, Drain, supervise
from agent.logging import get_logger, setup as setup_logging
from agent.masking import Masker, mask_plan
from agent.docker_engine import DockerEngine
from agent.docker_runner import RUNNER_UID, DockerRunner
from agent.registry_auth import credential_for, registry_host
from agent.tools import pick
from agent.runner import (
    LOCAL,
    Runner,
    _DEFAULT_TIMEOUT_SECONDS,
    LogStreamer,
    run_command,
    run_hooks,
    stream_json_command,
)
from agent.workspace import HCL_TFVARS, TFVARS_JSON, Workspace

log = get_logger()


def _safe_json(raw: str) -> dict:
    """Parse JSON from a tofu subcommand; tolerate empty/garbled output instead of crashing."""
    try:
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}


# The HTTP state backend's settings (§11.2): the state token rides in TF_HTTP_PASSWORD.
_STATE_BACKEND_ENV_PREFIX = "TF_HTTP_"
# All an untrusted repo hook gets of the worker's own environment (§8.3): what any shell command
# needs (plus LC_*), nothing else. A worker host can carry any secret in its env — the pool token,
# a cloud credential family (a mounted ~/.aws on the trusted-dev `local` runner exports AWS_*), a
# database URL — and none of it may reach a repo hook.
_REPO_ENV_ALLOWLIST = frozenset(
    {
        *("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TERM"),
        *("LANG", "LANGUAGE", "TZ"),
        *("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY"),
        *("http_proxy", "https_proxy", "no_proxy", "all_proxy"),
        *("SSL_CERT_FILE", "SSL_CERT_DIR"),  # the CA bundle a TLS-inspecting proxy needs
    }
)


# The agent's own settings (config.py, logging.py: the pool token, the API URL, …), and any other
# platform setting a worker host may carry (STACKD_ENCRYPTION_KEY, …): none of them is for tofu or
# a platform hook, and repo code runs inside tofu (`data "external"`, provisioners) and feeds the
# tools platform hooks run (their config files). No provider or tool reads this namespace.
_WORKER_ENV_PREFIX = "STACKD_"


# What a `docker` runner container gets of the worker's own environment (§7.4): the egress proxy
# settings and the locale — nothing else. Host paths (PATH, HOME, a CA bundle, TF_* caches) mean
# nothing in the image, and every other host variable stays on the host.
_DOCKER_ENV_ALLOWLIST = frozenset(
    {
        *("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY"),
        *("http_proxy", "https_proxy", "no_proxy", "all_proxy"),
        *("LANG", "LANGUAGE", "TZ"),
    }
)


def _docker_environ() -> dict[str, str]:
    return {
        k: v for k, v in os.environ.items() if k in _DOCKER_ENV_ALLOWLIST or k.startswith("LC_")
    }


def _plugin_cache(runner: str) -> Path | None:
    """The worker's provider plugin cache (`STACKD_PLUGIN_CACHE_DIR`, §7.4), opt-in: tofu's
    TF_PLUGIN_CACHE_DIR is its `providers/`, created on first use (and, under a root agent's
    `docker` runner, given to the runner images' user, who writes it). None when unset."""
    root = os.environ.get("STACKD_PLUGIN_CACHE_DIR")
    if not root:
        return None
    cache = Path(root).resolve() / "providers"
    cache.mkdir(parents=True, exist_ok=True)
    if runner == "docker" and os.getuid() == 0:
        os.chown(cache, RUNNER_UID, RUNNER_UID)
    return cache


# How often a job waiting for the plugin cache looks at its cancel token.
_CACHE_LOCK_POLL_SECONDS = 0.5


@contextmanager
def _plugin_cache_lock(cache: Path | None) -> Iterator[None]:
    """Held around each `init` that writes the plugin cache: Terraform documents its cache as not
    safe for concurrent installs, so the `init`s of this worker's jobs — and of any agent on the
    host sharing the directory (`flock`, next to the cache) — take turns. A stop request (§4.1)
    ends the wait."""
    if cache is None:
        yield
        return
    token = cancel.current()
    with open(cache.parent / "init.lock", "a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if token is not None and token.should_stop:
                    raise cancel.JobCanceled from None
                time.sleep(_CACHE_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _job_environ(runner: str = "local") -> dict[str, str]:
    """Base process env for tofu and platform hooks. `local`: the worker's, minus the agent's own
    settings (§7.4, §8.3) — the rest stays (PATH and locale, proxies and CA, TF_* plugin cache,
    provider settings) so no provider or check tool loses what it needs. `docker`: the proxies and
    the locale only — the container is not the worker's host."""
    if runner == "docker":
        return _docker_environ()
    return {k: v for k, v in os.environ.items() if not k.startswith(_WORKER_ENV_PREFIX)}


def _repo_environ(runner: str = "local") -> dict[str, str]:
    """Base process env for repo hooks: the allowlisted variables of the worker's own environment
    only (§8.3) — on the docker runner, the container's allowlist. The run's non-sensitive `env`
    is layered on top by the caller."""
    if runner == "docker":
        return _docker_environ()
    return {k: v for k, v in os.environ.items() if k in _REPO_ENV_ALLOWLIST or k.startswith("LC_")}


def _make_runner(settings: Settings, job: dict[str, Any], ws: Workspace) -> Runner:
    """The job's runner (§7.4): `local`, or a `docker` runner on the image pinned for the stack's
    tool version (pulled by digest when the daemon does not have it yet)."""
    if settings.runner != "docker":
        return LOCAL
    env_info = job["environment"]
    version = pick(settings.available_tools(), env_info["tool"], env_info["tool_version"])
    image = settings.runner_images[f"{env_info['tool']}:{version}"]
    engine = DockerEngine(settings.docker_host)
    if not engine.image_exists(image):
        # A private registry's credential (STACKD_REGISTRY_AUTH_FILE), for this pull only.
        credential = credential_for(image, settings.registry_auth_file)
        log.info(
            "pulling runner image",
            extra={
                "event": "agent.image_pull",
                "image": image,
                "registry": registry_host(image),
                "authenticated": credential is not None,
            },
        )
        engine.pull(image, credential)
    if os.getuid() == 0:
        # A root agent's files must not reach a container as root: the workspace goes to the
        # images' unprivileged user, the checkout once cloned, each file the agent writes after.
        ws.hand_over(RUNNER_UID, RUNNER_UID)
    return DockerRunner(
        engine,
        image,
        ws.path,
        job["job_id"],
        settings.docker_limits,
        plugin_cache=_plugin_cache("docker"),
    )


def _secret_leak_in_outputs(doc: str | dict[str, Any], masker: Masker) -> str | None:
    """Cleartext tripwire (§5.1): a value Stackd treats as sensitive appearing verbatim in a
    *non-sensitive* output. terraform only redacts outputs the repo marked `sensitive = true`, so a
    sensitive Stackd variable echoed by an unmarked output leaks. Scans `tofu show -json` (plan,
    parsed) or `tofu output -json` (apply). Returns a masked description, or None."""
    try:
        data = doc if isinstance(doc, dict) else json.loads(doc)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    # plan json nests outputs under planned_values.outputs; `output -json` is a flat name→obj map.
    outputs = (
        (data.get("planned_values") or {}).get("outputs") if "planned_values" in data else data
    )
    if not isinstance(outputs, dict):
        return None
    leaked = [
        name
        for name, meta in outputs.items()
        if isinstance(meta, dict)
        and not meta.get("sensitive")
        and masker.scan(json.dumps(meta.get("value")))
    ]
    if leaked:
        return f"sensitive value(s) appear in non-sensitive output(s): {', '.join(sorted(leaked))}"
    return None


def _repo_token(job: dict) -> str | None:
    """The HTTPS clone token (repo_auth_kind=token); None for `none`/`deploy_key`."""
    return (job.get("repo_credentials") or {}).get("token")


def _repo_ssh_key(job: dict) -> str | None:
    """The deploy key (repo_auth_kind=deploy_key); None for `none`/`token`."""
    return (job.get("repo_credentials") or {}).get("ssh_private_key")


def _masker(job: dict) -> Masker:
    """The job's log masker (§5.1): the API's `mask_values`, plus the repo token percent-encoded —
    the form it takes inside a URL, which git can echo in a clone or module-download error."""
    token = _repo_token(job)
    extra = [quote(token, safe="")] if token else []
    return Masker([*job.get("mask_values", []), *extra])


def _clone(ws: Workspace, job: dict, commit_sha: str | None) -> Path:
    env_info = job["environment"]
    return ws.git_clone(
        env_info["repo_url"],
        commit_sha,
        env_info["project_root"],
        token=_repo_token(job),
        branch=env_info.get("branch"),
        ssh_key=_repo_ssh_key(job),
    )


def _merge_hooks(platform: dict, repo: dict) -> dict[str, list[dict]]:
    """Platform hooks first (non-bypassable), then repo hooks, per stage (§8.1)."""
    merged: dict[str, list[dict]] = {}
    for stage in set(platform) | set(repo):
        merged[stage] = list(platform.get(stage, [])) + list(repo.get(stage, []))
    return merged


_APPLY_STAGES = ("before_apply", "after_apply")


def _apply_repo_hook_policy(
    hooks: dict[str, list[dict]],
    job: dict[str, Any],
    checks: list[dict[str, Any]],
    streamer: LogStreamer,
) -> dict[str, list[dict]]:
    """Repo hooks at the apply stages run only on a tier that allows them (`repo_hooks_at_apply`,
    §8.3): they would run next to the apply role's credentials. Otherwise each is skipped with a
    visible warning — a `warn` check result and a log line — and the platform hooks still run.
    An absent flag (an older API) counts as not allowed: fail closed."""
    if job.get("repo_hooks_at_apply") is True:
        return hooks
    out = dict(hooks)
    for stage in _APPLY_STAGES:
        kept = [h for h in hooks.get(stage, []) if h.get("source") != "repo"]
        for h in hooks.get(stage, []):
            if h.get("source") == "repo":
                detail = "skipped: this tier does not allow repo hooks at the apply stages"
                checks.append(
                    {"name": h["name"], "status": "warn", "detail": detail, "stage": stage}
                )
                streamer.emit(
                    "applying",
                    [f"[stackd] repo hook {h['name']} {detail}"],
                    section=f"hook:{h['name']}",
                )
        out[stage] = kept
    return out


# Counters every plan summary carries (SPECS §3.5): a rollout's gate fails closed without them.
_SUMMARY_KEYS = ("add", "change", "destroy", "import", "forget")


def _summary_from_events(events: list[dict]) -> dict:
    """Plan/apply counts straight from tofu's authoritative `change_summary` event (last wins):
    every counter it reports — `remove` as `destroy`; `import`; OpenTofu's `forget`; Terraform's
    `action_invocation`/`action_fail`; any a newer tool adds — so a rollout's gate (SPECS §21.5)
    sees every kind of action, not only add/change/destroy."""
    out: dict[str, int] = dict.fromkeys(_SUMMARY_KEYS, 0)
    for evt in reversed(events):
        if evt.get("type") == "change_summary":
            for key, value in (evt.get("changes") or {}).items():
                if isinstance(value, int) and not isinstance(value, bool):
                    out["destroy" if key == "remove" else key] = value
            break
    return out


def _forgets_in_plan(plan: dict[str, Any]) -> int:
    """Resources the plan forgets (a `removed` block with `destroy = false`), counted from the plan
    JSON: Terraform's `change_summary` has no `forget` counter, OpenTofu's does."""
    changes = plan.get("resource_changes") or []
    return sum(
        1
        for rc in changes
        if isinstance(rc, dict) and "forget" in ((rc.get("change") or {}).get("actions") or [])
    )


def _first_error(events: list[dict]) -> str | None:
    """The first error diagnostic's summary — surfaced as the run's error (real tofu message)."""
    for evt in events:
        if evt.get("@level") == "error":
            diag = evt.get("diagnostic") or {}
            return diag.get("summary") or evt.get("@message")
    return None


def _exit_error(what: str, code: int, budget: int, events: list[dict[str, Any]]) -> str:
    """Why the tofu command `what` failed with exit `code`, for the run's error: tofu's own error
    when it reported one, else the watchdog's kill (124), a signal, or the bare code."""
    if code == 124:
        return f"{what} exceeded the {budget}s time budget and was terminated"
    if code < 0:
        try:
            killer = signal.Signals(-code).name
        except ValueError:
            killer = f"signal {-code}"
        return f"{what} was killed by {killer}"
    return _first_error(events) or (
        f"{what} failed" if code == 1 else f"{what} failed (exit {code})"
    )


# tofu refuses a saved plan before changing anything when the state changed since it was made, or
# when the run's inputs no longer match the values it was planned with. Its summary alone is terse.
_SAVED_PLAN_REFUSALS = ("Saved plan is stale", "Mismatch between input and plan variable value")


def _apply_error(events: list[dict]) -> str | None:
    """`_first_error`, spelling out a saved-plan refusal: tofu's detail, and what to do."""
    error = _first_error(events)
    if error not in _SAVED_PLAN_REFUSALS:
        return error
    diag = next(e.get("diagnostic") or {} for e in events if e.get("@level") == "error")
    detail = str(diag.get("detail") or "").strip()
    return f"{error}: {detail} The reviewed plan was not applied; trigger a new plan."


def _plan_json(
    show: subprocess.CompletedProcess[str],
) -> tuple[dict[str, Any], None] | tuple[None, str]:
    """`tofu show -json`'s plan document, parsed once for every reader, or why its result is none
    (a failed run, or anything but a JSON object)."""
    if show.returncode != 0:
        stderr = (show.stderr or "").splitlines()
        detail = next((line.strip() for line in stderr if line.strip()), "")
        return None, f"exited {show.returncode}" + (f": {detail}" if detail else "")
    try:
        doc = json.loads(show.stdout)
    except ValueError:
        return None, "printed no JSON"
    return (doc, None) if isinstance(doc, dict) else (None, "printed no plan object")


# `state pull` reads the whole state: a short budget of its own, so a hung backend can't hold a job.
_STATE_PULL_TIMEOUT_SECONDS = 120


class _NoStateIdentity(ValueError):
    """`state pull` printed something that is not a state document. Never carries its content."""


def _state_identity(raw: str) -> tuple[int | None, str | None]:
    """The `serial` and `lineage` of a `state pull` document (§19.7) — nothing else of it is kept:
    the state holds secrets. An empty output is an empty state (none yet): (None, None)."""
    if not raw.strip():
        return None, None
    try:
        doc = json.loads(raw)
    except ValueError:
        raise _NoStateIdentity("not JSON") from None
    serial = doc.get("serial") if isinstance(doc, dict) else None
    lineage = doc.get("lineage") if isinstance(doc, dict) else None
    if isinstance(serial, bool) or not isinstance(serial, int) or serial < 0:
        raise _NoStateIdentity("no serial")
    if not isinstance(lineage, str) or not lineage:
        raise _NoStateIdentity("no lineage")
    return serial, lineage


def _observe_state(
    client: ApiClient,
    job: dict[str, Any],
    *,
    ws: Workspace,
    cwd: Path,
    tool: str,
    tf_env: dict[str, str],
    runner: Runner,
    streamer: LogStreamer,
    phase: str,
    baseline: bool,
) -> None:
    """Report the state's serial/lineage (`state_observed`, §19.7) when the API asked for it
    (`observe_state`): a check (`baseline=False`, the state as the job finds it) or a baseline (the
    state as its apply / command left it). `state pull`'s output stays in memory — never on disk,
    never logged — and only those two fields leave this function. Best effort: any failure is a
    warning, never the job's end."""
    if not job.get("observe_state"):
        return
    job_id = job["job_id"]
    try:
        with _tofu_files(ws, cwd, job):
            pulled = runner.capture(
                [tool, "state", "pull"], cwd, tf_env, timeout=_STATE_PULL_TIMEOUT_SECONDS
            )
        if pulled.returncode != 0:
            raise _NoStateIdentity(f"`{tool} state pull` exited {pulled.returncode}")
        serial, lineage = _state_identity(pulled.stdout)
        del pulled
        client.event(
            job_id,
            "state_observed",
            result={"serial": serial, "lineage": lineage, "baseline": baseline},
        )
    except Exception as exc:  # noqa: BLE001 — detection never fails the job
        # Our own reason, a timeout, or the API's refusal: never the tool's output (it can hold the
        # state), which none of these carries.
        known = (_NoStateIdentity, subprocess.TimeoutExpired, httpx.HTTPError)
        reason = Masker(job.get("mask_values", [])).mask(
            str(exc) if isinstance(exc, known) else type(exc).__name__
        )
        log.warning(
            "state observation failed",
            extra={"event": "agent.state_observe_error", "run_id": job_id, "reason": reason},
        )
        try:
            streamer.emit(
                phase,
                [f"[stackd] state serial not read ({reason}): out-of-band detection skipped"],
            )
        except Exception:  # noqa: BLE001
            pass


def _tool_bin(tool: str) -> str:
    return "tofu" if tool == "opentofu" else "terraform"


_OIDC_TOKEN_FILE = "oidc_token"
_AWS_CONFIG_FILE = "aws_config"
# The API's rule for a named profile (§10.6), checked again: the name becomes an INI section header.
_AWS_PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


# What the AWS SDK's credential chain reads before the web identity token (§10.5): static keys, a
# session token, a named profile and the files holding them, a container credentials endpoint.
_STATIC_AWS_CREDENTIAL_VARS = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AWS_CONFIG_FILE",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    }
)


def _run_env(ws: Workspace, job: dict[str, Any], runner: str = "local") -> dict[str, str]:
    """`_job_environ`, then the run's env, its sensitive env and the OIDC workload env (§10.4):
    what platform hooks run with, and tofu with the state backend's settings on top. With OIDC
    credentials, the worker host's own AWS credentials (a mounted ~/.aws on the trusted-dev `local`
    runner) are dropped: the role the integration names is the job's only identity (§10.5). The
    API drops the run's static AWS credentials at claim for the same reason."""
    env = {
        **_job_environ(runner),
        **job.get("env", {}),
        **job.get("sensitive_env", {}),
    }
    if job.get("cloud_credentials"):
        # The run's own layers too, in depth: the API already dropped them at claim.
        env = {k: v for k, v in env.items() if k not in _STATIC_AWS_CREDENTIAL_VARS}
    return {**env, **_cloud_env(ws, job)}


def _cloud_env(ws: Workspace, job: dict) -> dict[str, str]:
    """OIDC workload env for terraform and platform hooks only (§10.4 / §8.3 — never for repo
    hooks). The token and config files it names are on disk only while one of them runs
    (`_cloud_files`). No AWS_PROFILE: a named profile is used where the code names one (§10.6);
    the worker host's credentials file is masked, so it can't define a profile's credentials."""
    creds = job.get("cloud_credentials")
    if not creds:
        return {}
    _aws_config(ws, job)  # a payload the file can't be written from fails the job up front
    env = {
        "AWS_WEB_IDENTITY_TOKEN_FILE": str(ws.secret_path(_OIDC_TOKEN_FILE)),
        "AWS_ROLE_ARN": creds["role_arn"],
        "AWS_ROLE_SESSION_NAME": f"stackd-{job['job_id']}",
        "AWS_CONFIG_FILE": str(ws.secret_path(_AWS_CONFIG_FILE)),
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
    }
    if creds.get("region"):
        env["AWS_REGION"] = creds["region"]
    return env


# The managed HTTP backend's settings (§11.2) as tofu/terraform read them from the environment.
_BACKEND_ENV = {
    "address": "TF_HTTP_ADDRESS",
    "lock_address": "TF_HTTP_LOCK_ADDRESS",
    "unlock_address": "TF_HTTP_UNLOCK_ADDRESS",
    "lock_method": "TF_HTTP_LOCK_METHOD",
    "unlock_method": "TF_HTTP_UNLOCK_METHOD",
    "username": "TF_HTTP_USERNAME",
    "password": "TF_HTTP_PASSWORD",
}


def _tofu_env(base: dict[str, str], backend: dict | None) -> dict[str, str]:
    """Env for tofu/terraform subprocesses only — never for hooks (§8.3). Managed state passes the
    platform's HTTP backend through TF_HTTP_*, not `-backend-config`: tofu persists -backend-config
    values (the state token included) in .terraform/terraform.tfstate, where repo hooks can read
    them, and in the saved plan. Any inherited TF_HTTP_* is dropped first so neither the worker's
    env nor a run variable can redirect or weaken the managed backend. Unmanaged: unchanged."""
    if not backend:
        return base
    env = {k: v for k, v in base.items() if not k.startswith(_STATE_BACKEND_ENV_PREFIX)}
    env.update(
        {var: str(backend[k]) for k, var in _BACKEND_ENV.items() if backend.get(k) is not None}
    )
    return env


def _aws_config(ws: Workspace, job: dict[str, Any]) -> str:
    """The AWS config file of a job holding OIDC credentials (§10.6): `[default]` mirrors the
    default role, then one `[profile <name>]` per named profile with the role of the job's
    credential phase, every one assumed with the job's token. Built from the payload only; a value
    that could break out of its line or section fails the job instead of being written."""
    creds = job["cloud_credentials"]
    common = {
        "web_identity_token_file": str(ws.secret_path(_OIDC_TOKEN_FILE)),
        "role_session_name": f"stackd-{job['job_id']}",
    }
    default = {"role_arn": creds["role_arn"], **common, "region": creds.get("region")}
    sections = [("default", default)]
    for name, profile in sorted((creds.get("profiles") or {}).items()):
        if name == "default" or not _AWS_PROFILE_NAME.match(name):
            raise RuntimeError(f"invalid AWS profile name: {name!r}")
        values = {"role_arn": profile["role_arn"], **common, "region": profile.get("region")}
        sections.append((f"profile {name}", values))
    lines = []
    for header, values in sections:
        lines.append(f"[{header}]")
        for key, value in values.items():
            if value is None:
                continue
            if not isinstance(value, str) or any(c in value for c in "\r\n"):
                raise RuntimeError(f"invalid AWS config value for {key} in [{header}]")
            lines.append(f"{key} = {value}")
        lines.append("")
    return "\n".join(lines)


@contextmanager
def _cloud_files(ws: Workspace, job: dict[str, Any]) -> Iterator[None]:
    """The OIDC token file and the AWS config file `_cloud_env` points at, written for one tofu
    command or platform hook and removed right after: never on disk while a repo hook runs
    (§8.3)."""
    creds = job.get("cloud_credentials") or {}
    with ExitStack() as files:
        if creds.get("oidc_token"):
            files.enter_context(ws.secret_file(_OIDC_TOKEN_FILE, creds["oidc_token"]))
            files.enter_context(ws.secret_file(_AWS_CONFIG_FILE, _aws_config(ws, job)))
        yield


@contextmanager
def _tofu_files(
    ws: Workspace, cwd: Path, job: dict[str, Any], *, tfvars: bool = True
) -> Iterator[None]:
    """What one tofu command reads from disk that no repo hook may find (§8.3), present only while
    it runs: the OIDC token and AWS config files, and the run's variable files unless the command
    reads none."""
    with ExitStack() as files:
        files.enter_context(_cloud_files(ws, job))
        if tfvars:
            files.enter_context(
                ws.tfvars(cwd, job.get("tfvars_json", {}), job.get("hcl_tfvars", {}))
            )
        yield


def _init_cmd(
    tool: str,
    backend: dict | None,
    backend_config_file: str | None = None,
    backend_config: dict | None = None,
) -> list[str]:
    cmd = [tool, "init", "-input=false"]
    if backend:  # managed state: the platform's HTTP backend, configured via TF_HTTP_* (_tofu_env)
        return cmd
    # unmanaged: the repo's own partial backend — a .config file and/or inline key=value
    if backend_config_file:
        cmd.append(f"-backend-config={backend_config_file}")
    for k, v in (backend_config or {}).items():
        cmd.append(f"-backend-config={k}={v}")
    return cmd


def _var_file_flags(ws: Workspace, cwd: Path, job: dict[str, Any]) -> list[str]:
    """`-var-file=` flags for a command that reads variables (§3.4): the env's repo var files in
    their order, then — only when there are some — Stackd's own two files again, explicitly: a
    `-var-file` beats any auto-loaded file, so without them a repo file would override the run's
    resolved variables (dependency inputs and mocks included). Each repo file must be in the
    checkout: a missing one fails the job here, naming it, rather than in tofu's own words."""
    files = [f for f in job.get("var_files") or [] if isinstance(f, str)]
    if not files:
        return []
    problem = ws.var_file_problem(cwd, files)
    if problem:
        env_info = job["environment"]
        raise RuntimeError(
            f"{problem} (environment {env_info.get('name')!r}, relative to the project root "
            f"{env_info.get('project_root')!r})"
        )
    own = [TFVARS_JSON, *([HCL_TFVARS] if job.get("hcl_tfvars") else [])]
    return [f"-var-file={f}" for f in (*files, *own)]


def _init(
    ws: Workspace,
    cwd: Path,
    job: dict[str, Any],
    tool: str,
    tf_env: dict[str, str],
    *,
    phase: str,
    streamer: LogStreamer,
    runner: Runner,
) -> int:
    """`init`, the one tofu command that holds the stack's repo credential (§8.3): it downloads the
    modules, private ones hosted next to the repo included. On disk / in the env for it only. It
    is also the one that installs providers: into the worker's plugin cache, when there is one."""
    backend = job.get("backend")
    creds = ws.repo_credentials(
        job["environment"]["repo_url"], token=_repo_token(job), ssh_key=_repo_ssh_key(job)
    )
    cache = _plugin_cache(runner.name)
    env = dict(tf_env)
    if cache is not None:
        env["TF_PLUGIN_CACHE_DIR"] = str(cache)
        # The cache is shared by the worker's jobs: without this flag tofu uses a cached package
        # only when the job's own lock file records its checksum (else it installs from the
        # registry, verified) — a package another job put there is never taken on trust (§7.4).
        env.pop("TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE", None)
    with _plugin_cache_lock(cache), _tofu_files(ws, cwd, job), creds as git_env:
        return run_command(
            _init_cmd(tool, backend, job.get("backend_config_file"), job.get("backend_config")),
            cwd,
            {**env, **git_env},
            phase=phase,
            section=None,
            streamer=streamer,
            runner=runner.for_init(),
        )


def _job_failed(
    client: ApiClient,
    job_id: str,
    phase: str | None,
    checks: list[dict[str, Any]],
    **result: Any,
) -> None:
    """Report the job failed at `phase` with every check result it has so far (§8.3), whatever
    ended it — a failing check, a tofu error, a timeout, the plan JSON: the API records them, and
    audits a warning among them, as it does a finished job's."""
    client.event(job_id, "job_failed", phase=phase, result={**result, "checks": checks})


def _plan_option_flags(opts: dict | None) -> list[str]:
    """`-target=`/`-replace=` flags for a plan job's `plan_options` (§7.2), addresses as the API
    validated them. Each is one argv element (no shell), never a bare option."""
    flags: list[str] = []
    for key, flag in (("targets", "-target"), ("replace", "-replace")):
        for addr in (opts or {}).get(key) or []:
            if isinstance(addr, str) and addr and not addr.startswith("-"):
                flags.append(f"{flag}={addr}")
    return flags


def handle_plan(
    client: ApiClient, job: dict, settings: Settings, checks: list[dict[str, Any]] | None = None
) -> None:
    """The plan job (§7.4). `checks` collects its check results, for the caller to report too
    should the job end on an uncaught error."""
    checks = [] if checks is None else checks
    job_id = job["job_id"]
    env_info = job["environment"]
    masker = _masker(job)
    streamer = LogStreamer(client, job_id, masker)
    ws = Workspace(settings.workspace_root, job_id)
    tool = _tool_bin(env_info["tool"])

    try:
        runner = _make_runner(settings, job, ws)
        cwd = _clone(ws, job, env_info.get("commit_sha"))
        # The env's repo var files are the commit's: checked now, before any hook or tofu step.
        _var_file_flags(ws, cwd, job)
        backend = job.get("backend")
        if backend:
            ws.write_backend_override(cwd)
        hooks = _merge_hooks(job.get("hooks", {}), ws.load_stackd_yml(cwd))

        platform_env = _run_env(ws, job, runner.name)
        repo_env = {
            **_repo_environ(runner.name),
            **job.get("env", {}),
        }  # no secrets / cloud creds to repo hooks (§8.3)
        tf_env = _tofu_env(platform_env, backend)  # + state-backend creds, for tofu only

        def observe(*, baseline: bool) -> None:
            _observe_state(
                client,
                job,
                ws=ws,
                cwd=cwd,
                tool=tool,
                tf_env=tf_env,
                runner=runner,
                streamer=streamer,
                phase="planning",
                baseline=baseline,
            )

        # Report the exact commit checked out: the API pins the run to it (or refuses a mismatch
        # with the commit it pinned), and the apply job clones exactly that SHA (§7.2/§7.3).
        client.event(
            job_id, "phase_started", phase="planning", result={"commit_sha": ws.head_sha()}
        )

        def stage(name: str) -> bool:
            """A stage's hooks (§8.3): True when an `on_failure: fail` hook failed — the job fails
            at that stage, before the next tofu step. Every result (ok, warn, fail) is reported in
            the checks, with its stage."""
            results, aborted = run_hooks(
                hooks.get(name, []),
                cwd,
                platform_env=platform_env,
                repo_env=repo_env,
                phase="planning",
                streamer=streamer,
                platform_files=lambda: _cloud_files(ws, job),
                runner=runner,
            )
            checks.extend({**r, "stage": name} for r in results)
            return aborted

        if stage("before_init"):
            return _job_failed(client, job_id, "before_init", checks)
        init_rc = _init(
            ws, cwd, job, tool, tf_env, phase="planning", streamer=streamer, runner=runner
        )
        if init_rc != 0:
            return _job_failed(client, job_id, "init", checks, error="init failed")
        # The state as this job finds it, before the plan (§19.7): a write made outside Stackd since
        # its last observation shows here.
        observe(baseline=False)
        for name in ("after_init", "before_plan"):
            if stage(name):
                return _job_failed(client, job_id, name, checks)

        # `-json`: stream readable @message lines while collecting structured events (change_summary
        # for the counts, diagnostics for the real error message). detailed-exitcode: 0=no changes,
        # 2=changes, 1=error. `-refresh-only` (drift, §19): diff state against real infra only.
        # `-destroy` (a destroy run): the saved plan carries it to the apply. The plan file holds
        # the run's variable values in clear: it is made outside the checkout (§8.3).
        plan_file = ws.private_path("plan.tfplan")
        plan_cmd = [
            tool,
            "plan",
            "-input=false",
            "-json",
            "-detailed-exitcode",
            f"-out={plan_file}",
        ]
        if job.get("refresh_only"):
            plan_cmd.insert(2, "-refresh-only")
        if job.get("destroy"):
            plan_cmd.insert(2, "-destroy")
        # A targeted / replace plan (§3.5): the API validated the addresses; they reach the plan
        # only — the saved plan carries them to the apply, which never re-plans. Same for the
        # env's var files (§3.4), re-checked now that the hooks have run.
        plan_cmd[2:2] = [
            *_var_file_flags(ws, cwd, job),
            *_plan_option_flags(job.get("plan_options")),
        ]
        with _tofu_files(ws, cwd, job):
            code, events = stream_json_command(
                plan_cmd,
                cwd,
                tf_env,
                phase="planning",
                section=None,
                streamer=streamer,
                runner=runner,
            )
        if code not in (0, 2):
            # Only 0 and 2 are a plan: anything else — an error, the watchdog's kill (124), a
            # signal — fails the job closed, never passes for "no changes".
            error = _exit_error("plan", code, _DEFAULT_TIMEOUT_SECONDS, events)
            # Mask: a tofu diagnostic can echo a sensitive variable value verbatim (§5.1).
            return _job_failed(client, job_id, "plan", checks, error=masker.mask(error))
        has_changes = code == 2
        summary = _summary_from_events(events)

        # The -json events streamed above are terse ("Plan to create"); also stream the human-readable
        # diff operators expect (the real `terraform plan` output), read from the saved plan file
        # (fast — no refresh, no API calls). `-no-color` so the ANSI doesn't fight the log viewer.
        # Then plan.json for after_plan hooks (infracost/jq): `show -json` is machine-readable.
        with _tofu_files(ws, cwd, job, tfvars=False):
            run_command(
                [tool, "show", "-no-color", str(plan_file)],
                cwd,
                tf_env,
                phase="planning",
                section="plan",
                streamer=streamer,
                runner=runner,
            )
            show = runner.capture([tool, "show", "-json", "-no-color", str(plan_file)], cwd, tf_env)
        # The after_plan checks and the reviewers read this document: without it, fail closed — a
        # stand-in `{}` would let every policy pass on an empty plan.
        plan, unusable = _plan_json(show)
        if plan is None:
            error = f"plan JSON unavailable, not checked: `{tool} show -json` {unusable}"
            return _job_failed(client, job_id, "plan", checks, error=masker.mask(error))
        plan_doc = show.stdout
        summary["forget"] = max(summary.get("forget", 0), _forgets_in_plan(plan))
        # Cleartext tripwire (§5.1): a sensitive value surfacing in a non-sensitive output means
        # the repo didn't mark it `sensitive` — flag (or fail) the run, below. Read before the
        # masking, which redacts the parsed document in place.
        leak = _secret_leak_in_outputs(plan, masker)
        # It lists the run's variable values in clear (§8.3): the artifact, and what repo hooks
        # read, is a copy with every sensitive value masked.
        masked_doc = mask_plan(plan, masker)
        del plan
        client.upload_artifact(job_id, "plan.json", masked_doc.encode())
        # The saved plan, read once now (what was shown above) and removed before any hook runs.
        saved_plan = plan_file.read_bytes() if has_changes and job.get("save_plan") else b""
        plan_file.unlink(missing_ok=True)

        if leak:
            if settings.leak_action == "fail":
                return _job_failed(
                    client, job_id, "plan", checks, error=f"secret_leak_suspected: {leak}"
                )
            checks.append(
                {"name": "secret_leak_suspected", "status": "warn", "detail": leak, "stage": "plan"}
            )

        # The saved plan (§7.4): the apply job applies exactly this file, the one shown above.
        # Uploaded before any after_plan hook runs, so a repo hook can't swap what gets applied.
        # A run that can't be applied (proposed, drift) or has nothing to apply saves none.
        if saved_plan:
            client.upload_artifact(job_id, "plan.tfplan", saved_plan)

        if job.get("hooks", {}).get("after_plan") or ws.load_stackd_yml(cwd).get("after_plan"):
            client.event(job_id, "phase_started", phase="checking")
        # Platform hooks (trusted with the run's secrets, §8.3) read tofu's plan JSON as is; it is
        # replaced by the masked copy before the first repo hook runs.
        after_plan = hooks.get("after_plan", [])
        aborted = False
        for doc, batch in (
            (plan_doc, [h for h in after_plan if h.get("source") != "repo"]),
            (masked_doc, [h for h in after_plan if h.get("source") == "repo"]),
        ):
            if not batch or aborted:
                continue
            ws.write_plan_json(cwd, doc)
            results, aborted = run_hooks(
                batch,
                cwd,
                platform_env=platform_env,
                repo_env=repo_env,
                phase="checking",
                streamer=streamer,
                platform_files=lambda: _cloud_files(ws, job),
                runner=runner,
            )
            checks.extend({**r, "stage": "after_plan"} for r in results)
        if aborted:
            return _job_failed(client, job_id, "after_plan", checks)

        client.event(
            job_id,
            "phase_finished",
            result={
                "has_changes": has_changes,
                "summary": summary,
                "checks": checks,
            },
        )
    finally:
        ws.cleanup()


def handle_apply(
    client: ApiClient, job: dict, settings: Settings, checks: list[dict[str, Any]] | None = None
) -> None:
    """The apply job (§7.3/§7.4). `checks` as in `handle_plan`."""
    checks = [] if checks is None else checks
    job_id = job["job_id"]
    env_info = job["environment"]
    masker = _masker(job)
    streamer = LogStreamer(client, job_id, masker)
    ws = Workspace(settings.workspace_root, job_id)
    tool = _tool_bin(env_info["tool"])

    try:
        runner = _make_runner(settings, job, ws)
        # The apply runs on exactly the commit the plan was made on — pinned by the API from the
        # plan job's report (§7.2) — never on wherever the branch head has moved since.
        commit_sha = env_info.get("commit_sha")
        if not commit_sha:
            error = "apply refused: the run has no pinned commit; trigger a new plan"
            return _job_failed(client, job_id, "apply", checks, error=error)
        cwd = _clone(ws, job, commit_sha)
        backend = job.get("backend")
        if backend:
            ws.write_backend_override(cwd)
        hooks = _merge_hooks(job.get("hooks", {}), ws.load_stackd_yml(cwd))
        hooks = _apply_repo_hook_policy(hooks, job, checks, streamer)
        platform_env = _run_env(ws, job, runner.name)
        repo_env = {**_repo_environ(runner.name), **job.get("env", {})}
        tf_env = _tofu_env(platform_env, backend)  # + state-backend creds, for tofu only

        def observe(*, baseline: bool) -> None:
            _observe_state(
                client,
                job,
                ws=ws,
                cwd=cwd,
                tool=tool,
                tf_env=tf_env,
                runner=runner,
                streamer=streamer,
                phase="applying",
                baseline=baseline,
            )

        def stage(name: str) -> bool:
            """As in the plan job (§8.3): True when an `on_failure: fail` hook failed. Every result is
            reported in the checks, with its stage; a `warn` one can't send the run, already
            confirmed, back to `unconfirmed`."""
            results, aborted = run_hooks(
                hooks.get(name, []),
                cwd,
                platform_env=platform_env,
                repo_env=repo_env,
                phase="applying",
                streamer=streamer,
                platform_files=lambda: _cloud_files(ws, job),
                runner=runner,
            )
            checks.extend({**r, "stage": name} for r in results)
            return aborted

        init_rc = _init(
            ws, cwd, job, tool, tf_env, phase="applying", streamer=streamer, runner=runner
        )
        if init_rc != 0:
            return _job_failed(client, job_id, "init", checks, error="init failed")
        # A write made outside Stackd since the plan job shows here (§19.7) — tofu would then
        # refuse the saved plan as stale, and the baseline below must not absorb that write.
        observe(baseline=False)
        # The saved plan the human reviewed (§7.4). Missing or unreadable → the apply fails closed:
        # it never re-plans, so it can only ever apply what was reviewed.
        try:
            saved_plan = client.download_artifact(job_id, "plan.tfplan")
            refusal = "" if saved_plan else "the plan file is empty"
        except Exception as exc:  # noqa: BLE001 — any refusal or transport error: fail closed
            saved_plan, refusal = b"", str(exc)
        if refusal:
            error = masker.mask(f"saved plan unavailable, not applied: {refusal}")
            return _job_failed(client, job_id, "apply", checks, error=error)
        # A failing before_apply hook fails the job before tofu runs: nothing is applied.
        if stage("before_apply"):
            return _job_failed(client, job_id, "before_apply", checks)
        # Written only now, after the before_apply hooks (a repo hook can't swap it), and removed
        # right after the apply (after_apply repo hooks never see its secrets).
        plan_file = ws.write_plan(cwd, saved_plan)
        # Hard apply budget (§4.2): the API caps an apply's wall-clock; the watchdog kills tofu past
        # it (exit 124) so a hung apply can't pin the worker or outlive its short-lived credentials.
        apply_timeout = job.get("apply_timeout_seconds") or _DEFAULT_TIMEOUT_SECONDS
        try:
            # With the run's variables too: tofu refuses a saved plan whose inputs changed (§7.3).
            with _tofu_files(ws, cwd, job):
                code, events = stream_json_command(
                    [tool, "apply", "-input=false", "-json", plan_file.name],
                    cwd,
                    tf_env,
                    phase="applying",
                    section=None,
                    streamer=streamer,
                    runner=runner,
                    timeout=apply_timeout,
                )
        finally:
            plan_file.unlink(missing_ok=True)
            # The state as the apply left it — whatever its exit: a failed or stopped apply may
            # have written part of it — is Stackd's own write: the new baseline (§19.7).
            observe(baseline=True)
        if code != 0:
            # A stale saved plan (state changed since it was made) is refused by tofu itself.
            error = (
                f"apply exceeded the {apply_timeout}s time budget and was terminated"
                if code == 124
                else (_apply_error(events) or "apply failed")
            )
            # Mask: a tofu diagnostic can echo a sensitive variable value verbatim (§5.1).
            return _job_failed(client, job_id, "apply", checks, error=masker.mask(error))
        # The apply is done: what is left (outputs, after_apply hooks) is bookkeeping the run needs
        # finished, so a stop request that arrives now no longer interrupts the job (§4.1).
        _token = cancel.current()
        if _token is not None:
            _token.disarm()
        with _tofu_files(ws, cwd, job, tfvars=False):
            out = runner.capture([tool, "output", "-json"], cwd, tf_env)
        ok_output = out.returncode == 0
        if ok_output:
            client.upload_artifact(job_id, "outputs.json", masker.mask(out.stdout).encode())
            # Tripwire on apply is warn-only: infra already changed, so failing the job here would
            # misrepresent a successful apply (§5.1). Surface it as a masked log line.
            leak = _secret_leak_in_outputs(out.stdout, masker)
            if leak:
                streamer.emit("applying", [f"[stackd] secret_leak_suspected: {leak}"])
        outputs = _safe_json(out.stdout) if ok_output else {}
        # A failing after_apply hook still fails the run, like at any stage (§8.3), and nothing
        # cascades from it. But the infra has changed: its outputs, read before the hooks ran,
        # are reported so the API records them (§9.1).
        if stage("after_apply"):
            return _job_failed(client, job_id, "after_apply", checks, outputs=outputs)
        client.event(job_id, "phase_finished", result={"outputs": outputs, "checks": checks})
    finally:
        ws.cleanup()


def _holds_apply_credentials(job: dict) -> bool:
    """An apply job, or a mutating command (`cred_phase` "apply", or absent: fail closed, §4.3)."""
    if job.get("phase") == "apply":
        return True
    return job.get("command") is not None and job.get("cred_phase") != "plan"


def _command_timeout(job: dict) -> int:
    """Wall-clock cap for a command run's subcommand, from the job's credential phase as the API
    sent it (`cred_phase`, §7.2). A mutating command holds apply credentials (apply OIDC role,
    apply-window state token) and the API reclaims it after the apply budget + grace (§4.2/§4.3),
    so it gets the same hard budget as an apply — it must not outlive its credentials or its
    reclaim window. Only an explicit `cred_phase: "plan"` (a read-only command) keeps the default
    cap: an absent or unknown value (an older API) fails closed to the apply budget."""
    if job.get("cred_phase") == "plan":
        return _DEFAULT_TIMEOUT_SECONDS
    return job.get("apply_timeout_seconds") or _DEFAULT_TIMEOUT_SECONDS


# Flags the agent adds to a subcommand so it never waits on a prompt: `force-unlock` asks for a
# confirmation otherwise. The user never passes these (§4.3) — the API refuses a leading `-` arg
# outside its per-command option allowlist.
_NON_INTERACTIVE: dict[str, list[str]] = {"force-unlock": ["-force"]}


# The allowlisted subcommands (§4.3) that read the configuration's variables and take
# `-var-file`: they get the env's repo var files (§3.4), as the plan does.
_READS_VARIABLES = frozenset({"import", "refresh"})


def _command_argv(tool: str, cmd: dict, var_flags: list[str] | None = None) -> list[str]:
    name = str(cmd.get("name", ""))
    return [
        tool,
        *name.split(),
        *_NON_INTERACTIVE.get(name, []),
        *(var_flags or []),
        *(cmd.get("args") or []),
    ]


def handle_command_run(client: ApiClient, job: dict, settings: Settings) -> None:
    """Run one allowlisted tofu/terraform subcommand (RunType.command): clone, init, run it.
    No hooks, no plan/apply phases — a one-off operation (import, state rm, …)."""
    job_id = job["job_id"]
    env_info = job["environment"]
    cmd = job.get("command") or {}
    masker = _masker(job)
    streamer = LogStreamer(client, job_id, masker)
    ws = Workspace(settings.workspace_root, job_id)
    tool = _tool_bin(env_info["tool"])

    try:
        runner = _make_runner(settings, job, ws)
        cwd = _clone(ws, job, env_info.get("commit_sha"))
        var_flags = _var_file_flags(ws, cwd, job) if cmd.get("name") in _READS_VARIABLES else []
        backend = job.get("backend")
        if backend:
            ws.write_backend_override(cwd)
        tf_env = _tofu_env(_run_env(ws, job, runner.name), backend)  # + state backend, tofu only

        client.event(job_id, "phase_started", phase="running")
        init_rc = _init(
            ws, cwd, job, tool, tf_env, phase="running", streamer=streamer, runner=runner
        )
        if init_rc != 0:
            return client.event(job_id, "job_failed", phase="init", result={"error": "init failed"})

        # A mutating command writes the state (`observe_state` is set for those only, §19.7): the
        # state before it is a check, the state after it the new baseline.
        def observe(*, baseline: bool) -> None:
            _observe_state(
                client,
                job,
                ws=ws,
                cwd=cwd,
                tool=tool,
                tf_env=tf_env,
                runner=runner,
                streamer=streamer,
                phase="running",
                baseline=baseline,
            )

        observe(baseline=False)
        argv = _command_argv(tool, cmd, var_flags)
        timeout = _command_timeout(job)
        try:
            with _tofu_files(ws, cwd, job):
                code = run_command(
                    argv,
                    cwd,
                    tf_env,
                    phase="running",
                    section=None,
                    streamer=streamer,
                    runner=runner,
                    timeout=timeout,
                )
        finally:
            observe(baseline=True)
        if code != 0:
            error = (
                f"{cmd.get('name')} exceeded the {timeout}s time budget and was terminated"
                if code == 124
                else f"{cmd.get('name')} failed"
            )
            return client.event(
                job_id, "job_failed", phase="command", result={"error": masker.mask(error)}
            )
        client.event(job_id, "phase_finished", result={"command": cmd.get("name")})
    finally:
        ws.cleanup()


def _handle_command(
    client: ApiClient, cmd: dict, settings: Settings, jobs: cancel.JobRegistry | None = None
) -> None:
    """Downward commands delivered via heartbeat (§7.1): read-only diagnostics, and `cancel_job`,
    which stops a job this worker is executing (§4.1)."""
    if cmd.get("type") == "cancel_job" and jobs is not None:
        job_id = str((cmd.get("payload") or {}).get("job_id", ""))
        found = jobs.cancel(job_id)
        log.info(
            "cancel requested",
            extra={"event": "agent.cancel_requested", "run_id": job_id, "running": found},
        )
        client.command_result(cmd["id"], {"job_id": job_id, "running_here": found})
        return
    if cmd.get("type") == "diagnostics":
        from agent.diagnostics import collect

        log.info(
            "running diagnostics", extra={"event": "agent.diagnostics", "command_id": cmd["id"]}
        )
        try:
            client.command_result(cmd["id"], collect(settings.api_url, settings.runner))
        except Exception as exc:  # noqa: BLE001
            client.command_result(cmd["id"], {"error": str(exc)}, status="failed")


class _InFlight:
    """Thread-safe count of jobs currently executing (§7, Phase E). Drives the heartbeat's
    busy/idle and bounds the claim loop."""

    def __init__(self) -> None:
        self._n = 0
        self._lock = threading.Lock()

    def inc(self) -> None:
        with self._lock:
            self._n += 1

    def dec(self) -> None:
        with self._lock:
            self._n -= 1

    def count(self) -> int:
        with self._lock:
            return self._n


def _heartbeat_loop(
    client: ApiClient,
    settings: Settings,
    inflight: _InFlight,
    jobs: cancel.JobRegistry | None = None,
) -> None:
    """Beat on a fixed cadence, independent of the claim long-poll and of job execution — otherwise
    a blocking claim (poll_wait) or a long plan/apply would starve the heartbeat and the worker would
    be marked offline while it's actually fine. Reports in-flight count (→ busy/idle) and capacity.
    Downward commands are handled here too."""
    while True:
        try:
            commands = client.heartbeat(
                in_flight=inflight.count(), capacity=settings.max_concurrent_jobs
            )
            for cmd in commands:
                _handle_command(client, cmd, settings, jobs)
        except Exception as exc:  # noqa: BLE001 — never let a transient API error kill the heartbeat
            log.warning(
                "heartbeat failed", extra={"event": "agent.heartbeat_error", "error": str(exc)}
            )
        time.sleep(settings.heartbeat_interval)


_SHUTDOWN_ERROR = (
    "worker_shutdown: the worker was stopped before the job finished (drain timeout or a second "
    "stop signal); the job was interrupted"
)


def _run_job(client: ApiClient, job: dict, settings: Settings) -> None:
    """Execute one claimed job to completion, reporting a job_failed on any uncaught error."""
    log.info(
        "claimed job",
        extra={"event": "agent.claimed", "run_id": job["job_id"], "phase": job["phase"]},
    )
    checks: list[dict[str, Any]] = []  # the job's check results so far (§8.3)
    try:
        # §7.4: the stack's tool version, on this worker, before anything runs — else the job fails
        # (ToolUnavailable, reported below) instead of running another version.
        env_info = job["environment"]
        pick(settings.available_tools(), env_info["tool"], env_info["tool_version"])
        if job["phase"] == "plan":
            handle_plan(client, job, settings, checks)
        elif job["phase"] == "apply":
            handle_apply(client, job, settings, checks)
        else:
            handle_command_run(client, job, settings)
        log.info("job done", extra={"event": "agent.done", "run_id": job["job_id"]})
    except cancel.JobCanceled:
        token = cancel.current()
        if token is not None and token.by_shutdown_only:
            # Stopped by this worker's own shutdown (the drain gave up, §7.1), not on request: the
            # API never asked to cancel the run, so it is a failure — reported now rather than left
            # to the `worker_lost` reclaim.
            jid = job["job_id"]
            log.warning(
                "job stopped by shutdown", extra={"event": "agent.shutdown_stopped", "run_id": jid}
            )
            try:
                _job_failed(client, jid, job.get("phase"), checks, error=_SHUTDOWN_ERROR)
            except Exception as report_exc:  # noqa: BLE001 — e.g. the API already ended the run
                log.warning(
                    "job_failed not accepted",
                    extra={"event": "agent.report_error", "run_id": jid, "error": str(report_exc)},
                )
            return
        # Stopped on a user's request (§4.1): the tool was interrupted, the workspace is gone
        # (`finally: ws.cleanup()` ran). With the checks run so far, like a failure's.
        log.info("job canceled", extra={"event": "agent.canceled", "run_id": job["job_id"]})
        try:
            client.event(
                job["job_id"], "job_canceled", phase=job.get("phase"), result={"checks": checks}
            )
        except Exception as report_exc:  # noqa: BLE001 — e.g. the API already ended the run
            log.warning(
                "job_canceled not accepted",
                extra={
                    "event": "agent.report_error",
                    "run_id": job["job_id"],
                    "error": str(report_exc),
                },
            )
    except Exception as exc:  # noqa: BLE001 — report and keep polling
        jid, jphase = job.get("job_id"), job.get("phase")
        # The exception message can carry secrets (e.g. a clone URL with the repo token, a tofu
        # error echoing a sensitive value). Mask before logging or reporting it to the API (§5.1).
        detail = _masker(job).mask(str(exc))
        log.error("job failed", extra={"event": "agent.failed", "run_id": jid, "error": detail})
        if jid:
            try:
                # With the checks run so far: an error ending the job doesn't lose them (§8.3).
                _job_failed(client, jid, jphase, checks, error=detail)
            except Exception as report_exc:  # noqa: BLE001 — e.g. the API already failed the run
                log.warning(
                    "job_failed not accepted",
                    extra={"event": "agent.report_error", "run_id": jid, "error": str(report_exc)},
                )


def _claim_loop(
    client: ApiClient,
    settings: Settings,
    drain: Drain,
    slots: threading.Semaphore,
    start: Callable[[dict], None],
) -> None:
    """Claim jobs until a drain starts (§7.1). No claim starts once draining; a long-poll already in
    flight is let through and a job it returns is started — it is claimed, the run is this worker's
    now: dropping it would leave the run to the `worker_lost` reclaim. `start` takes over the slot
    it is handed."""
    while not drain.draining:
        if not slots.acquire(timeout=0.5):  # every slot busy: look at the drain now and then
            continue
        if drain.draining:
            slots.release()
            break
        try:
            job = client.claim(wait=settings.poll_wait)
        except Exception as exc:  # noqa: BLE001 — a transient claim error must not kill the worker
            log.warning("claim failed", extra={"event": "agent.claim_error", "error": str(exc)})
            slots.release()
            drain.sleep(settings.heartbeat_interval)
            continue
        if job is None:
            slots.release()
            continue
        start(job)


def run() -> int:
    setup_logging()
    # First SIGTERM/SIGINT: drain; second: stop now (§7.1). Installed before anything else so a
    # stop while waiting for the token or the registration ends the agent at once.
    drain = Drain()
    signal.signal(signal.SIGTERM, drain.on_signal)
    signal.signal(signal.SIGINT, drain.on_signal)

    settings = Settings.from_env()
    drain.timeout = settings.drain_timeout_seconds
    # Token may be delivered via a file the platform seed writes after the worker boots; re-read
    # until it appears rather than idling forever (handles boot order + token rotation).
    while not settings.pool_token:
        log.warning("waiting for STACKD_POOL_TOKEN", extra={"event": "agent.idle"})
        drain.sleep(settings.heartbeat_interval)
        if drain.draining:
            return 0
        settings = Settings.from_env()

    client = ApiClient(settings.api_url)
    # Registration must survive the API not being ready yet (boot order: the worker can come up
    # before the API has migrated/seeded). Retry instead of crashing — otherwise the worker dies on
    # the first 5xx and never comes back without a restart.
    while True:
        if drain.draining:
            return 0
        try:
            client.register(
                settings.pool_token,
                settings.worker_name,
                labels=settings.labels or None,
                runner=settings.runner,
                tools=settings.available_tools(),
            )
            break
        except Exception as exc:  # noqa: BLE001
            refused = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 403
            if refused:
                # Refused for good (e.g. a production API and a non-docker runner, SPECS §7.4):
                # say why, and retry slowly — the API audits a refusal once an hour, not per try.
                log.error(
                    "registration refused by the API",
                    extra={"event": "agent.register_refused", "error": exc.response.text[:500]},
                )
            else:
                log.warning(
                    "register failed, retrying",
                    extra={"event": "agent.register_error", "error": str(exc)},
                )
            drain.sleep(
                max(settings.heartbeat_interval, 300) if refused else settings.heartbeat_interval
            )
            # Re-read the token: the seed rotates the pool token on a reset (DB wiped → fresh token
            # written to the shared file), so a token read at boot can be stale → 401.
            settings = Settings.from_env()
    log.info(
        "registered",
        extra={
            "event": "agent.registered",
            "worker": settings.worker_name,
            "api": settings.api_url,
        },
    )

    inflight = _InFlight()
    # Heartbeat runs on its own daemon thread so neither the claim long-poll nor a long-running job
    # can starve it (fixes the worker flickering offline between claims / during plan & apply). It
    # keeps beating through a drain: the worker stays online while its jobs finish.
    jobs = cancel.JobRegistry(settings.cancel_grace_seconds)
    threading.Thread(
        target=_heartbeat_loop, args=(client, settings, inflight, jobs), daemon=True
    ).start()

    # Up to max_concurrent_jobs run at once, each on its own thread (§7, Phase E). The semaphore
    # caps in-flight jobs; the API's SELECT … FOR UPDATE SKIP LOCKED + one-active-run-per-env index
    # keep concurrent claims safe — concurrency is across *different* environments.
    pool = ThreadPoolExecutor(max_workers=settings.max_concurrent_jobs)
    slots = threading.Semaphore(settings.max_concurrent_jobs)

    # Jobs holding apply credentials → the monotonic time the drain must let them run until
    # (claim + their apply budget + margin, §7.1): a drain never interrupts an apply in its budget.
    holds: dict[str, float] = {}
    holds_lock = threading.Lock()

    def _hold_until() -> float | None:
        with holds_lock:
            return max(holds.values(), default=None)

    def _execute(job: dict, token: cancel.CancelToken) -> None:
        cancel.bind(token)  # this job's thread: the runners find its token here
        try:
            _run_job(client, job, settings)  # reports the job's end before the slot is freed
        finally:
            with holds_lock:
                holds.pop(job["job_id"], None)
            cancel.bind(None)
            jobs.unregister(job["job_id"])
            inflight.dec()
            slots.release()

    def _start(job: dict) -> None:
        if _holds_apply_credentials(job):
            with holds_lock:
                holds[job["job_id"]] = (
                    time.monotonic()
                    + (job.get("apply_timeout_seconds") or _DEFAULT_TIMEOUT_SECONDS)
                    + APPLY_HOLD_MARGIN_SECONDS
                )
        inflight.inc()
        # Registered before the job starts, so a cancel that arrives first still finds it.
        pool.submit(_execute, job, jobs.register(job["job_id"]))

    outcome: list[str] = []

    def _supervisor() -> None:
        result = supervise(
            drain,
            in_flight=inflight.count,
            stop_all=jobs.shutdown,
            cancel_grace=settings.cancel_grace_seconds,
            hold_until=_hold_until,
        )
        if result == "abandoned":
            # A job did not end even after its stop (or the claim in flight never returned): exit
            # without waiting for its thread. Its run goes to the API's `worker_lost` reclaim.
            logging.shutdown()
            os._exit(EXIT_CODES[result])
        outcome.append(result)

    supervisor = threading.Thread(target=_supervisor, name="drain-supervisor", daemon=True)
    supervisor.start()

    _claim_loop(client, settings, drain, slots, _start)
    drain.claims_stopped = True
    supervisor.join()
    pool.shutdown(wait=True)  # every job already reported (the supervisor waited for them)
    return EXIT_CODES["drained" if outcome == ["drained"] else "stopped"]


if __name__ == "__main__":
    sys.exit(run())
