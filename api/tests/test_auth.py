from __future__ import annotations

import re

import httpx


def _set_cookies(resp: httpx.Response) -> dict[str, str]:
    out: dict[str, str] = {}
    for sc in resp.headers.get_list("set-cookie"):
        m = re.match(r"([^=]+)=([^;]*)", sc)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _auth_cookie_header(refresh: str, csrf: str) -> dict[str, str]:
    return {
        "Cookie": f"stackd_refresh={refresh}; stackd_csrf={csrf}",
        "X-CSRF-Token": csrf,
    }


async def test_dev_login_personas(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/v1/auth/dev/login", json={"persona": "bob"})
    assert r.status_code == 200
    user = r.json()["user"]
    assert user["email"] == "bob@dev.local"
    assert user["role"] == "writer"
    assert user["allowed_tiers"] == ["dev", "staging"]  # bob cannot confirm prod (§2.4)

    me = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["email"] == "bob@dev.local"


async def test_refresh_requires_csrf(client: httpx.AsyncClient) -> None:
    login = await client.post("/api/v1/auth/dev/login", json={"persona": "alice"})
    ck = _set_cookies(login)
    # Missing CSRF header → 403 (double-submit guard, §2.1).
    cookie = f"stackd_refresh={ck['stackd_refresh']}; stackd_csrf={ck['stackd_csrf']}"
    no_csrf = await client.post("/api/v1/auth/refresh", headers={"Cookie": cookie})
    assert no_csrf.status_code == 403


async def test_refresh_rotation_and_reuse_detection(client: httpx.AsyncClient) -> None:
    login = await client.post("/api/v1/auth/dev/login", json={"persona": "admin"})
    ck = _set_cookies(login)
    old_refresh, csrf = ck["stackd_refresh"], ck["stackd_csrf"]

    r2 = await client.post("/api/v1/auth/refresh", headers=_auth_cookie_header(old_refresh, csrf))
    assert r2.status_code == 200
    ck2 = _set_cookies(r2)
    new_refresh, new_csrf = ck2["stackd_refresh"], ck2["stackd_csrf"]
    assert new_refresh != old_refresh  # rotated

    # Reusing the consumed token is detected → 401 and the whole family is revoked.
    reuse = await client.post(
        "/api/v1/auth/refresh", headers=_auth_cookie_header(old_refresh, csrf)
    )
    assert reuse.status_code == 401

    after = await client.post(
        "/api/v1/auth/refresh", headers=_auth_cookie_header(new_refresh, new_csrf)
    )
    assert after.status_code == 401  # family revoked


async def test_onboarding_flag_persists(client: httpx.AsyncClient) -> None:
    login = await client.post("/api/v1/auth/dev/login", json={"persona": "bob"})
    token = login.json()["access_token"]
    bearer = {"Authorization": f"Bearer {token}"}
    assert login.json()["user"]["onboarded"] is False

    done = await client.post("/api/v1/auth/me/onboarded", headers=bearer)
    assert done.status_code == 200 and done.json()["onboarded"] is True

    me = await client.get("/api/v1/auth/me", headers=bearer)
    assert me.json()["onboarded"] is True  # persisted server-side


async def test_unauthenticated_me(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/auth/me")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
