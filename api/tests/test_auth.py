from __future__ import annotations

import re

import httpx
import pytest


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
    # Mismatched CSRF header → 403 too.
    bad = await client.post(
        "/api/v1/auth/refresh", headers={"Cookie": cookie, "X-CSRF-Token": "nope"}
    )
    assert bad.status_code == 403


async def test_refresh_without_session_is_401(client: httpx.AsyncClient) -> None:
    # Fresh visit: no cookies at all → 401 "not authenticated", not a CSRF 403.
    r = await client.post("/api/v1/auth/refresh")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
    # A CSRF pair without a refresh cookie is still just "not authenticated".
    r2 = await client.post(
        "/api/v1/auth/refresh",
        headers={"Cookie": "stackd_csrf=abc", "X-CSRF-Token": "abc"},
    )
    assert r2.status_code == 401


async def test_refresh_rotation_and_reuse_detection(client: httpx.AsyncClient) -> None:
    login = await client.post("/api/v1/auth/dev/login", json={"persona": "admin"})
    ck = _set_cookies(login)
    old_refresh, csrf = ck["stackd_refresh"], ck["stackd_csrf"]

    r2 = await client.post("/api/v1/auth/refresh", headers=_auth_cookie_header(old_refresh, csrf))
    assert r2.status_code == 200
    ck2 = _set_cookies(r2)
    new_refresh, new_csrf = ck2["stackd_refresh"], ck2["stackd_csrf"]
    assert new_refresh != old_refresh  # rotated

    # Past the grace window, reusing the consumed token is detected → 401 and the whole family
    # is revoked (SPECS §2.5).
    from sqlalchemy import text

    from app.db import SessionLocal
    from app.security import hash_token

    async with SessionLocal() as s:
        await s.execute(
            text(
                "UPDATE refresh_tokens SET used_at = now() - interval '31 seconds' "
                "WHERE token_hash = :h"
            ),
            {"h": hash_token(old_refresh)},
        )
        await s.commit()
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


def test_google_admission_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """§2.1 step 5: a verified email of an allowed `hd` only. An empty allowlist admits nobody —
    never "any verified Google account" (a personal account has no `hd` at all)."""
    from app.auth import google
    from app.config import Settings
    from app.errors import ProblemException

    def _admit(domains: str, **claims: object) -> bool:
        settings = Settings(stackd_allowed_domains=domains)  # type: ignore[call-arg]
        monkeypatch.setattr(google, "get_settings", lambda: settings)
        try:
            google.admit({"email_verified": True, **claims})
        except ProblemException as exc:
            assert exc.status == 403
            return False
        return True

    assert _admit("acme.com, other.org", hd="acme.com")
    assert not _admit("acme.com", hd="evil.com")
    assert not _admit("acme.com")  # a personal account (no hd)
    assert not _admit("", hd="acme.com")  # nothing configured → nobody
    assert not _admit("")
    assert not _admit("acme.com", hd="acme.com", email_verified=False)


async def _family(refresh: str) -> tuple[int, int]:
    """(rows, revoked rows) of the family the given refresh token belongs to."""
    from sqlalchemy import text

    from app.db import SessionLocal
    from app.security import hash_token

    async with SessionLocal() as s:
        row = (
            await s.execute(
                text(
                    "SELECT count(*), count(revoked_at) FROM refresh_tokens WHERE family_id = "
                    "(SELECT family_id FROM refresh_tokens WHERE token_hash = :h)"
                ),
                {"h": hash_token(refresh)},
            )
        ).one()
    return row[0], row[1]


async def test_refresh_reuse_within_grace_keeps_the_session(client: httpx.AsyncClient) -> None:
    """Two tabs share the refresh cookie: the second one's refresh, sent before the first one's
    rotated cookie landed, presents the parent token. Within the grace window it gets an access
    token, sets no cookie, and the family is neither revoked nor forked (SPECS §2.5)."""
    login = await client.post("/api/v1/auth/dev/login", json={"persona": "admin"})
    ck = _set_cookies(login)
    parent = _auth_cookie_header(ck["stackd_refresh"], ck["stackd_csrf"])

    first = await client.post("/api/v1/auth/refresh", headers=parent)
    assert first.status_code == 200
    second = await client.post("/api/v1/auth/refresh", headers=parent)
    assert second.status_code == 200
    assert second.json()["access_token"]
    assert second.headers.get_list("set-cookie") == []
    me = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {second.json()['access_token']}"}
    )
    assert me.status_code == 200

    child = _set_cookies(first)
    assert await _family(child["stackd_refresh"]) == (2, 0)
    after = await client.post(
        "/api/v1/auth/refresh",
        headers=_auth_cookie_header(child["stackd_refresh"], child["stackd_csrf"]),
    )
    assert after.status_code == 200


async def test_concurrent_refreshes_do_not_fork_the_family(client: httpx.AsyncClient) -> None:
    import asyncio

    login = await client.post("/api/v1/auth/dev/login", json={"persona": "admin"})
    ck = _set_cookies(login)
    h = _auth_cookie_header(ck["stackd_refresh"], ck["stackd_csrf"])
    rs = await asyncio.gather(*(client.post("/api/v1/auth/refresh", headers=h) for _ in range(3)))
    assert [r.status_code for r in rs] == [200, 200, 200]
    rotated = [r for r in rs if r.headers.get_list("set-cookie")]
    assert len(rotated) == 1  # one child minted, the others served in the grace window
    assert await _family(ck["stackd_refresh"]) == (2, 0)
