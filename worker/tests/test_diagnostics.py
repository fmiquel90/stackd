from __future__ import annotations

import json
import os

from agent.diagnostics import collect


def test_diagnostics_exposes_env_names_not_values() -> None:
    os.environ["DIAG_TEST_SECRET"] = "topsecret-value"
    bundle = collect(api_url="http://api:8000", runner="local")
    blob = json.dumps(bundle)

    assert "DIAG_TEST_SECRET" in bundle["env_var_names"]  # name is shown
    assert "topsecret-value" not in blob  # value is NEVER shown
    assert "recent_logs" in bundle and "disk" in bundle and "tools" in bundle
