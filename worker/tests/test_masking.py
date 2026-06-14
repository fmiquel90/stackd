from __future__ import annotations

from agent.main import _first_error, _merge_hooks, _summary_from_events
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


def test_summary_from_change_summary_event() -> None:
    events = [
        {"type": "planned_change", "@message": "aws_vpc.main: Plan to create"},
        {
            "type": "change_summary",
            "@message": "Plan: 3 to add, 1 to change, 2 to destroy.",
            "changes": {"add": 3, "change": 1, "remove": 2, "import": 0, "operation": "plan"},
        },
    ]
    assert _summary_from_events(events) == {"add": 3, "change": 1, "destroy": 2}


def test_summary_defaults_when_no_change_summary() -> None:
    assert _summary_from_events([{"type": "version", "@message": "Terraform v1.12"}]) == {
        "add": 0,
        "change": 0,
        "destroy": 0,
    }


def test_first_error_picks_the_diagnostic_summary() -> None:
    events = [
        {"@level": "info", "@message": "Initializing…"},
        {
            "@level": "error",
            "@message": "Error: Reference to undeclared resource",
            "type": "diagnostic",
            "diagnostic": {"severity": "error", "summary": "Reference to undeclared resource"},
        },
    ]
    assert _first_error(events) == "Reference to undeclared resource"
    assert _first_error([{"@level": "info", "@message": "ok"}]) is None
