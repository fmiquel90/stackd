from __future__ import annotations

import json
import os
import subprocess
import threading
import time

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


def _init_cmd(tool: str, backend: dict | None) -> list[str]:
    cmd = [tool, "init", "-input=false"]
    if backend:
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
            **os.environ,
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
                _init_cmd(tool, backend),
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
        # 2=changes, 1=error.
        code, events = stream_json_command(
            [tool, "plan", "-input=false", "-json", "-detailed-exitcode", "-out=plan.tfplan"],
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
        repo_env = {**os.environ, **job.get("env", {})}

        if (
            run_command(
                _init_cmd(tool, backend),
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
                _init_cmd(tool, backend),
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


def _heartbeat_loop(client: ApiClient, settings: Settings) -> None:
    """Beat on a fixed cadence, independent of the claim long-poll and of job execution — otherwise
    a blocking claim (poll_wait) or a long plan/apply would starve the heartbeat and the worker would
    be marked offline while it's actually fine. Downward commands are handled here too."""
    while True:
        try:
            for cmd in client.heartbeat():
                _handle_command(client, cmd, settings)
        except Exception as exc:  # noqa: BLE001 — never let a transient API error kill the heartbeat
            log.warning(
                "heartbeat failed", extra={"event": "agent.heartbeat_error", "error": str(exc)}
            )
        time.sleep(settings.heartbeat_interval)


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

    # Heartbeat runs on its own daemon thread so neither the claim long-poll nor a long-running job
    # can starve it (fixes the worker flickering offline between claims / during plan & apply).
    threading.Thread(target=_heartbeat_loop, args=(client, settings), daemon=True).start()

    while True:
        job = client.claim(wait=settings.poll_wait)
        if job is None:
            continue
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


if __name__ == "__main__":
    run()
