import type { Environment, Run } from "@/api/types";
import { isActive, outcomeOf, type Outcome } from "./runOutcome";

// The truth of an environment (DESIGN §2.3). Every place that summarises an env — the overview cell,
// the stack page, the graph node, ⌘K — goes through this one function.

export interface EnvModifiers {
  drift: "drifted" | "error" | null;
  /** Branch head ahead of the deployed commit; `commitsAhead` is null until the API computes it. */
  stale: { commitsAhead: number | null; affectsProjectRoot: boolean | null } | null;
  locked: boolean;
  mocked: boolean;
  fallback: boolean;
  /** The env's active run is a proposed (PR) run. */
  pr: boolean;
}

export interface EnvSummary {
  /** Active run if any, else the last real tracked/destroy outcome. Null: never ran. */
  primary: Run | null;
  active: boolean;
  outcome: Outcome | null;
  /** Commit of the last finished tracked run (applied or no changes). */
  deployedSha: string | null;
  modifiers: EnvModifiers;
}

/** Runs that may stand for the env when it is idle (DESIGN §2.3 rule 2). */
function isRealOutcome(run: Run): boolean {
  if (run.type !== "tracked" && run.type !== "destroy") return false;
  if (run.triggered_by === "schedule") return false; // drift checks are proposed, but be explicit
  const o = outcomeOf(run);
  return o !== null && o !== "discarded" && o !== "canceled";
}

const byNewest = (a: Run, b: Run) => b.created_at.localeCompare(a.created_at);

export function summarizeEnv(env: Environment, runs: Run[]): EnvSummary {
  const sorted = [...runs].sort(byNewest);
  // The run that holds the env (unique per env, SPECS §3.5 — the index excludes `queued`) wins over a
  // queued one: a queued run behind an unconfirmed plan must not hide the pending decision.
  const activeRun =
    sorted.find((r) => isActive(r.state) && r.state !== "queued") ??
    sorted.find((r) => r.state === "queued") ??
    null;
  const primary = activeRun ?? sorted.find(isRealOutcome) ?? null;
  const deployed = sorted.find((r) => r.type === "tracked" && r.state === "finished");

  return {
    primary,
    active: activeRun !== null,
    outcome: primary ? outcomeOf(primary) : null,
    deployedSha: deployed?.commit_sha ?? null,
    modifiers: {
      drift: env.drift_status === "drifted" || env.drift_status === "error" ? env.drift_status : null,
      stale: env.stale
        ? { commitsAhead: env.commits_ahead, affectsProjectRoot: env.affects_project_root }
        : null,
      locked: env.locked,
      mocked: Boolean(primary?.used_mocks),
      fallback: Boolean(primary?.used_secret_fallback),
      pr: activeRun?.type === "proposed",
    },
  };
}

/** Sort key for "worst first": needs a decision < failed < drift < running < fine < never ran. */
export function severity(s: EnvSummary): number {
  if (s.primary?.state === "unconfirmed") return 0;
  if (s.outcome === "failed" || s.outcome === "failed_during_apply") return 1;
  if (s.modifiers.drift) return 2;
  if (s.active) return 3;
  if (s.primary) return 4;
  return 5;
}
