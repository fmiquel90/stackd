from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from pathlib import Path
from typing import Any

from agent.client import ApiClient
from agent.config import Settings
from agent.logging import get_logger, setup as setup_logging
from agent.masking import Masker, mask_plan_json
from agent.runner import (
    _DEFAULT_TIMEOUT_SECONDS,
    LogStreamer,
    run_command,
    run_hooks,
    stream_json_command,
)
from agent.workspace import Workspace

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


def _job_environ() -> dict[str, str]:
    """Base process env for tofu and platform hooks: the worker's, minus the agent's own settings
    (§7.4, §8.3). The rest stays — PATH and locale, proxies and CA, TF_* (plugin cache, …),
    provider settings — so no provider or check tool loses what it needs."""
    return {k: v for k, v in os.environ.items() if not k.startswith(_WORKER_ENV_PREFIX)}


def _repo_environ() -> dict[str, str]:
    """Base process env for repo hooks: the allowlisted variables of the worker's own environment
    only (§8.3). The run's non-sensitive `env` is layered on top by the caller."""
    return {k: v for k, v in os.environ.items() if k in _REPO_ENV_ALLOWLIST or k.startswith("LC_")}


def _secret_leak_in_outputs(doc: str, masker: Masker) -> str | None:
    """Cleartext tripwire (§5.1): a value Stackd treats as sensitive appearing verbatim in a
    *non-sensitive* output. terraform only redacts outputs the repo marked `sensitive = true`, so a
    sensitive Stackd variable echoed by an unmarked output leaks. Scans `tofu show -json` (plan) or
    `tofu output -json` (apply). Returns a masked description, or None."""
    try:
        data = json.loads(doc)
    except (json.JSONDecodeError, ValueError):
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


def _merge_hooks(platform: dict, repo: dict) -> dict[str, list[dict]]:
    """Platform hooks first (non-bypassable), then repo hooks, per stage (§8.1)."""
    merged: dict[str, list[dict]] = {}
    for stage in set(platform) | set(repo):
        merged[stage] = list(platform.get(stage, [])) + list(repo.get(stage, []))
    return merged


def _summary_from_events(events: list[dict]) -> dict:
    """Plan/apply counts straight from tofu's authoritative `change_summary` event (last wins)."""
    for evt in reversed(events):
        if evt.get("type") == "change_summary":
            c = evt.get("changes", {}) or {}
            return {
                "add": c.get("add", 0),
                "change": c.get("change", 0),
                "destroy": c.get("remove", 0),
            }
    return {"add": 0, "change": 0, "destroy": 0}


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


def _plan_json_unusable(show: subprocess.CompletedProcess[str]) -> str | None:
    """Why `tofu show -json`'s result is no plan document (a failed run, or anything but a JSON
    object), or None when it is one."""
    if show.returncode != 0:
        stderr = (show.stderr or "").splitlines()
        detail = next((line.strip() for line in stderr if line.strip()), "")
        return f"exited {show.returncode}" + (f": {detail}" if detail else "")
    try:
        doc = json.loads(show.stdout)
    except ValueError:
        return "printed no JSON"
    return None if isinstance(doc, dict) else "printed no plan object"


def _tool_bin(tool: str) -> str:
    return "tofu" if tool == "opentofu" else "terraform"


_OIDC_TOKEN_FILE = "oidc_token"


def _run_env(ws: Workspace, job: dict[str, Any]) -> dict[str, str]:
    """`_job_environ`, then the run's env, its sensitive env and the OIDC workload env (§10.4):
    what platform hooks run with, and tofu with the state backend's settings on top."""
    return {
        **_job_environ(),
        **job.get("env", {}),
        **job.get("sensitive_env", {}),
        **_cloud_env(ws, job),
    }


