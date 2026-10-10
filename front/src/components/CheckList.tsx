import { Check, CircleHelp, TriangleAlert, X, type LucideIcon } from "lucide-react";
import type { CheckResult, Run } from "@/api/types";
import { Badge, Card } from "./ui";

type CheckResults = Run["check_results"];

// Color is paired with a label + icon, never the only signal (DESIGN §7). Amber = warn (needs a human, §3.1).
const STATUS: Record<string, { color: string; icon: LucideIcon }> = {
  ok: { color: "var(--color-state-finished)", icon: Check },
  warn: { color: "var(--color-state-unconfirmed)", icon: TriangleAlert },
  fail: { color: "var(--color-state-failed)", icon: X },
};
const UNKNOWN_STATUS = { color: "var(--color-state-queued)", icon: CircleHelp };

// The apply job's stages (SPECS §8.3): a warning there comes after the confirmation and only informs.
const APPLY_STAGES = new Set(["before_apply", "after_apply"]);

/**
 * Every check with its stage when the API sent `results` (SPECS §8.3); otherwise the warn/fail
 * `checks` of older runs, which may carry no stage.
 */
export function checkEntries(cr: CheckResults): CheckResult[] {
  return cr?.results?.length ? cr.results : (cr?.checks ?? []);
}

/**
 * Whether the plan job ran `after_plan` hooks, i.e. went through `checking` (SPECS §4.2:
 * `planning → checking` iff ≥ 1 after_plan hook).
 */
export function ranAfterPlanHooks(cr: CheckResults): boolean {
  if (cr?.results?.length) return cr.results.some((c) => c.stage === "after_plan");
  // Older runs: only the warn/fail `checks`, mostly without a stage. A check with no stage keeps
  // the previous rule (any check shows the segment); one with a stage must be after_plan.
  return (cr?.checks ?? []).some((c) => !c.stage || c.stage === "after_plan");
}

// DESIGN §5.2: a plan-job warn asks for a human (amber note); an apply-stage warn only informs.
function warnNote(stage: string | undefined): { text: string; color: string } | null {
  if (!stage) return null; // older run: the stage, hence the meaning of the warning, is unknown
  return APPLY_STAGES.has(stage)
    ? { text: "after confirmation — informational", color: "var(--color-text-secondary)" }
    : { text: "manual confirmation required", color: "var(--color-state-unconfirmed)" };
}

/** The run page's Checks block (DESIGN §5.2): one row per hook check — status, stage, name, output. */
export function CheckList({ checkResults }: { checkResults: CheckResults }) {
  const entries = checkEntries(checkResults);
  if (entries.length === 0) return null;
  return (
    <Card>
      <div className="mb-2 text-[13px] font-medium">Checks</div>
      <table className="w-full text-left font-data text-[12px]">
        <thead>
          <tr style={{ color: "var(--color-text-secondary)" }}>
            <th className="py-1 pr-4 font-normal">STATUS</th>
            <th className="py-1 pr-4 font-normal">STAGE</th>
            <th className="py-1 pr-4 font-normal">CHECK</th>
            <th className="py-1 font-normal">OUTPUT</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((c, i) => {
            const s = STATUS[c.status] ?? UNKNOWN_STATUS;
            const note = c.status === "warn" ? warnNote(c.stage) : null;
            return (
              // One hook name can report at several stages (after_plan, then after_apply): the
              // key carries the stage and the position, not just the name.
              <tr key={`${c.stage ?? "-"}:${c.name}:${i}`} className="align-baseline">
                <td className="py-1 pr-4">
                  <Badge color={s.color} icon={s.icon}>
                    {c.status}
                  </Badge>
                </td>
                <td className="py-1 pr-4" style={{ color: "var(--color-text-secondary)" }}>
                  {c.stage ?? (
                    <span title="Stage not recorded: this run predates per-stage check results">—</span>
                  )}
                </td>
                <td className="py-1 pr-4">{c.name}</td>
                <td className="py-1 whitespace-pre-wrap" style={{ color: "var(--color-text-secondary)" }}>
                  {c.detail}
                  {note && (
                    <span style={{ color: note.color }}>
                      {c.detail ? " · " : ""}
                      {note.text}
                    </span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </Card>
  );
}
