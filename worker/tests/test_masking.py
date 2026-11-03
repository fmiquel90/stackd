from __future__ import annotations

import json

from agent.main import _first_error, _forgets_in_plan, _merge_hooks, _summary_from_events
from agent.masking import Masker, mask_plan_json


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
            "changes": {
                "add": 3,
                "change": 1,
                "remove": 2,
                "import": 1,
                "forget": 1,
                "operation": "plan",
            },
        },
    ]
    # Every counter: a rollout never confirms a plan that adopts or forgets resources (§21.5).
    assert _summary_from_events(events) == {
        "add": 3,
        "change": 1,
        "destroy": 2,
        "import": 1,
        "forget": 1,
    }


def test_summary_keeps_counters_a_newer_tool_reports() -> None:
    """Terraform's actions (1.14+): reported as is, so the gate can refuse what it doesn't know."""
    events = [
        {
            "type": "change_summary",
            "changes": {
                "add": 1,
                "change": 0,
                "remove": 0,
                "import": 0,
                "action_invocation": 2,
                "action_fail": 0,
                "operation": "plan",
            },
        }
    ]
    assert _summary_from_events(events) == {
        "add": 1,
        "change": 0,
        "destroy": 0,
        "import": 0,
        "forget": 0,
        "action_invocation": 2,
        "action_fail": 0,
    }


def test_summary_defaults_when_no_change_summary() -> None:
    assert _summary_from_events([{"type": "version", "@message": "Terraform v1.12"}]) == {
        "add": 0,
        "change": 0,
        "destroy": 0,
        "import": 0,
        "forget": 0,
    }


def test_forgets_are_counted_from_the_plan_json() -> None:
    """Terraform's `change_summary` has no `forget` counter: the plan JSON says it."""
    doc = {
        "resource_changes": [
            {"address": "a.x", "change": {"actions": ["forget"]}},
            {"address": "a.y", "change": {"actions": ["create", "forget"]}},
            {"address": "a.z", "change": {"actions": ["create"]}},
        ]
    }
    assert _forgets_in_plan(json.dumps(doc)) == 2
    assert _forgets_in_plan("not json") == 0


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


def _tofu_json(doc: dict) -> str:
    """JSON as tofu writes it: Go escapes <, > and & in strings."""
    return json.dumps(doc).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _plan() -> dict:
    """The parts of a `tofu show -json` plan that carry values (shapes from tofu 1.12)."""
    db = {"password": "pw-new", "region": "eu-west-1", "port": 4242}
    return {
        "format_version": "1.2",
        "variables": {
            "db_password": {"value": "pw-new"},
            "region": {"value": "eu-west-1"},
            "api_key": {"value": "key-<&>-9"},  # sensitive for Stackd, not declared so by the repo
        },
        "planned_values": {
            "outputs": {
                "pw": {"sensitive": True, "type": "string", "value": "pw-new"},
                "region": {"sensitive": False, "type": "string", "value": "eu-west-1"},
            },
            "root_module": {
                "resources": [
                    {
                        "address": "terraform_data.db",
                        "values": {"input": db},
                        "sensitive_values": {"input": {"password": True}},
                    }
                ],
                "child_modules": [
                    {
                        "address": "module.app",
                        "resources": [
                            {
                                "address": "module.app.terraform_data.x",
                                "values": {"token": "tok-2", "tags": ["app", "tok-2"]},
                                "sensitive_values": {"token": True, "tags": [False, True]},
                            }
                        ],
                    }
                ],
            },
        },
        "resource_changes": [
            {
                "address": "terraform_data.db",
                "change": {
                    "actions": ["update"],
                    "before": {"input": {**db, "password": "pw-old"}},
                    "after": {"input": db},
                    "before_sensitive": {"input": {"password": True}},
                    "after_sensitive": {"input": {"password": True}},
                },
            }
        ],
        "output_changes": {
            "pw": {
                "actions": ["create"],
                "before": None,
                "after": "pw-new",
                "before_sensitive": True,
                "after_sensitive": True,
            },
            "region": {
                "actions": ["create"],
                "before": None,
                "after": "eu-west-1",
                "before_sensitive": False,
                "after_sensitive": False,
            },
        },
        "prior_state": {
            "values": {"outputs": {"pw": {"sensitive": True, "value": "pw-old"}}, "root_module": {}}
        },
        "configuration": {
            "root_module": {
                "variables": {
                    "db_password": {"type": "string", "sensitive": True},
                    "region": {"type": "string"},
                    "api_key": {"type": "string"},
                }
            }
        },
    }


def test_mask_plan_json_hides_sensitive_values_and_keeps_the_rest() -> None:
    # tofu's own marks hide pw-new/pw-old/tok-2; the log masker hides what only Stackd knows is
    # sensitive (api_key, escaped by tofu as key-<&>-9; and a number).
    masked = mask_plan_json(_tofu_json(_plan()), Masker(["key-<&>-9", "4242"]))

    for secret in ("pw-new", "pw-old", "tok-2", "key-", "4242"):
        assert secret not in masked, secret
    expected = _plan()
    expected["variables"]["db_password"]["value"] = "***"
    expected["variables"]["api_key"]["value"] = "***"
    expected["planned_values"]["outputs"]["pw"]["value"] = "***"
    db = expected["planned_values"]["root_module"]["resources"][0]["values"]["input"]
    db.update(password="***", port="***")
    app = expected["planned_values"]["root_module"]["child_modules"][0]["resources"][0]["values"]
    app.update(token="***", tags=["app", "***"])
    change = expected["resource_changes"][0]["change"]
    change["before"]["input"].update(password="***", port="***")
    change["after"]["input"].update(password="***", port="***")
    expected["output_changes"]["pw"]["after"] = "***"  # a sensitive null stays null
    expected["prior_state"]["values"]["outputs"]["pw"]["value"] = "***"
    assert json.loads(masked) == expected


def test_mask_plan_json_masks_a_non_json_document_as_text() -> None:
    assert mask_plan_json("Error: pw-new is wrong", Masker(["pw-new"])) == "Error: *** is wrong"