def _cloud_env(ws: Workspace, job: dict) -> dict[str, str]:
    """OIDC workload env for terraform and platform hooks only (§10.4 / §8.3 — never for repo
    hooks). The token file it names is on disk only while one of them runs (`_cloud_token`)."""
    creds = job.get("cloud_credentials")
    if not creds:
        return {}
    env = {
        "AWS_WEB_IDENTITY_TOKEN_FILE": str(ws.secret_path(_OIDC_TOKEN_FILE)),
        "AWS_ROLE_ARN": creds["role_arn"],
        "AWS_ROLE_SESSION_NAME": f"stackd-{job['job_id']}",
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


def _cloud_token(ws: Workspace, job: dict[str, Any]) -> AbstractContextManager[object]:
    """The OIDC token file `_cloud_env` points at, written for one tofu command or platform hook
    and removed right after: never on disk while a repo hook runs (§8.3)."""
    token = (job.get("cloud_credentials") or {}).get("oidc_token")
    return ws.secret_file(_OIDC_TOKEN_FILE, token) if token else nullcontext()


@contextmanager
def _tofu_files(
    ws: Workspace, cwd: Path, job: dict[str, Any], *, tfvars: bool = True
) -> Iterator[None]:
    """What one tofu command reads from disk that no repo hook may find (§8.3), present only while
    it runs: the OIDC token file, and the run's variable files unless the command reads none."""
    with ExitStack() as files:
        files.enter_context(_cloud_token(ws, job))
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


def handle_plan(client: ApiClient, job: dict, settings: Settings) -> None:
    job_id = job["job_id"]
    env_info = job["environment"]
    masker = Masker(job.get("mask_values", []))
    streamer = LogStreamer(client, job_id, masker)
    ws = Workspace(settings.workspace_root, job_id)
    tool = _tool_bin(env_info["tool"])

    try:
        cwd = ws.git_clone(
            env_info["repo_url"],
            env_info.get("commit_sha"),
            env_info["project_root"],
            token=_repo_token(job),
            branch=env_info.get("branch"),
        )
        backend = job.get("backend")
        if backend:
            ws.write_backend_override(cwd)
        hooks = _merge_hooks(job.get("hooks", {}), ws.load_stackd_yml(cwd))

        platform_env = _run_env(ws, job)
        repo_env = {
            **_repo_environ(),
            **job.get("env", {}),
        }  # no secrets / cloud creds to repo hooks (§8.3)
        tf_env = _tofu_env(platform_env, backend)  # + state-backend creds, for tofu only
        checks: list[dict] = []

        # Report the exact commit checked out: the API pins the run to it (or refuses a mismatch
        # with the commit it pinned), and the apply job clones exactly that SHA (§7.2/§7.3).
        client.event(
            job_id, "phase_started", phase="planning", result={"commit_sha": ws.head_sha()}
        )

        def stage(name: str) -> bool:
            """A stage's hooks (§8.3): True when an `on_failure: fail` hook failed — the job fails
            at that stage, before the next tofu step. A `warn` one is reported in the checks."""
            results, aborted = run_hooks(
                hooks.get(name, []),
                cwd,
                platform_env=platform_env,
                repo_env=repo_env,
                phase="planning",
                streamer=streamer,
                platform_files=lambda: _cloud_token(ws, job),
            )
            checks.extend(r for r in results if r["status"] != "ok")
            return aborted

        if stage("before_init"):
            return client.event(
                job_id, "job_failed", phase="before_init", result={"checks": checks}
            )
        with _tofu_files(ws, cwd, job):
            init_rc = run_command(
                _init_cmd(tool, backend, job.get("backend_config_file"), job.get("backend_config")),
                cwd,
                tf_env,
                phase="planning",
                section=None,
                streamer=streamer,
            )
        if init_rc != 0:
            return client.event(job_id, "job_failed", phase="init", result={"error": "init failed"})
        for name in ("after_init", "before_plan"):
            if stage(name):
                return client.event(job_id, "job_failed", phase=name, result={"checks": checks})

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
        with _tofu_files(ws, cwd, job):
            code, events = stream_json_command(
                plan_cmd,
                cwd,
                tf_env,
                phase="planning",
                section=None,
                streamer=streamer,
            )
        if code not in (0, 2):
            # Only 0 and 2 are a plan: anything else — an error, the watchdog's kill (124), a
            # signal — fails the job closed, never passes for "no changes".
            error = _exit_error("plan", code, _DEFAULT_TIMEOUT_SECONDS, events)
            return client.event(
                job_id,
                "job_failed",
                phase="plan",
                # Mask: a tofu diagnostic can echo a sensitive variable value verbatim (§5.1).
                result={"error": masker.mask(error)},
            )
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
            )
            show = subprocess.run(
                [tool, "show", "-json", "-no-color", str(plan_file)],
                cwd=cwd,
                env=tf_env,
                capture_output=True,
                text=True,
            )
        # The after_plan checks and the reviewers read this document: without it, fail closed — a
        # stand-in `{}` would let every policy pass on an empty plan.
        unusable = _plan_json_unusable(show)
        if unusable:
            error = f"plan JSON unavailable, not checked: `{tool} show -json` {unusable}"
            return client.event(
                job_id, "job_failed", phase="plan", result={"error": masker.mask(error)}
            )
        plan_doc = show.stdout
        # It lists the run's variable values in clear (§8.3): the artifact, and what repo hooks
        # read, is a copy with every sensitive value masked.
        masked_doc = mask_plan_json(plan_doc, masker)
        client.upload_artifact(job_id, "plan.json", masked_doc.encode())
        # The saved plan, read once now (what was shown above) and removed before any hook runs.
        saved_plan = plan_file.read_bytes() if has_changes and job.get("save_plan") else b""
        plan_file.unlink(missing_ok=True)

        # Cleartext tripwire (§5.1): the artifact is masked, but a sensitive value surfacing in a
        # non-sensitive output means the repo didn't mark it `sensitive` — flag (or fail) the run.
        leak = _secret_leak_in_outputs(plan_doc, masker)
        if leak:
            if settings.leak_action == "fail":
                return client.event(
                    job_id,
                    "job_failed",
                    phase="plan",
                    result={"error": f"secret_leak_suspected: {leak}"},
                )
            checks.append({"name": "secret_leak_suspected", "status": "warn", "detail": leak})

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
                platform_files=lambda: _cloud_token(ws, job),
            )
            checks.extend(r for r in results if r["status"] != "ok")
        if aborted:
            return client.event(job_id, "job_failed", phase="after_plan", result={"checks": checks})

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


