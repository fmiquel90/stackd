"""Repo hooks at the apply stages run only on a tier that allows them (SPECS §8.3): elsewhere they
are skipped with a visible warning, and the platform hooks still run."""

from __future__ import annotations

from agent.main import _apply_repo_hook_policy

HOOKS = {
    "before_plan": [{"name": "repo-lint", "command": "true", "source": "repo"}],
    "before_apply": [
        {"name": "platform-gate", "command": "true", "source": "platform"},
        {"name": "repo-pre", "command": "true", "source": "repo"},
    ],
    "after_apply": [{"name": "repo-post", "command": "true", "source": "repo"}],
}


class _Streamer:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def emit(self, phase: str, lines: list[str], section: str | None = None) -> None:
        self.lines.extend(lines)


def test_tier_without_the_flag_skips_repo_apply_hooks() -> None:
    for job in ({"repo_hooks_at_apply": False}, {}):  # absent (an older API): fail closed
        checks: list[dict] = []
        streamer = _Streamer()
        out = _apply_repo_hook_policy(HOOKS, job, checks, streamer)  # type: ignore[arg-type]
        assert [h["name"] for h in out["before_apply"]] == ["platform-gate"]
        assert out["after_apply"] == []
        assert out["before_plan"] == HOOKS["before_plan"]  # plan stages untouched
        assert [(c["name"], c["status"], c["stage"]) for c in checks] == [
            ("repo-pre", "warn", "before_apply"),
            ("repo-post", "warn", "after_apply"),
        ]
        assert any("repo-pre" in line and "skipped" in line for line in streamer.lines)


def test_tier_with_the_flag_runs_them() -> None:
    checks: list[dict] = []
    out = _apply_repo_hook_policy(HOOKS, {"repo_hooks_at_apply": True}, checks, _Streamer())  # type: ignore[arg-type]
    assert out == HOOKS and checks == []
