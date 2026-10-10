import type { Run, RunState } from "@/api/types";

// The run vocabulary of DESIGN §3.2: what a run's state *means* to a reader, independent of styling.

export const TERMINAL_STATES: readonly RunState[] = ["finished", "failed", "discarded", "canceled"];
export const RUNNING_STATES: readonly RunState[] = [
  "preparing",
  "planning",
  "checking",
  "confirmed",
  "applying",
  "running",
];

export function isTerminal(state: RunState): boolean {
  return TERMINAL_STATES.includes(state);
}

/** Non-terminal = holds the environment (one active run per env, SPECS §3.5) — `queued` included. */
export function isActive(state: RunState): boolean {
  return !isTerminal(state);
}

export type Outcome =
  | "applied"
  | "no_changes"
  | "destroyed"
  | "planned"
  | "executed"
  | "failed"
  | "failed_during_apply"
  | "discarded"
  | "canceled";

/**
 * The terminal outcome of a run, or null while it is live. `confirmed_at` is set by a human confirm
 * and by the system auto-confirm alike, so it means "an apply was decided" (DESIGN §3.2).
 */
export function outcomeOf(run: Pick<Run, "state" | "type" | "confirmed_at">): Outcome | null {
  switch (run.state) {
    case "finished":
      if (run.type === "proposed") return "planned";
      if (run.type === "command") return "executed";
      if (run.type === "destroy") return run.confirmed_at ? "destroyed" : "no_changes";
      return run.confirmed_at ? "applied" : "no_changes";
    case "failed":
      // A failure after the apply was decided may have changed real infrastructure.
      return run.confirmed_at && run.type !== "proposed" ? "failed_during_apply" : "failed";
    case "discarded":
      return "discarded";
    case "canceled":
      return "canceled";
    default:
      return null;
  }
}

export interface PlanCounts {
  add: number;
  change: number;
  destroy: number;
}

export function planCounts(run: Pick<Run, "plan_summary">): PlanCounts | null {
  const s = run.plan_summary;
  if (!s) return null;
  return { add: s.add ?? 0, change: s.change ?? 0, destroy: s.destroy ?? 0 };
}

export function hasChanges(c: PlanCounts | null): boolean {
  return !!c && c.add + c.change + c.destroy > 0;
}

/** `+4 ~0 −1` — the minus is U+2212 so the three columns align in mono. */
export function formatCounts(c: PlanCounts): string {
  return `+${c.add} ~${c.change} −${c.destroy}`;
}
