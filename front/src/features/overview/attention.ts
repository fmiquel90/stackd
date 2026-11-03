import type { Environment, Run, TierDef } from "@/api/types";
import type { EnvEntry } from "@/features/data/spaceEnvs";
import { checkEntries } from "@/lib/phases";

// The attention zone (DESIGN §5.1): what needs a human, grouped, each env in its first matching group.

export type AttentionGroup = "needs_you" | "waiting" | "blocked" | "failed" | "drift";

export const GROUP_LABEL: Record<AttentionGroup, string> = {
  needs_you: "Needs you",
  waiting: "Waiting on someone",
  blocked: "Blocked",
  failed: "Failed",
  drift: "Drift",
};

export const GROUP_ORDER: AttentionGroup[] = ["needs_you", "waiting", "blocked", "failed", "drift"];

export interface AttentionItem {
  group: AttentionGroup;
  entry: EnvEntry;
  run: Run | null;
  /** Why it is here / what is missing, in words. */
  reason: string;
  /** The plan was computed on a commit the branch has since moved past. */
  stalePlan: boolean;
  /** Where Enter goes. */
  href: string;
}

export function isStalePlan(run: Run, env: Environment): boolean {
  return (
    run.state === "unconfirmed" &&
    !!env.head_sha &&
    !!run.commit_sha &&
    !env.head_sha.startsWith(run.commit_sha) &&
    !run.commit_sha.startsWith(env.head_sha)
  );
}

export function attentionOf(entry: EnvEntry, envHref: string): AttentionItem | null {
  const { summary, env, rights } = entry;
  const run = summary.primary;
  if (run?.state === "unconfirmed" && rights) {
    const stalePlan = isStalePlan(run, env);
    const base = { entry, run, stalePlan, href: `/runs/${run.id}?tab=plan` };
    if (rights.blockedForAll.length) return { ...base, group: "blocked", reason: rights.blockedForAll[0] };
    if (rights.needsYou) return { ...base, group: "needs_you", reason: "you can confirm" };
    return {
      ...base,
      group: "waiting",
      reason: entry.whoMayConfirm ? `needs ${entry.whoMayConfirm}` : rights.confirm.reasons[0],
    };
  }
  if (run && (summary.outcome === "failed" || summary.outcome === "failed_during_apply")) {
    return {
      group: "failed",
      entry,
      run,
      stalePlan: false,
      reason:
        summary.outcome === "failed_during_apply"
          ? "failed during apply — infrastructure may be partially changed"
          : failureReason(run),
      href: `/runs/${run.id}?tab=logs`,
    };
  }
  if (summary.modifiers.drift) {
    return {
      group: "drift",
      entry,
      run: null,
      stalePlan: false,
      reason:
        summary.modifiers.drift === "error"
          ? "the last drift check could not complete"
          : "real infrastructure diverged from the applied state",
      href: env.drift_run_id ? `/runs/${env.drift_run_id}?tab=plan` : envHref,
    };
  }
  return null;
}

/** Column order of the matrix: tier position, then env position, then name (DESIGN §5.1). */
export function envColumns(entries: EnvEntry[], catalog: TierDef[] | undefined): string[] {
  const rank = new Map<string, [number, number]>();
  for (const { env } of entries) {
    const tierPos = catalog?.find((t) => t.name === env.tier)?.position ?? 999;
    const prev = rank.get(env.name);
    const next: [number, number] = [tierPos, env.position];
    if (!prev || next[0] < prev[0] || (next[0] === prev[0] && next[1] < prev[1])) rank.set(env.name, next);
  }
  return [...rank.entries()]
    .sort(([an, a], [bn, b]) => a[0] - b[0] || a[1] - b[1] || an.localeCompare(bn))
    .map(([name]) => name);
}

export type StateFilter = "all" | "attention" | "needs_you" | "running" | "failed" | "drift" | "stale";

export const STATE_FILTERS: { value: StateFilter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "attention", label: "Needs attention" },
  { value: "needs_you", label: "Needs you" },
  { value: "running", label: "Running" },
  { value: "failed", label: "Failed" },
  { value: "drift", label: "Drift" },
  { value: "stale", label: "Branch ahead" },
];

export function matchesState(entry: EnvEntry, f: StateFilter, envHref: string): boolean {
  const { summary } = entry;
  switch (f) {
    case "all":
      return true;
    case "attention":
      return attentionOf(entry, envHref) !== null;
    case "needs_you":
      return !!entry.rights?.needsYou;
    case "running":
      return summary.active && summary.primary?.state !== "unconfirmed";
    case "failed":
      return summary.outcome === "failed" || summary.outcome === "failed_during_apply";
    case "drift":
      return !!summary.modifiers.drift;
    case "stale":
      return !!summary.modifiers.stale;
  }
}

/** A failure in words: the failing check when there is one (the run's error may be a bare stage). */
export function failureReason(run: Run): string {
  const failing = checkEntries(run.check_results).find((c) => c.status === "fail");
  if (failing) return `check ${failing.name} failed${failing.stage ? ` at ${failing.stage}` : ""}`;
  return run.error ?? "failed";
}
