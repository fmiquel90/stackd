import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient } from "@tanstack/react-query";
import { ApiError, api, getAccessToken, retryQuery, setAccessToken } from "@/api/client";
import type { User } from "@/api/types";
import { bindSessionCache } from "./session";

const USER: User = {
  id: "u1",
  kind: "human",
  email: "alice@dev.local",
  display_name: null,
  avatar_url: null,
  role: "approver",
  allowed_tiers: ["dev"],
  can_destroy: false,
  disabled: false,
  onboarded: true,
  last_login_at: null,
};

/** An unsigned access JWT: the client only reads `iat`/`exp` to time its refresh. */
function jwt(name: string, ttl = 900): string {
  const b64 = (o: object) => btoa(JSON.stringify(o)).replace(/=+$/, "");
  const iat = Math.floor(Date.now() / 1000);
  return `${b64({ alg: "HS256" })}.${b64({ sub: name, iat, exp: iat + ttl })}.sig`;
}

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

/** A fake API: `/auth/refresh` answers `refresh()`, anything else needs the `valid` token. */
function fakeApi(refresh: () => Response, valid: () => string) {
  const calls: { path: string; auth: string | null; csrf: string | null }[] = [];
  const fetchMock = vi.fn(async (url: string, init: RequestInit) => {
    const headers = new Headers(init.headers);
    const path = url.replace("/api/v1", "");
    calls.push({ path, auth: headers.get("Authorization"), csrf: headers.get("X-CSRF-Token") });
    await Promise.resolve();
    if (path === "/auth/refresh") return refresh();
    return headers.get("Authorization") === `Bearer ${valid()}`
      ? json(200, { ok: path })
      : json(401, { title: "Not authenticated" });
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

let unbind: (() => void) | undefined;
beforeEach(() => {
  document.cookie = "stackd_csrf=c1; path=/";
});
afterEach(() => {
  unbind?.();
  setAccessToken(null);
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("expired access token (401 → refresh → replay)", () => {
  it("refreshes once and replays the request with the new token", async () => {
    const fresh = jwt("fresh");
    setAccessToken(jwt("stale"));
    const calls = fakeApi(
      () => json(200, { access_token: fresh, user: USER }),
      () => fresh,
    );

    await expect(api("/runs/r1")).resolves.toEqual({ ok: "/runs/r1" });
    expect(calls.map((c) => c.path)).toEqual(["/runs/r1", "/auth/refresh", "/runs/r1"]);
    expect(calls[1].csrf).toBe("c1"); // the refresh keeps the double-submit header
    expect(calls[2].auth).toBe(`Bearer ${fresh}`);
    expect(getAccessToken()).toBe(fresh);
  });

  it("shares one refresh between concurrent 401s", async () => {
    const fresh = jwt("fresh");
    setAccessToken(jwt("stale"));
    const calls = fakeApi(
      () => json(200, { access_token: fresh, user: USER }),
      () => fresh,
    );

    const results = await Promise.all(["/a", "/b", "/c"].map((p) => api(p)));
    expect(results).toEqual([{ ok: "/a" }, { ok: "/b" }, { ok: "/c" }]);
    expect(calls.filter((c) => c.path === "/auth/refresh")).toHaveLength(1);
  });

  it("loses the session when the refresh is refused, without replaying", async () => {
    setAccessToken(jwt("stale"));
    const calls = fakeApi(
      () => json(401, { title: "Invalid refresh token" }),
      () => "never",
    );
    const qc = new QueryClient();
    qc.setQueryData(["session"], USER);
    unbind = bindSessionCache(qc);

    const err = (await api("/runs/r1").catch((e) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(401);
    expect(calls.map((c) => c.path)).toEqual(["/runs/r1", "/auth/refresh"]);
    expect(getAccessToken()).toBeNull();
    expect(qc.getQueryData(["session"])).toBeNull(); // → the login page
  });

  it("does not refresh a 401 on a request sent without a token", async () => {
    const calls = fakeApi(
      () => json(200, { access_token: jwt("x"), user: USER }),
      () => "never",
    );
    await expect(api("/auth/me")).rejects.toMatchObject({ status: 401 });
    expect(calls.map((c) => c.path)).toEqual(["/auth/me"]);
  });

  it("refreshes ahead of expiry, at ~80% of the token's lifetime", async () => {
    vi.useFakeTimers();
    const fresh = jwt("fresh");
    const calls = fakeApi(
      () => json(200, { access_token: fresh, user: USER }),
      () => fresh,
    );
    const qc = new QueryClient();
    unbind = bindSessionCache(qc);
    setAccessToken(jwt("first", 900));

    await vi.advanceTimersByTimeAsync(700_000);
    expect(calls).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(calls.map((c) => c.path)).toEqual(["/auth/refresh"]);
    expect(getAccessToken()).toBe(fresh);
    expect(qc.getQueryData(["session"])).toEqual(USER);
  });
});

describe("query retries", () => {
  it("never retries a refusal, retries a server or network error twice", () => {
    expect(retryQuery(0, new ApiError(401, "Not authenticated"))).toBe(false);
    expect(retryQuery(0, new ApiError(403, "Forbidden"))).toBe(false);
    expect(retryQuery(0, new ApiError(404, "Not found"))).toBe(false);
    expect(retryQuery(0, new ApiError(503, "Unavailable"))).toBe(true);
    expect(retryQuery(1, new TypeError("Failed to fetch"))).toBe(true);
    expect(retryQuery(2, new ApiError(500, "Internal"))).toBe(false);
  });
});
