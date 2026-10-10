import type { CheckResult, Run } from "@/api/types";
import { formatCounts, outcomeOf, planCounts } from "./runOutcome";
import { formatDuration } from "./time";

// The phase rail model (DESIGN §2.2): which segments a run has, and the status of each.

export type SegmentKey = "prepare" | "plan" | "check" | "decision" | "apply" | "run";
export type SegmentStatus = "pending" | "active" | "waiting" | "done" | "failed" | "not_needed" | "not_run";

export interface Segment {
  key: SegmentKey;
  label: string;
  status: SegmentStatus;
  /** Mono metadata under the segment: duration, plan summary, check counts, who confirmed. */
  meta?: string;
  /** Log phase the segment opens in the Logs tab. */
  logPhase?: string;
}

/** Per log phase, the seconds between its first and last line (from `logModel.phaseDurations`). */
export type PhaseDurations = Partial<Record<string, number>>;

const LABEL: Record<SegmentKey, string> = {
  prepare: "Prepare",
  plan: "Plan",
  check: "Check",
  decision: "Decision",
  apply: "Apply",
  run: "Run",
};

const STAGE_SEGMENT: Record<string, SegmentKey> = {
  before_init: "prepare",
  after_init: "prepare",
  before_plan: "plan",
  after_plan: "check",
  before_apply: "apply",
  after_apply: "apply",
};

export function checkEntries(cr: Run["check_results"]): CheckResult[] {
  return cr?.results?.length ? cr.results : (cr?.checks ?? []);
}

/** The plan ran `after_plan` hooks, i.e. went through `checking` (SPECS §4.2). */
export function ranAfterPlanHooks(cr: Run["check_results"]): boolean {
  if (cr?.results?.length) return cr.results.some((c) => c.stage === "after_plan");
  // Older runs: only warn/fail `checks`, often without a stage — any check shows the segment.
  return (cr?.checks ?? []).some((c) => !c.stage || c.stage === "after_plan");
}

function keysFor(run: Run): SegmentKey[] {
  if (run.type === "command") return ["prepare", "run"];
  const check = run.state === "checking" || ranAfterPlanHooks(run.check_results);
  const keys: SegmentKey[] = ["prepare", "plan", ...(check ? (["check"] as const) : [])];
  if (run.type === "proposed") return keys;
  return [...keys, "decision", "apply"];
}

/** Where a failed run failed (DESIGN §2.2). */
export function failedSegment(run: Run, keys: SegmentKey[]): SegmentKey {
  if (run.type === "command") return run.claimed_at ? "run" : "prepare";
  if (run.confirmed_at && keys.includes("apply")) return "apply";
  const failing = checkEntries(run.check_results).find((c) => c.status === "fail");
  const fromStage = failing?.stage ? STAGE_SEGMENT[failing.stage] : undefined;
  if (fromStage && keys.includes(fromStage)) return fromStage;
  return run.claimed_at ? "plan" : "prepare";
}

/** The segment the run is in (live) or stopped at (terminal), and the status it has there. */
function cursor(run: Run, keys: SegmentKey[]): { at: SegmentKey; status: SegmentStatus } | "complete" {
  switch (run.state) {
    case "queued":
      return { at: "prepare", status: "pending" };
    case "preparing":
      return { at: "prepare", status: "active" };
    case "planning":
      return { at: "plan", status: "active" };
    case "checking":
      return { at: "check", status: "active" };
    case "unconfirmed":
      return { at: "decision", status: "waiting" };
    case "confirmed":
    case "applying":
      return { at: "apply", status: "active" };
    case "running":
      return { at: "run", status: "active" };
    case "failed":
      return { at: failedSegment(run, keys), status: "failed" };
    case "discarded":
    case "canceled":
      // Stopped at the decision when a plan exists, otherwise before anything ran.
      return {
        at: run.plan_summary && keys.includes("decision") ? "decision" : "prepare",
        status: "not_run",
      };
    case "finished":
      return "complete";
  }
}

export function buildSegments(run: Run, durations: PhaseDurations = {}, confirmedBy?: string): Segment[] {
  const keys = keysFor(run);
  const cur = cursor(run, keys);
  const counts = planCounts(run);
  const noChanges = outcomeOf(run) === "no_changes";
  const checks = checkEntries(run.check_results);

  return keys.map((key, i): Segment => {
    let status: SegmentStatus;
    if (cur === "complete") {
      status = (key === "decision" || key === "apply") && noChanges ? "not_needed" : "done";
    } else {
      const at = keys.indexOf(cur.at);
      status =
        i < at
          ? "done"
          : i === at
            ? cur.status
            : run.state === "queued" || !isStopped(run)
              ? "pending"
              : "not_run";
    }
    // An empty plan never reaches the decision: nothing to decide, nothing to apply.
    if (noChanges && (key === "decision" || key === "apply")) status = "not_needed";

    const seg: Segment = { key, label: LABEL[key], status };
    const d = (p: string) => (durations[p] !== undefined ? formatDuration(durations[p]!) : undefined);
    switch (key) {
      case "plan":
        seg.logPhase = "planning";
        seg.meta = [d("planning"), counts && formatCounts(counts)].filter(Boolean).join(" · ") || undefined;
        break;
      case "check": {
        seg.logPhase = "checking";
        const after = checks.filter((c) => !c.stage || c.stage === "after_plan");
        const by = (s: string) => after.filter((c) => c.status === s).length;
        const parts = [
          by("ok") && `${by("ok")} ok`,
          by("warn") && `${by("warn")} warn`,
          by("fail") && `${by("fail")} fail`,
        ];
        seg.meta = parts.filter(Boolean).join(" · ") || undefined;
        break;
      }
      case "decision":
        if (run.confirmed_at) seg.meta = run.confirmed_by_user_id ? (confirmedBy ?? "confirmed") : "auto";
        else if (run.state === "discarded") seg.meta = "discarded";
        else if (run.state === "canceled") seg.meta = "canceled";
        else if (status === "not_needed") seg.meta = "no changes";
        break;
      case "apply":
        seg.logPhase = "applying";
        seg.meta = d("applying");
        break;
      case "run":
        seg.logPhase = "running";
        seg.meta = d("running") ?? run.command?.name;
        break;
    }
    return seg;
  });
}

function isStopped(run: Run): boolean {
  return run.state === "failed" || run.state === "discarded" || run.state === "canceled";
}