def handle_apply(client: ApiClient, job: dict, settings: Settings) -> None:
    job_id = job["job_id"]
    env_info = job["environment"]
    masker = Masker(job.get("mask_values", []))
    streamer = LogStreamer(client, job_id, masker)
    ws = Workspace(settings.workspace_root, job_id)
    tool = _tool_bin(env_info["tool"])

    try:
        # The apply runs on exactly the commit the plan was made on — pinned by the API from the
        # plan job's report (§7.2) — never on wherever the branch head has moved since.
        commit_sha = env_info.get("commit_sha")
        if not commit_sha:
            return client.event(
                job_id,
                "job_failed",
                phase="apply",
                result={"error": "apply refused: the run has no pinned commit; trigger a new plan"},
            )
        cwd = ws.git_clone(
            env_info["repo_url"],
            commit_sha,
            env_info["project_root"],
            token=_repo_token(job),
            branch=env_info.get("branch"),
        )
        backend = job.get("backend")
        if backend:
            ws.write_backend_override(cwd)
        hooks = _merge_hooks(job.get("hooks", {}), ws.load_stackd_yml(cwd))
        platform_env = _run_env(ws, job)
        repo_env = {**_repo_environ(), **job.get("env", {})}
        tf_env = _tofu_env(platform_env, backend)  # + state-backend creds, for tofu only
        checks: list[dict[str, Any]] = []

        def stage(name: str) -> bool:
            """As in the plan job (§8.3): True when an `on_failure: fail` hook failed. A `warn` one
            is reported in the checks — the run is already confirmed, it can't go back to it."""
            results, aborted = run_hooks(
                hooks.get(name, []),
                cwd,
                platform_env=platform_env,
                repo_env=repo_env,
                phase="applying",
                streamer=streamer,
                platform_files=lambda: _cloud_token(ws, job),
            )
            checks.extend(r for r in results if r["status"] != "ok")
            return aborted

        with _tofu_files(ws, cwd, job):
            init_rc = run_command(
                _init_cmd(tool, backend, job.get("backend_config_file"), job.get("backend_config")),
                cwd,
                tf_env,
                phase="applying",
                section=None,
                streamer=streamer,
            )
        if init_rc != 0:
            return client.event(job_id, "job_failed", phase="init", result={"error": "init failed"})
        # The saved plan the human reviewed (§7.4). Missing or unreadable → the apply fails closed:
        # it never re-plans, so it can only ever apply what was reviewed.
        try:
            saved_plan = client.download_artifact(job_id, "plan.tfplan")
            refusal = "" if saved_plan else "the plan file is empty"
        except Exception as exc:  # noqa: BLE001 — any refusal or transport error: fail closed
            saved_plan, refusal = b"", str(exc)
        if refusal:
            return client.event(
                job_id,
                "job_failed",
                phase="apply",
                result={"error": masker.mask(f"saved plan unavailable, not applied: {refusal}")},
            )
        # A failing before_apply hook fails the job before tofu runs: nothing is applied.
        if stage("before_apply"):
            return client.event(
                job_id, "job_failed", phase="before_apply", result={"checks": checks}
            )
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
                    timeout=apply_timeout,
                )
        finally:
            plan_file.unlink(missing_ok=True)
        if code != 0:
            # A stale saved plan (state changed since it was made) is refused by tofu itself.
            error = (
                f"apply exceeded the {apply_timeout}s time budget and was terminated"
                if code == 124
                else (_apply_error(events) or "apply failed")
            )
            return client.event(
                job_id,
                "job_failed",
                phase="apply",
                # Mask: a tofu diagnostic can echo a sensitive variable value verbatim (§5.1).
                result={"error": masker.mask(error)},
            )
        with _tofu_files(ws, cwd, job, tfvars=False):
            out = subprocess.run(
                [tool, "output", "-json"], cwd=cwd, env=tf_env, capture_output=True, text=True
            )
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
            return client.event(
                job_id,
                "job_failed",
                phase="after_apply",
                result={"checks": checks, "outputs": outputs},
            )
        client.event(job_id, "phase_finished", result={"outputs": outputs, "checks": checks})
    finally:
        ws.cleanup()


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


