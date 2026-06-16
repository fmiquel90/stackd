from agent.main import _init_cmd


def test_init_managed_backend_uses_http_config():
    cmd = _init_cmd("tofu", {"address": "https://api/state/v1/e", "lock_method": "POST"})
    assert cmd[:3] == ["tofu", "init", "-input=false"]
    assert "-backend-config=address=https://api/state/v1/e" in cmd
    assert "-backend-config=lock_method=POST" in cmd


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
