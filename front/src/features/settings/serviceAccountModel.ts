import type { ApiToken } from "@/api/types";

// Service accounts & API tokens (SPECS §23): the rules the UI mirrors before the API checks them.

/** A service account's name (also the local part of its `…@service-accounts.stackd.invalid`). */
export const SERVICE_ACCOUNT_NAME = /^[a-z0-9][a-z0-9-]{0,62}$/;

/** The email domain the API gives every service account (RFC 2606: never deliverable). */
export const SERVICE_ACCOUNT_DOMAIN = "service-accounts.stackd.invalid";

export const TOKEN_DAYS = { min: 1, max: 365, default: 90 } as const;

export type TokenStatus = "active" | "expired" | "revoked";

export function tokenStatus(t: Pick<ApiToken, "expires_at" | "revoked_at">, now = new Date()): TokenStatus {
  if (t.revoked_at) return "revoked";
  return new Date(t.expires_at).getTime() <= now.getTime() ? "expired" : "active";
}

/** Whole days until an active token expires (0 = within a day). */
export function daysLeft(expiresAt: string, now = new Date()): number {
  return Math.max(0, Math.floor((new Date(expiresAt).getTime() - now.getTime()) / 86_400_000));
}

/** A valid `expires_in_days` from what was typed, or null. */
export function parseDays(raw: string): number | null {
  if (!/^\d+$/.test(raw.trim())) return null;
  const n = Number(raw);
  return n >= TOKEN_DAYS.min && n <= TOKEN_DAYS.max ? n : null;
}