def handle_command_run(client: ApiClient, job: dict, settings: Settings) -> None:
    """Run one allowlisted tofu/terraform subcommand (RunType.command): clone, init, run it.
    No hooks, no plan/apply phases — a one-off operation (import, state rm, …)."""
    job_id = job["job_id"]
    env_info = job["environment"]
    cmd = job.get("command") or {}
    masker = Masker(job.get("mask_values", []))
    streamer = LogStreamer(client, job_id, masker)
    ws = Workspace(settings.workspace_root, job_id)
    tool = _tool_bin(env_info["tool"])

    try:
        cwd = ws.git_clone(
            env_info["repo_url"],
            env_info.get("commit_sha"),
            env_info["project_root"],
            token=_repo_token(job),
            branch=env_info.get("branch"),
        )
        backend = job.get("backend")
        if backend:
            ws.write_backend_override(cwd)
        tf_env = _tofu_env(_run_env(ws, job), backend)  # + state-backend creds, for tofu only

        client.event(job_id, "phase_started", phase="running")
        with _tofu_files(ws, cwd, job):
            init_rc = run_command(
                _init_cmd(tool, backend, job.get("backend_config_file"), job.get("backend_config")),
                cwd,
                tf_env,
                phase="running",
                section=None,
                streamer=streamer,
            )
        if init_rc != 0:
            return client.event(job_id, "job_failed", phase="init", result={"error": "init failed"})

        argv = [tool, *str(cmd.get("name", "")).split(), *(cmd.get("args") or [])]
        timeout = _command_timeout(job)
        with _tofu_files(ws, cwd, job):
            code = run_command(
                argv,
                cwd,
                tf_env,
                phase="running",
                section=None,
                streamer=streamer,
                timeout=timeout,
            )
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


def _handle_command(client: ApiClient, cmd: dict, settings: Settings) -> None:
    """Downward commands delivered via heartbeat (§7.1). Today: read-only diagnostics."""
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


