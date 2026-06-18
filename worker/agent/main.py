from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from agent.client import ApiClient
from agent.config import Settings
from agent.logging import get_logger, setup as setup_logging
from agent.masking import Masker
from agent.runner import LogStreamer, run_command, run_hooks, stream_json_command
from agent.workspace import Workspace

log = get_logger()


def _safe_json(raw: str) -> dict:
    """Parse JSON from a tofu subcommand; tolerate empty/garbled output instead of crashing."""
    try:
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}


# Cloud-credential env families an untrusted repo hook must never see (§8.3). The docker runner
# bakes none of these in; this also protects the trusted-dev `local` runner where a mounted ~/.aws
# can export AWS_* into the worker process.
_CLOUD_ENV_PREFIXES = ("AWS_", "GOOGLE_", "GCLOUD_", "GCP_", "AZURE_", "ARM_")


def _repo_environ() -> dict[str, str]:
    """Base process env for repo hooks, stripped of cloud credentials (§8.3)."""
    return {k: v for k, v in os.environ.items() if not k.startswith(_CLOUD_ENV_PREFIXES)}


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


def _tool_bin(tool: str) -> str:
    return "tofu" if tool == "opentofu" else "terraform"


def _cloud_env(ws: Workspace, job: dict) -> dict[str, str]:
    """OIDC workload env for terraform only (§10.4 / §8.3 — never for repo hooks)."""
    creds = job.get("cloud_credentials")
    if not creds:
        return {}
    token_file = ws.write_secret("oidc_token", creds["oidc_token"])
    env = {
        "AWS_WEB_IDENTITY_TOKEN_FILE": token_file,
        "AWS_ROLE_ARN": creds["role_arn"],
        "AWS_ROLE_SESSION_NAME": f"stackd-{job['job_id']}",
    }
    if creds.get("region"):
        env["AWS_REGION"] = creds["region"]
    return env


