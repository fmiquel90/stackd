import type { UserUpdate } from "@/api/resources";
import type { Role } from "@/api/types";

// Staged user edits (DESIGN §5.7): role, allowed tiers and can_destroy are changed in a draft and
// saved together, behind a summary in words ("bob: + prod in allowed tiers").

/** What a user holds — instance-wide (`User`) or in a space (`SpaceMember`). */
export interface Grant {
  email: string;
  role: Role;
  allowed_tiers: string[];
  can_destroy: boolean;
  disabled?: boolean;
}

/** Only the fields that differ from the user; `null` when the draft changes nothing. */
export function diffUser(user: Grant, draft: UserUpdate): UserUpdate | null {
  const out: UserUpdate = {};
  if (draft.role !== undefined && draft.role !== user.role) out.role = draft.role;
  if (draft.allowed_tiers !== undefined) {
    const a = [...draft.allowed_tiers].sort().join(",");
    const b = [...user.allowed_tiers].sort().join(",");
    if (a !== b) out.allowed_tiers = draft.allowed_tiers;
  }
  if (draft.can_destroy !== undefined && draft.can_destroy !== user.can_destroy)
    out.can_destroy = draft.can_destroy;
  if (draft.disabled !== undefined && draft.disabled !== user.disabled) out.disabled = draft.disabled;
  return Object.keys(out).length ? out : null;
}

/** One line per change, prefixed by the user's short name. */
export function describeChange(user: Grant, change: UserUpdate): string[] {
  const who = user.email.split("@")[0];
  const lines: string[] = [];
  if (change.role) lines.push(`${who}: role ${user.role} → ${change.role}`);
  if (change.allowed_tiers) {
    const added = change.allowed_tiers.filter((t) => !user.allowed_tiers.includes(t));
    const removed = user.allowed_tiers.filter((t) => !change.allowed_tiers!.includes(t));
    for (const t of added) lines.push(`${who}: + ${t} in allowed tiers`);
    for (const t of removed) lines.push(`${who}: − ${t} from allowed tiers`);
  }
  if (change.can_destroy !== undefined)
    lines.push(`${who}: ${change.can_destroy ? "may now destroy" : "may no longer destroy"}`);
  if (change.disabled !== undefined)
    lines.push(`${who}: ${change.disabled ? "disabled (cannot sign in)" : "re-enabled"}`);
  return lines;
}
