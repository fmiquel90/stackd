import type { ReactNode } from "react";
import type { Run, RunState } from "@/api/types";
import type { EnvModifiers } from "@/lib/envSummary";
import { formatCounts, outcomeOf, planCounts, type Outcome, type PlanCounts } from "@/lib/runOutcome";
import { failedSegment } from "@/lib/phases";
import { Badge, type Tone } from "@/ui/primitives";
import { cn } from "@/ui/cn";

// The state language of DESIGN §3.2: glyph + uppercase mono label + colour, never colour alone.

export type DecisionForm = "needsYou" | "waiting";

interface Look {
  label: string;
  glyph: string;
  tone: Tone;
  struck?: boolean;
  pulse?: boolean;
}

const RUNNING: Partial<Record<RunState, string>> = {
  preparing: "PREPARING",
  planning: "PLANNING",
  checking: "CHECKING",
  confirmed: "APPLYING · STARTING",
  applying: "APPLYING",
  running: "RUNNING",
};

const OUTCOME: Record<Outcome, Look> = {
  applied: { label: "APPLIED", glyph: "✓", tone: "finished" },
  no_changes: { label: "NO CHANGES", glyph: "=", tone: "finished" },
  destroyed: { label: "DESTROYED", glyph: "✓", tone: "finished" },
  planned: { label: "PLANNED", glyph: "◇", tone: "finished" },
  executed: { label: "EXECUTED", glyph: "✓", tone: "finished" },
  failed: { label: "FAILED", glyph: "✕", tone: "failed" },
  failed_during_apply: { label: "FAILED DURING APPLY", glyph: "✕", tone: "failed" },
  discarded: { label: "DISCARDED", glyph: "⊘", tone: "queued", struck: true },
  canceled: { label: "CANCELED", glyph: "⊘", tone: "queued", struck: true },
};

export function lookOf(state: RunState, outcome: Outcome | null): Look {
  if (outcome) return OUTCOME[outcome];
  if (state === "queued") return { label: "QUEUED", glyph: "○", tone: "queued" };
  if (state === "unconfirmed") return { label: "UNCONFIRMED", glyph: "⏸", tone: "decision" };
  return { label: RUNNING[state] ?? state.toUpperCase(), glyph: "◐", tone: "running", pulse: true };
}

const SEGMENT_LABEL = {
  prepare: "Prepare",
  plan: "Plan",
  check: "Check",
  decision: "Decision",
  apply: "Apply",
  run: "Run",
};

export interface StateBadgeProps {
  state: RunState;
  outcome?: Outcome | null;
  /** Plan counts shown after APPLIED / PLANNED / DESTROYED. */
  counts?: PlanCounts | null;
  /** FAILED at <segment>. */
  failedAt?: string;
  /** Unconfirmed only: solid "needs you" vs outlined "waiting on someone" (DESIGN §3.2). */
  decision?: DecisionForm;
  /** Short form for dense cells: no counts, no "at …". */
  compact?: boolean;
  className?: string;
}

export function StateBadge({
  state,
  outcome = null,
  counts,
  failedAt,
  decision,
  compact,
  className,
}: StateBadgeProps) {
  const look = lookOf(state, outcome);
  let label = look.label;
  if (!compact && failedAt && outcome === "failed") label = `FAILED at ${failedAt}`;
  const showCounts =
    !compact && counts && (outcome === "applied" || outcome === "planned" || outcome === "destroyed");
  const needsYou = state === "unconfirmed" && decision === "needsYou";
  return (
    <span className={cn("inline-flex items-center gap-2", className)}>
      <Badge
        tone={look.tone}
        solid={needsYou}
        glyph={<span className={look.pulse ? "rail-pulse" : undefined}>{look.glyph}</span>}
      >
        {/* Compact cells say only "NEEDS YOU": the personal signal is the one that must fit. */}
        {!(needsYou && compact) && <span className={look.struck ? "line-through" : undefined}>{label}</span>}
        {needsYou && <span>{compact ? "NEEDS YOU" : "· NEEDS YOU"}</span>}
      </Badge>
      {showCounts && <span className="font-data text-[12px] text-muted">{formatCounts(counts)}</span>}
    </span>
  );
}

/** StateBadge for a run: derives the outcome, counts and failing segment. */
export function RunStateBadge({
  run,
  decision,
  compact,
  className,
}: {
  run: Run;
  decision?: DecisionForm;
  compact?: boolean;
  className?: string;
}) {
  const outcome = outcomeOf(run);
  const failedAt =
    outcome === "failed"
      ? SEGMENT_LABEL[failedSegment(run, ["prepare", "plan", "check", "decision", "apply", "run"])]
      : undefined;
  return (
    <StateBadge
      state={run.state}
      outcome={outcome}
      counts={planCounts(run)}
      failedAt={failedAt}
      decision={decision}
      compact={compact}
      className={className}
    />
  );
}

/* ---------- Modifiers (DESIGN §3.2 table) ---------- */

export function ModifierChips({
  modifiers,
  compact,
}: {
  modifiers: Partial<EnvModifiers>;
  compact?: boolean;
}) {
  const chips: ReactNode[] = [];
  if (modifiers.mocked)
    chips.push(
      <Badge key="mock" tone="mock" glyph="◆" title="Mocked inputs: this plan is not real">
        {compact ? "MOCK" : "MOCKED"}
      </Badge>,
    );
  if (modifiers.fallback)
    chips.push(
      <Badge key="fb" tone="decision" glyph="◈" title="A secret came from a fallback, not the real source">
        FALLBACK
      </Badge>,
    );
  if (modifiers.stale) {
    const n = modifiers.stale.commitsAhead;
    const attenuated = modifiers.stale.affectsProjectRoot === false;
    chips.push(
      <span
        key="stale"
        title={
          attenuated
            ? "The branch moved ahead, but not this folder"
            : n
              ? `The branch is ${n} commit${n > 1 ? "s" : ""} ahead of the deployed commit`
              : "The branch is ahead of the deployed commit"
        }
        className={cn(
          "font-data text-[12px] text-muted",
          attenuated && "underline decoration-dotted underline-offset-2",
        )}
      >
        ↑{n ?? ""}
        <span className="sr-only"> branch ahead</span>
      </span>,
    );
  }
  if (modifiers.drift)
    chips.push(
      <Badge
        key="drift"
        glyph={modifiers.drift === "error" ? "≈?" : "≈"}
        title={
          modifiers.drift === "error"
            ? "The last drift check could not complete"
            : "Real infrastructure diverged from the last applied state"
        }
      >
        {modifiers.drift === "error" ? "DRIFT CHECK FAILED" : "DRIFT"}
      </Badge>,
    );
  if (modifiers.locked)
    chips.push(
      <Badge key="lock" glyph="⚿" title="State is locked">
        LOCKED
      </Badge>,
    );
  if (modifiers.pr)
    chips.push(
      <Badge key="pr" tone="queued" title="The active run is a pull-request plan">
        PR
      </Badge>,
    );
  if (!chips.length) return null;
  return <span className="inline-flex flex-wrap items-center gap-1.5">{chips}</span>;
}