def _init_cmd(
    tool: str,
    backend: dict | None,
    backend_config_file: str | None = None,
    backend_config: dict | None = None,
) -> list[str]:
    cmd = [tool, "init", "-input=false"]
    if backend:  # managed state: the platform's HTTP backend
        for k in (
            "address",
            "lock_address",
            "unlock_address",
            "lock_method",
            "unlock_method",
            "username",
            "password",
        ):
            if backend.get(k) is not None:
                cmd.append(f"-backend-config={k}={backend[k]}")
    else:  # unmanaged: the repo's own partial backend — a .config file and/or inline key=value
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
        )
        ws.write_tfvars(cwd, job.get("tfvars_json", {}))
        ws.write_hcl_tfvars(cwd, job.get("hcl_tfvars", {}))
        backend = job.get("backend")
        if backend:
            ws.write_backend_override(cwd)
        hooks = _merge_hooks(job.get("hooks", {}), ws.load_stackd_yml(cwd))

        platform_env = {
            **os.environ,
            **job.get("env", {}),
            **job.get("sensitive_env", {}),
            **_cloud_env(ws, job),
        }
        repo_env = {
            **_repo_environ(),
            **job.get("env", {}),
        }  # no secrets / cloud creds to repo hooks (§8.3)
        checks: list[dict] = []

        client.event(job_id, "phase_started", phase="planning")

        def stage(name: str) -> bool:
            results, aborted = run_hooks(
                hooks.get(name, []),
                cwd,
                platform_env=platform_env,
                repo_env=repo_env,
                phase="planning",
                streamer=streamer,
            )
            checks.extend(r for r in results if r["status"] != "ok")
            return aborted

        if stage("before_init"):
            return client.event(
                job_id, "job_failed", phase="before_init", result={"checks": checks}
            )
        if (
            run_command(
                _init_cmd(tool, backend, job.get("backend_config_file"), job.get("backend_config")),
                cwd,
                platform_env,
                phase="planning",
                section=None,
                streamer=streamer,
            )
            != 0
        ):
            return client.event(job_id, "job_failed", phase="init", result={"error": "init failed"})
        stage("after_init")
        stage("before_plan")

        # `-json`: stream readable @message lines while collecting structured events (change_summary
        # for the counts, diagnostics for the real error message). detailed-exitcode: 0=no changes,
        # 2=changes, 1=error. `-refresh-only` (drift, §19): diff state against real infra only.
        plan_cmd = [tool, "plan", "-input=false", "-json", "-detailed-exitcode", "-out=plan.tfplan"]
        if job.get("refresh_only"):
            plan_cmd.insert(2, "-refresh-only")
        code, events = stream_json_command(
            plan_cmd,
            cwd,
            platform_env,
            phase="planning",
            section=None,
            streamer=streamer,
        )
        if code == 1:
            return client.event(
                job_id,
                "job_failed",
                phase="plan",
                result={"error": _first_error(events) or "plan failed"},
            )
        has_changes = code == 2
        summary = _summary_from_events(events)

        # The -json events streamed above are terse ("Plan to create"); also stream the human-readable
        # diff operators expect (the real `terraform plan` output), read from the saved plan file
        # (fast — no refresh, no API calls). `-no-color` so the ANSI doesn't fight the log viewer.
        run_command(
            [tool, "show", "-no-color", "plan.tfplan"],
            cwd,
            platform_env,
            phase="planning",
            section="plan",
            streamer=streamer,
        )

        # plan.json artifact for after_plan hooks (infracost/jq); `show -json` is already machine-readable.
        show = subprocess.run(
            [tool, "show", "-json", "plan.tfplan"],
            cwd=cwd,
            env=platform_env,
            capture_output=True,
            text=True,
        )
        plan_doc = show.stdout if show.returncode == 0 and show.stdout.strip() else "{}"
        (cwd / "plan.json").write_text(plan_doc)
        # Mask the artifact too: plan.json can echo sensitive variable values (§8.3 leak note).
        client.upload_artifact(job_id, "plan.json", masker.mask(plan_doc).encode())

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

        if job.get("hooks", {}).get("after_plan") or ws.load_stackd_yml(cwd).get("after_plan"):
            client.event(job_id, "phase_started", phase="checking")
        results, aborted = run_hooks(
            hooks.get("after_plan", []),
            cwd,
            platform_env=platform_env,
            repo_env=repo_env,
            phase="checking",
            streamer=streamer,
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
        cwd = ws.git_clone(
            env_info["repo_url"],
            env_info.get("commit_sha"),
            env_info["project_root"],
            token=_repo_token(job),
        )
        ws.write_tfvars(cwd, job.get("tfvars_json", {}))
        ws.write_hcl_tfvars(cwd, job.get("hcl_tfvars", {}))
        backend = job.get("backend")
        if backend:
            ws.write_backend_override(cwd)
        hooks = _merge_hooks(job.get("hooks", {}), ws.load_stackd_yml(cwd))
        platform_env = {
            **os.environ,
            **job.get("env", {}),
            **job.get("sensitive_env", {}),
            **_cloud_env(ws, job),
        }
        repo_env = {**_repo_environ(), **job.get("env", {})}

        if (
            run_command(
                _init_cmd(tool, backend, job.get("backend_config_file"), job.get("backend_config")),
                cwd,
                platform_env,
                phase="applying",
                section=None,
                streamer=streamer,
            )
            != 0
        ):
            return client.event(job_id, "job_failed", phase="init", result={"error": "init failed"})
        run_hooks(
            hooks.get("before_apply", []),
            cwd,
            platform_env=platform_env,
            repo_env=repo_env,
            phase="applying",
            streamer=streamer,
        )
        code, events = stream_json_command(
            [tool, "apply", "-input=false", "-json", "-auto-approve"],
            cwd,
            platform_env,
            phase="applying",
            section=None,
            streamer=streamer,
        )
        if code != 0:
            return client.event(
                job_id,
                "job_failed",
                phase="apply",
                result={"error": _first_error(events) or "apply failed"},
            )
        out = subprocess.run(
            [tool, "output", "-json"], cwd=cwd, env=platform_env, capture_output=True, text=True
        )
        client.upload_artifact(job_id, "outputs.json", masker.mask(out.stdout).encode())
        # Tripwire on apply is warn-only: infra already changed, so failing the job here would
        # misrepresent a successful apply (§5.1). Surface it as a masked log line.
        leak = _secret_leak_in_outputs(out.stdout, masker)
        if leak:
            streamer.emit("applying", [f"[stackd] secret_leak_suspected: {leak}"])
        outputs = _safe_json(out.stdout) if out.returncode == 0 else {}
        run_hooks(
            hooks.get("after_apply", []),
            cwd,
            platform_env=platform_env,
            repo_env=repo_env,
            phase="applying",
            streamer=streamer,
        )
        client.event(job_id, "phase_finished", result={"outputs": outputs})
    finally:
        ws.cleanup()


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
        )
        ws.write_tfvars(cwd, job.get("tfvars_json", {}))
        ws.write_hcl_tfvars(cwd, job.get("hcl_tfvars", {}))
        backend = job.get("backend")
        if backend:
            ws.write_backend_override(cwd)
        platform_env = {
            **os.environ,
            **job.get("env", {}),
            **job.get("sensitive_env", {}),
            **_cloud_env(ws, job),
        }

        client.event(job_id, "phase_started", phase="running")
        if (
            run_command(
                _init_cmd(tool, backend, job.get("backend_config_file"), job.get("backend_config")),
                cwd,
                platform_env,
                phase="running",
                section=None,
                streamer=streamer,
            )
            != 0
        ):
            return client.event(job_id, "job_failed", phase="init", result={"error": "init failed"})

        argv = [tool, *str(cmd.get("name", "")).split(), *(cmd.get("args") or [])]
        if (
            run_command(argv, cwd, platform_env, phase="running", section=None, streamer=streamer)
            != 0
        ):
            return client.event(
                job_id, "job_failed", phase="command", result={"error": f"{cmd.get('name')} failed"}
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
        log.exception("job failed", extra={"event": "agent.failed", "run_id": jid})
        if jid:
            client.event(jid, "job_failed", phase=jphase, result={"error": str(exc)})


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
    client.register(settings.pool_token, settings.worker_name)
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
