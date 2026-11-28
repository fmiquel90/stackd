import type { DevPersona, Session, User } from "./types";

// Access token lives in memory only — never browser storage (CLAUDE invariant #8 / DESIGN §8).
let accessToken: string | null = null;
let proactiveRefresh: ReturnType<typeof setTimeout> | undefined;

/** The `iat`/`exp` of a JWT, read without verification (only to time the next refresh). */
function tokenTimes(token: string): { iat: number; exp: number } | null {
  try {
    const b64 = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    const { iat, exp } = JSON.parse(atob(b64)) as { iat?: unknown; exp?: unknown };
    return typeof iat === "number" && typeof exp === "number" && exp > iat ? { iat, exp } : null;
  } catch {
    return null;
  }
}

export function setAccessToken(token: string | null): void {
  accessToken = token;
  clearTimeout(proactiveRefresh);
  proactiveRefresh = undefined;
  const times = token ? tokenTimes(token) : null;
  if (!times) return;
  // At ~80% of the token's lifetime, counted from now rather than from `iat`: the token was just
  // issued, and the browser's clock may disagree with the API's. A failure is left to the 401 path.
  const delay = Math.max(10_000, 0.8 * (times.exp - times.iat) * 1000);
  proactiveRefresh = setTimeout(() => void refreshSession().catch(() => undefined), delay);
}

export function getAccessToken(): string | null {
  return accessToken;
}

function readCsrfCookie(): string | null {
  const match = document.cookie.match(/(?:^|;\s*)stackd_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : null;
}

/** One entry of a 422's `errors` (FastAPI validation): where, and what is wrong. */
export interface ProblemFieldError {
  loc: (string | number)[];
  msg: string;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public title: string,
    public detail?: string,
    /** The field errors of a 422 (problem+json `errors`), when the API lists them. */
    public errors?: ProblemFieldError[],
  ) {
    super(detail ?? title);
  }
}

// Only `loc` and `msg` are kept: a validation error also echoes the offending `input`, which may be
// a value the user supplied (a secret in an imported document) — it is never surfaced.
function fieldErrors(data: unknown): ProblemFieldError[] | undefined {
  const raw = (data as { errors?: unknown } | null)?.errors;
  if (!Array.isArray(raw)) return undefined;
  return raw.flatMap((e: { loc?: unknown; msg?: unknown } | null) =>
    e && Array.isArray(e.loc) && typeof e.msg === "string"
      ? [{ loc: e.loc as (string | number)[], msg: e.msg }]
      : [],
  );
}

function problem(resp: Response, data: { title?: string; detail?: string } | null): ApiError {
  return new ApiError(resp.status, data?.title ?? resp.statusText, data?.detail, fieldErrors(data));
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  /** Send the double-submit CSRF header (cookie-borne endpoints: /auth/refresh, /auth/logout). */
  csrf?: boolean;
  /** Read the response headers (a paged list's `X-Next-Cursor`). */
  onHeaders?: (headers: Headers) => void;
  /** No refresh-and-replay on a 401: the endpoints that open, renew or close the session. */
  noAuthRetry?: boolean;
}

type SessionListener = (user: User | null) => void;
const sessionListeners = new Set<SessionListener>();

/** Told the user after each refresh, and `null` once the session is lost (the refresh refused). */
export function onSessionChange(listener: SessionListener): () => void {
  sessionListeners.add(listener);
  return () => sessionListeners.delete(listener);
}

let refreshing: Promise<Session> | null = null;

/** Exchange the refresh cookie for a new access token; concurrent callers share one request. */
export function refreshSession(): Promise<Session> {
  refreshing ??= (async () => {
    try {
      const session = await api<Session>("/auth/refresh", { method: "POST", csrf: true, noAuthRetry: true });
      setAccessToken(session.access_token);
      sessionListeners.forEach((l) => l(session.user));
      return session;
    } catch (err) {
      if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
        setAccessToken(null);
        sessionListeners.forEach((l) => l(null));
      }
      throw err;
    } finally {
      refreshing = null;
    }
  })();
  return refreshing;
}

