from agent.workspace import _authed_url


def test_authed_url_injects_https_token():
    assert (
        _authed_url("https://github.com/acme/infra.git", "ghp_secret")
        == "https://x-access-token:ghp_secret@github.com/acme/infra.git"
    )


def test_authed_url_preserves_port_and_path():
    assert (
        _authed_url("https://git.example.com:8443/acme/infra", "tok")
        == "https://x-access-token:tok@git.example.com:8443/acme/infra"
    )


def test_authed_url_passthrough_without_token():
    url = "https://github.com/acme/infra.git"
    assert _authed_url(url, None) == url


def test_authed_url_passthrough_for_ssh_and_file():
    # Token only applies to http(s); SSH uses a deploy key, file:// fixtures need nothing.
    assert _authed_url("git@github.com:acme/infra.git", "tok") == "git@github.com:acme/infra.git"
    assert _authed_url("file:///srv/fixtures/infra", "tok") == "file:///srv/fixtures/infra"
