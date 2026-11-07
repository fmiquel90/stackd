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
  const data = resp.headers.get("content-type")?.includes("json") ? await resp.json() : null;
  if (!resp.ok) throw problem(resp, data);
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
  const headers: Record<string, string> = {};
  if (accessToken) headers["Authorization"] = `Bearer ${accessToken}`;
  const resp = await fetch(`/api/v1${path}`, { headers, credentials: "include" });
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
