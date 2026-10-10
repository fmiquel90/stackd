import { forwardRef, type ReactNode } from "react";
import type { EnvSummary } from "@/lib/envSummary";
import { buildSegments } from "@/lib/phases";
import { formatCounts, planCounts } from "@/lib/runOutcome";
import { age } from "@/lib/time";
import { cn } from "@/ui/cn";
import { PhaseRail } from "./PhaseRail";
import { ModifierChips, RunStateBadge } from "./StateBadge";

// One environment in the overview matrix, the stack page and the graph node (DESIGN §5.1). It renders
// `summarizeEnv` — the active run, else the last real outcome — never "the latest run" (§2.3).

export interface EnvCellProps {
  summary: EnvSummary;
  /** The viewer can confirm the env's pending run: solid amber + decision tint. */
  needsYou?: boolean;
  /** Selection mark (matrix cursor, selected graph node). */
  selected?: boolean;
  /** Highlighted as a downstream of the selected cell (cascade preview). */
  downstream?: "will_plan" | "plans_if_outputs_change";
  /** Extra line (graph node: stack/env title). */
  title?: ReactNode;
  now?: Date;
  className?: string;
}

export const EnvCell = forwardRef<HTMLDivElement, EnvCellProps>(function EnvCell(
  { summary, needsYou, selected, downstream, title, now, className },
  ref,
) {
  const run = summary.primary;
  const counts = run ? planCounts(run) : null;
  const pending = run?.state === "unconfirmed";
  const label = run
    ? [
        run.state === "unconfirmed"
          ? needsYou
            ? "Unconfirmed, needs your decision"
            : "Unconfirmed, waiting on someone"
          : null,
        counts && pending ? `plan ${formatCounts(counts)}` : null,
      ]
        .filter(Boolean)
        .join(", ") || undefined
    : undefined;

  return (
    <div
      ref={ref}
      data-selected={selected || undefined}
      className={cn(
        "flex min-w-[180px] flex-col gap-1 rounded-base border-l-2 px-2 py-1.5",
        needsYou ? "bg-tint-decision" : "bg-transparent",
        selected ? "border-ring" : downstream ? "border-dashed border-ring" : "border-transparent",
        className,
      )}
    >
      {title}
      {run ? (
        <>
          <span className="flex min-w-0 items-center gap-2">
            <PhaseRail variant="mini" segments={buildSegments(run)} label={label} />
            <RunStateBadge
              run={run}
              compact
              decision={pending ? (needsYou ? "needsYou" : "waiting") : undefined}
            />
          </span>
          <span className="flex min-w-0 flex-wrap items-center gap-1.5">
            <time dateTime={run.finished_at ?? run.created_at} className="font-data text-[12px] text-muted">
              {age(run.finished_at ?? run.created_at, now)}
            </time>
            {pending && counts && (
              <span className="font-data text-[12px] text-muted">{formatCounts(counts)}</span>
            )}
            <ModifierChips modifiers={summary.modifiers} compact />
          </span>
        </>
      ) : (
        <span className="flex flex-wrap items-center gap-1.5">
          <span className="font-data text-[12px] text-muted">never run</span>
          <ModifierChips modifiers={summary.modifiers} compact />
        </span>
      )}
      {downstream && (
        <span className="font-data text-[11px] text-muted">
          {downstream === "will_plan" ? "↳ will plan" : "↳ plans if outputs change"}
        </span>
      )}
    </div>
  );
});
