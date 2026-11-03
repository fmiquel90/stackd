import type { Rollout, RolloutMember, RolloutMemberStatus, RolloutState } from "@/api/resources";
import type { Tone } from "@/ui/primitives";

// Pure presentation logic of a rollout (SPECS §21, DESIGN §5.11): labels and tones never carry
// meaning by colour alone — every tone comes with its label and a glyph.

export const ROLLOUT_STATE: Record<RolloutState, { label: string; tone: Tone; glyph: string }> = {
  awaiting_approval: { label: "awaiting approval", tone: "decision", glyph: "◈" },
  running: { label: "running", tone: "running", glyph: "▸" },
  paused: { label: "paused", tone: "decision", glyph: "⏸" },
  succeeded: { label: "succeeded", tone: "finished", glyph: "✓" },
  failed: { label: "failed", tone: "failed", glyph: "✕" },
  canceled: { label: "canceled", tone: "neutral", glyph: "⊘" },
};

export const MEMBER_STATUS: Record<RolloutMemberStatus, { label: string; tone: Tone; glyph: string }> = {
  pending: { label: "waiting for its upstreams", tone: "queued", glyph: "○" },
  planning: { label: "planning", tone: "running", glyph: "▸" },
  needs_confirmation: { label: "needs a confirmation", tone: "decision", glyph: "◈" },
  applying: { label: "applying", tone: "running", glyph: "▸" },
  done: { label: "done", tone: "finished", glyph: "✓" },
  failed: { label: "failed", tone: "failed", glyph: "✕" },
  skipped: { label: "skipped (an upstream failed)", tone: "neutral", glyph: "⊘" },
  canceled: { label: "canceled", tone: "neutral", glyph: "⊘" },
};

export const PAUSE_REASON: Record<NonNullable<Rollout["pause_reason"]>, string> = {
  needs_confirmation: "a plan needs a human confirmation",
  approval_expired: "the approval expired: approve again to go on",
  approver_lost_rights: "the approver may no longer apply on a member: approve again",
};

/** A member's look; before the approval, a pending member is simply not started yet. */
export function memberLook(
  m: RolloutMember,
  state: RolloutState,
): { label: string; tone: Tone; glyph: string } {
  if (m.status === "pending" && (state === "awaiting_approval" || m.level === 0))
    return { label: "not started", tone: "queued", glyph: "○" };
  return MEMBER_STATUS[m.status];
}

/** Members grouped by topological level, level 0 first. */
export function byLevel(members: RolloutMember[]): RolloutMember[][] {
  const levels: RolloutMember[][] = [];
  for (const m of members) (levels[m.level] ??= []).push(m);
  return levels.filter(Boolean).map((l) => [...l].sort((a, b) => a.stack_name.localeCompare(b.stack_name)));
}

/** `+2 ~1 -0` for a member's plan, or null before it planned. */
export function summaryText(m: RolloutMember): string | null {
  const s = m.plan_summary;
  if (!s) return null;
  const parts = [`+${s.add ?? 0}`, `~${s.change ?? 0}`, `-${s.destroy ?? 0}`];
  if (s.import) parts.push(`⇣${s.import}`);
  return parts.join(" ");
}

/** Progress line: "3 / 5 done". */
export function progress(r: Pick<Rollout, "members">): string {
  const done = r.members.filter((m) => m.status === "done").length;
  return `${done} / ${r.members.length} done`;
}