def _heartbeat_loop(client: ApiClient, settings: Settings, inflight: _InFlight) -> None:
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
                _handle_command(client, cmd, settings)
        except Exception as exc:  # noqa: BLE001 — never let a transient API error kill the heartbeat
            log.warning(
                "heartbeat failed", extra={"event": "agent.heartbeat_error", "error": str(exc)}
            )
        time.sleep(settings.heartbeat_interval)


def _run_job(client: ApiClient, job: dict, settings: Settings) -> None:
    """Execute one claimed job to completion, reporting a job_failed on any uncaught error."""
    log.info(
        "claimed job",
        extra={"event": "agent.claimed", "run_id": job["job_id"], "phase": job["phase"]},
    )
    try:
        if job["phase"] == "plan":
            handle_plan(client, job, settings)
        elif job["phase"] == "apply":
            handle_apply(client, job, settings)
        else:
            handle_command_run(client, job, settings)
        log.info("job done", extra={"event": "agent.done", "run_id": job["job_id"]})
    except Exception as exc:  # noqa: BLE001 — report and keep polling
        jid, jphase = job.get("job_id"), job.get("phase")
        # The exception message can carry secrets (e.g. a clone URL with the repo token, a tofu
        # error echoing a sensitive value). Mask before logging or reporting it to the API (§5.1).
        detail = Masker(job.get("mask_values", [])).mask(str(exc))
        log.error("job failed", extra={"event": "agent.failed", "run_id": jid, "error": detail})
        if jid:
            try:
                client.event(jid, "job_failed", phase=jphase, result={"error": detail})
            except Exception as report_exc:  # noqa: BLE001 — e.g. the API already failed the run
                log.warning(
                    "job_failed not accepted",
                    extra={"event": "agent.report_error", "run_id": jid, "error": str(report_exc)},
                )


def run() -> None:
    setup_logging()
    settings = Settings.from_env()
    # Token may be delivered via a file the platform seed writes after the worker boots; re-read
    # until it appears rather than idling forever (handles boot order + token rotation).
    while not settings.pool_token:
        log.warning("waiting for STACKD_POOL_TOKEN", extra={"event": "agent.idle"})
        time.sleep(settings.heartbeat_interval)
        settings = Settings.from_env()

    client = ApiClient(settings.api_url)
    # Registration must survive the API not being ready yet (boot order: the worker can come up
    # before the API has migrated/seeded). Retry instead of crashing — otherwise the worker dies on
    # the first 5xx and never comes back without a restart.
    while True:
        try:
            client.register(settings.pool_token, settings.worker_name)
            break
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "register failed, retrying",
                extra={"event": "agent.register_error", "error": str(exc)},
            )
            time.sleep(settings.heartbeat_interval)
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
    # can starve it (fixes the worker flickering offline between claims / during plan & apply).
    threading.Thread(target=_heartbeat_loop, args=(client, settings, inflight), daemon=True).start()

    # Up to max_concurrent_jobs run at once, each on its own thread (§7, Phase E). The semaphore
    # caps in-flight jobs; the API's SELECT … FOR UPDATE SKIP LOCKED + one-active-run-per-env index
    # keep concurrent claims safe — concurrency is across *different* environments.
    pool = ThreadPoolExecutor(max_workers=settings.max_concurrent_jobs)
    slots = threading.Semaphore(settings.max_concurrent_jobs)

    def _execute(job: dict) -> None:
        try:
            _run_job(client, job, settings)
        finally:
            inflight.dec()
            slots.release()

    while True:
        slots.acquire()  # block until a slot frees up (serial when max_concurrent_jobs=1)
        try:
            job = client.claim(wait=settings.poll_wait)
        except Exception as exc:  # noqa: BLE001 — a transient claim error must not kill the worker
            log.warning("claim failed", extra={"event": "agent.claim_error", "error": str(exc)})
            slots.release()
            time.sleep(settings.heartbeat_interval)
            continue
        if job is None:
            slots.release()
            continue
        inflight.inc()
        pool.submit(_execute, job)


if __name__ == "__main__":
    run()