/**
 * Fetch with the current access token. A 401 on a request that carried one means the token
 * expired: one shared refresh, then a single replay with the new token. A refused refresh loses
 * the session, and the 401 is returned as is.
 */
async function send(path: string, init: RequestInit, opts: RequestOptions = {}): Promise<Response> {
  const attempt = () => {
    const headers = new Headers(init.headers);
    if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
    if (opts.csrf) {
      const csrf = readCsrfCookie();
      if (csrf) headers.set("X-CSRF-Token", csrf);
    }
    return fetch(`/api/v1${path}`, { ...init, headers, credentials: "include" });
  };
  const sentWith = accessToken;
  const resp = await attempt();
  if (resp.status !== 401 || opts.noAuthRetry || !sentWith) return resp;
  // Another request may have refreshed meanwhile: then only the replay is needed.
  if (accessToken === sentWith) {
    try {
      await refreshSession();
    } catch {
      return resp;
    }
  }
  return accessToken ? attempt() : resp;
}

export async function api<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const resp = await send(
    path,
    {
      method: opts.method ?? (opts.body !== undefined ? "POST" : "GET"),
      headers: opts.body !== undefined ? { "Content-Type": "application/json" } : {},
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    },
    opts,
  );

  opts.onHeaders?.(resp.headers);
  if (resp.status === 204) return undefined as T;
  const data = resp.headers.get("content-type")?.includes("json") ? await resp.json() : null;
  if (!resp.ok) throw problem(resp, data);
  return data as T;
}

/** Query retry policy: a refusal (4xx) is final; a server or network error gets 2 more tries. */
export function retryQuery(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status < 500) return false;
  return failureCount < 2;
}

// Authenticated GET returning the raw body (for file downloads like the audit CSV export, which
// is served as text/csv — not JSON — so it can't go through api<T>()).
export async function apiBlob(path: string): Promise<Blob> {
  const resp = await send(path, {});
  if (!resp.ok) {
    // Errors come back as problem+json even on a blob endpoint — surface the detail, not bare status.
    const data = resp.headers.get("content-type")?.includes("json") ? await resp.json() : null;
    throw problem(resp, data);
  }
  return resp.blob();
}

/** The file name a `Content-Disposition: attachment; filename="…"` header gives, if any. */
export function attachmentName(header: string | null): string | null {
  // RFC 5987 `filename*=UTF-8''…` first: it carries the exact name (any script); `filename="…"`
  // is the server's ASCII-safe fallback (RFC 6266).
  const exact = header?.match(/filename\*=UTF-8''([^;\s]+)/i);
  if (exact) {
    try {
      return decodeURIComponent(exact[1]);
    } catch {
      // malformed percent-encoding: fall back to the plain name
    }
  }
  const m = header?.match(/filename="?([^";]+)"?/i);
  return m ? m[1] : null;
}

/** Authenticated GET of a download: the body and the server's file name (Content-Disposition). */
export async function apiFile(path: string): Promise<{ blob: Blob; filename: string | null }> {
  const resp = await send(path, {});
  if (!resp.ok) {
    const data = resp.headers.get("content-type")?.includes("json") ? await resp.json() : null;
    throw problem(resp, data);
  }
  return { blob: await resp.blob(), filename: attachmentName(resp.headers.get("content-disposition")) };
}

/** Hand a blob to the browser as a download: an object URL, revoked at once — nothing is stored. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export const auth = {
  refresh: refreshSession,
  async me(): Promise<User> {
    return api<User>("/auth/me");
  },
  async markOnboarded(): Promise<User> {
    return api<User>("/auth/me/onboarded", { method: "POST" });
  },
  async devPersonas(): Promise<{ personas: DevPersona[] }> {
    return api("/auth/dev/personas");
  },
  async devLogin(persona: string): Promise<Session> {
    const session = await api<Session>("/auth/dev/login", { body: { persona }, noAuthRetry: true });
    setAccessToken(session.access_token);
    return session;
  },
  async logout(): Promise<void> {
    await api<void>("/auth/logout", { method: "POST", csrf: true, noAuthRetry: true });
    setAccessToken(null);
  },
  googleStartUrl: "/api/v1/auth/google/start",
};
