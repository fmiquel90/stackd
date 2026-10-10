import type { Run } from "@/api/types";
import { checkEntries, ranAfterPlanHooks } from "@/lib/phases";
import { Badge, Table, Td, Th, type Tone } from "@/ui/primitives";

export { checkEntries, ranAfterPlanHooks };

// The run page's Checks tab (DESIGN §5.2): one row per hook check, from every stage of the plan and
// apply jobs (`check_results.results`, SPECS §8.3); older runs fall back to their warn/fail `checks`.

const STATUS: Record<string, { tone: Tone; glyph: string }> = {
  ok: { tone: "finished", glyph: "✓" },
  warn: { tone: "decision", glyph: "!" },
  fail: { tone: "failed", glyph: "✕" },
};
const UNKNOWN = { tone: "queued" as Tone, glyph: "?" };

// The apply job's stages: a warning there comes after the confirmation and only informs.
const APPLY_STAGES = new Set(["before_apply", "after_apply"]);

function warnNote(stage: string | undefined): { text: string; decision: boolean } | null {
  if (!stage) return null; // older run: the stage, hence the meaning of the warning, is unknown
  return APPLY_STAGES.has(stage)
    ? { text: "after confirmation — informational", decision: false }
    : { text: "manual confirmation required", decision: true };
}

export function CheckList({ checkResults }: { checkResults: Run["check_results"] }) {
  const entries = checkEntries(checkResults);
  if (entries.length === 0) return null;
  return (
    <Table>
      <thead>
        <tr>
          <Th>Status</Th>
          <Th>Stage</Th>
          <Th>Check</Th>
          <Th>Output</Th>
        </tr>
      </thead>
      <tbody>
        {entries.map((c, i) => {
          const s = STATUS[c.status] ?? UNKNOWN;
          const note = c.status === "warn" ? warnNote(c.stage) : null;
          return (
            // One hook can report at several stages: the key carries the stage and the position.
            <tr key={`${c.stage ?? "-"}:${c.name}:${i}`} className="align-baseline">
              <Td>
                <Badge tone={s.tone} glyph={s.glyph}>
                  {c.status}
                </Badge>
              </Td>
              <Td className="font-data text-[12px] text-muted">
                {c.stage ?? (
                  <span title="Stage not recorded: this run predates per-stage check results">—</span>
                )}
              </Td>
              <Td className="font-data text-[12px]">{c.name}</Td>
              <Td className="font-data text-[12px] whitespace-pre-wrap text-muted">
                {c.detail}
                {note && (
                  <span className={note.decision ? "text-st-decision" : "text-muted"}>
                    {c.detail ? " · " : ""}
                    {note.text}
                  </span>
                )}
              </Td>
            </tr>
          );
        })}
      </tbody>
    </Table>
  );
}
