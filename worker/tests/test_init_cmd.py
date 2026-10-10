from agent.main import _init_cmd


def test_init_managed_backend_passes_no_backend_config():
    # The managed HTTP backend is configured through TF_HTTP_* (see test_backend_env): anything
    # passed as -backend-config is persisted under .terraform/.
    cmd = _init_cmd("tofu", {"address": "https://api/state/v1/e", "lock_method": "POST"})
    assert cmd == ["tofu", "init", "-input=false"]


def test_init_unmanaged_with_backend_config_file():
    cmd = _init_cmd("terraform", None, "prod.config")
    assert cmd == ["terraform", "init", "-input=false", "-backend-config=prod.config"]


def test_init_managed_takes_precedence_over_file():
    # A managed env uses the platform HTTP backend; the file is ignored.
    cmd = _init_cmd("tofu", {"address": "https://api/state/v1/e"}, "prod.config")
    assert "-backend-config=prod.config" not in cmd


def test_init_unmanaged_with_inline_key_values():
    cmd = _init_cmd("terraform", None, None, {"bucket": "tf-state", "key": "prod/app.tfstate"})
    assert "-backend-config=bucket=tf-state" in cmd
    assert "-backend-config=key=prod/app.tfstate" in cmd


def test_init_unmanaged_file_and_inline_combined():
    cmd = _init_cmd("terraform", None, "base.config", {"key": "staging/app.tfstate"})
    # File first, then inline overrides (terraform: later -backend-config wins).
    assert cmd.index("-backend-config=base.config") < cmd.index(
        "-backend-config=key=staging/app.tfstate"
    )


def test_init_plain_when_nothing_set():
    assert _init_cmd("tofu", None) == ["tofu", "init", "-input=false"]
