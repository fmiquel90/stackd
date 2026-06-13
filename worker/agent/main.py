from __future__ import annotations

import json
import os
import subprocess
import time

from agent.client import ApiClient
from agent.config import Settings
from agent.logging import get_logger, setup as setup_logging
from agent.masking import Masker
from agent.runner import LogStreamer, run_command, run_hooks
from agent.workspace import Workspace

log = get_logger()


def _merge_hooks(platform: dict, repo: dict) -> dict[str, list[dict]]:
    """Platform hooks first (non-bypassable), then repo hooks, per stage (§8.1)."""
    merged: dict[str, list[dict]] = {}
    for stage in set(platform) | set(repo):
        merged[stage] = list(platform.get(stage, [])) + list(repo.get(stage, []))
    return merged


def _plan_summary(plan_json: dict) -> dict:
    add = change = destroy = 0
    for rc in plan_json.get("resource_changes", []):
        actions = rc.get("change", {}).get("actions", [])
        if actions == ["create"]:
            add += 1
        elif actions == ["delete"]:
            destroy += 1
        elif "update" in actions or set(actions) == {"create", "delete"}:
            change += 1
    return {"add": add, "change": change, "destroy": destroy}


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
            env_info["repo_url"], env_info.get("commit_sha"), env_info["project_root"]
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

        code = run_command(
            [tool, "plan", "-input=false", "-detailed-exitcode", "-out=plan.tfplan"],
            cwd,
            platform_env,
            phase="planning",
            section=None,
            streamer=streamer,
        )
        # detailed-exitcode: 0 = no changes, 2 = changes, 1 = error.
        if code == 1:
            return client.event(job_id, "job_failed", phase="plan", result={"error": "plan failed"})
        has_changes = code == 2

        show = subprocess.run(
            [tool, "show", "-json", "plan.tfplan"],
            cwd=cwd,
            env=platform_env,
            capture_output=True,
            text=True,
        )
        plan_json = json.loads(show.stdout) if show.returncode == 0 and show.stdout else {}
        (cwd / "plan.json").write_text(json.dumps(plan_json))
        client.upload_artifact(job_id, "plan.json", show.stdout.encode())

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
                "summary": _plan_summary(plan_json),
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
            env_info["repo_url"], env_info.get("commit_sha"), env_info["project_root"]
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
        if (
            run_command(
                [tool, "apply", "-input=false", "-auto-approve"],
                cwd,
                platform_env,
                phase="applying",
                section=None,
                streamer=streamer,
            )
            != 0
        ):
            return client.event(
                job_id, "job_failed", phase="apply", result={"error": "apply failed"}
            )
        out = subprocess.run(
            [tool, "output", "-json"], cwd=cwd, env=platform_env, capture_output=True, text=True
        )
        client.upload_artifact(job_id, "outputs.json", out.stdout.encode())
        outputs = json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else {}
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

    last_heartbeat = 0.0
    while True:
        now = time.monotonic()
        if now - last_heartbeat >= settings.heartbeat_interval:
            for cmd in client.heartbeat():
                _handle_command(client, cmd, settings)
            last_heartbeat = now
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
            else:
                handle_apply(client, job, settings)
            log.info("job done", extra={"event": "agent.done", "run_id": job["job_id"]})
        except Exception as exc:  # noqa: BLE001 — report and keep polling
            log.exception("job failed", extra={"event": "agent.failed", "run_id": job["job_id"]})
            client.event(
                job["job_id"], "job_failed", phase=job["phase"], result={"error": str(exc)}
            )


if __name__ == "__main__":
    run()
