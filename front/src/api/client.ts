import type { DevPersona, Session, User } from "./types";

// Access token lives in memory only — never browser storage (CLAUDE invariant #8 / DESIGN §8).
let accessToken: string | null = null;

export function setAccessToken(token: string | null): void {
  accessToken = token;
}

export function getAccessToken(): string | null {
  return accessToken;
}

function readCsrfCookie(): string | null {
  const match = document.cookie.match(/(?:^|;\s*)stackd_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : null;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public title: string,
    public detail?: string,
  ) {
    super(detail ?? title);
  }
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  /** Send the double-submit CSRF header (cookie-borne endpoints: /auth/refresh, /auth/logout). */
  csrf?: boolean;
}

export async function api<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = {};
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (accessToken) headers["Authorization"] = `Bearer ${accessToken}`;
  if (opts.csrf) {
    const csrf = readCsrfCookie();
    if (csrf) headers["X-CSRF-Token"] = csrf;
  }

  const resp = await fetch(`/api/v1${path}`, {
    method: opts.method ?? (opts.body !== undefined ? "POST" : "GET"),
    headers,
    credentials: "include",
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });

  if (resp.status === 204) return undefined as T;
  const data = resp.headers.get("content-type")?.includes("json")
    ? await resp.json()
    : null;
  if (!resp.ok) {
    throw new ApiError(resp.status, data?.title ?? resp.statusText, data?.detail);
  }
  return data as T;
}

// Authenticated GET returning the raw body (for file downloads like the audit CSV export, which
// is served as text/csv — not JSON — so it can't go through api<T>()).
export async function apiBlob(path: string): Promise<Blob> {
  const headers: Record<string, string> = {};
  if (accessToken) headers["Authorization"] = `Bearer ${accessToken}`;
  const resp = await fetch(`/api/v1${path}`, { headers, credentials: "include" });
  if (!resp.ok) {
    // Errors come back as problem+json even on a blob endpoint — surface the detail, not bare status.
    const data = resp.headers.get("content-type")?.includes("json") ? await resp.json() : null;
    throw new ApiError(resp.status, data?.title ?? resp.statusText, data?.detail);
  }
  return resp.blob();
}

export const auth = {
  async refresh(): Promise<Session> {
    const session = await api<Session>("/auth/refresh", { method: "POST", csrf: true });
    setAccessToken(session.access_token);
    return session;
  },
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
    const session = await api<Session>("/auth/dev/login", { body: { persona } });
    setAccessToken(session.access_token);
    return session;
  },
  async logout(): Promise<void> {
    await api<void>("/auth/logout", { method: "POST", csrf: true });
    setAccessToken(null);
  },
  googleStartUrl: "/api/v1/auth/google/start",
};
