from __future__ import annotations

from agent.main import _merge_hooks, _plan_summary
from agent.masking import Masker


def test_masker_replaces_all_secrets() -> None:
    m = Masker(["s3cr3t", "tok_ABC"])
    out = m.mask("connecting with s3cr3t and tok_ABC now")
    assert "s3cr3t" not in out
    assert "tok_ABC" not in out
    assert out.count("***") == 2


def test_masker_longest_first() -> None:
    # Overlapping secrets: the longer one must be fully masked, not partially.
    m = Masker(["abc", "abcdef"])
    assert m.mask("value=abcdef") == "value=***"


def test_merge_hooks_platform_before_repo() -> None:
    platform = {"after_plan": [{"name": "tfsec", "source": "platform"}]}
    repo = {"after_plan": [{"name": "infracost", "source": "repo"}]}
    merged = _merge_hooks(platform, repo)
    names = [h["name"] for h in merged["after_plan"]]
    assert names == ["tfsec", "infracost"]  # platform (non-bypassable) runs first


def test_plan_summary_counts_actions() -> None:
    plan = {
        "resource_changes": [
            {"change": {"actions": ["create"]}},
            {"change": {"actions": ["update"]}},
            {"change": {"actions": ["delete"]}},
            {"change": {"actions": ["no-op"]}},
        ]
    }
    assert _plan_summary(plan) == {"add": 1, "change": 1, "destroy": 1}
