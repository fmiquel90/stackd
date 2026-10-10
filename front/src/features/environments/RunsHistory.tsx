import { useState } from "react";
import { Link } from "react-router-dom";
import type { Run } from "@/api/types";
import { PhaseRail } from "@/identity/PhaseRail";
import { RunStateBadge } from "@/identity/StateBadge";
import { buildSegments } from "@/lib/phases";
import { formatDuration } from "@/lib/time";
import { Button } from "@/ui/Button";
import { EmptyState, NativeSelect, RelativeTime, Sha, Table, Td, Th } from "@/ui/primitives";

// The environment's run history (DESIGN §5.8) — v1 had none: only the latest run was reachable.

export type RunKind = "all" | "tracked" | "pr" | "drift" | "destroy" | "command";

const KINDS: { value: RunKind; label: string }[] = [
  { value: "all", label: "All runs" },
  { value: "tracked", label: "Tracked" },
  { value: "destroy", label: "Destroy" },
  { value: "pr", label: "PR plans" },
  { value: "drift", label: "Drift checks" },
  { value: "command", label: "Commands" },
];

export function kindOf(r: Run): Exclude<RunKind, "all"> {
  if (r.triggered_by === "schedule") return "drift";
  if (r.type === "proposed") return "pr";
  return r.type;
}

const KIND_LABEL: Record<Exclude<RunKind, "all">, string> = {
  tracked: "tracked",
  destroy: "destroy",
  pr: "PR",
  drift: "drift",
  command: "command",
};

const PAGE = 50;

export function RunsHistory({
  runs,
  kind,
  onKind,
  emailOf,
}: {
  runs: Run[];
  kind: RunKind;
  onKind: (k: RunKind) => void;
  emailOf: (id: string | null) => string | null;
}) {
  const [limit, setLimit] = useState(PAGE);
  const filtered = runs.filter((r) => kind === "all" || kindOf(r) === kind);
  return (
    <div className="flex flex-col gap-2">
      <label className="flex items-center gap-2 self-start">
        <span className="eyebrow text-muted">Show</span>
        <NativeSelect value={kind} onChange={(e) => onKind(e.target.value as RunKind)}>
          {KINDS.map((k) => (
            <option key={k.value} value={k.value}>
              {k.label}
            </option>
          ))}
        </NativeSelect>
        <span className="font-data text-[12px] text-muted">{filtered.length} runs</span>
      </label>
      {filtered.length === 0 ? (
        <EmptyState title="No run of this kind yet." />
      ) : (
        <div className="overflow-x-auto">
          <Table>
            <thead>
              <tr>
                <Th>Run</Th>
                <Th>Kind</Th>
                <Th>Outcome</Th>
                <Th className="max-md:hidden">Phases</Th>
                <Th>Commit</Th>
                <Th>Trigger</Th>
                <Th>Started</Th>
                <Th className="max-md:hidden">Duration</Th>
              </tr>
            </thead>
            <tbody>
              {filtered.slice(0, limit).map((r) => (
                <tr key={r.id} className="hover:bg-raised">
                  <Td>
                    <Link to={`/runs/${r.id}`} className="font-data text-[12px] underline underline-offset-2">
                      {r.id.slice(0, 8)}
                    </Link>
                  </Td>
                  <Td className="font-data text-[12px] text-muted">{KIND_LABEL[kindOf(r)]}</Td>
                  <Td>
                    <RunStateBadge run={r} />
                  </Td>
                  <Td className="max-md:hidden">
                    <PhaseRail variant="mini" segments={buildSegments(r)} />
                  </Td>
                  <Td>
                    <Sha value={r.commit_sha} />
                  </Td>
                  <Td className="font-data text-[12px] text-muted">
                    {r.triggered_by === "manual"
                      ? (emailOf(r.trigger_user_id) ?? "user")
                      : r.triggered_by === "schedule"
                        ? "drift check"
                        : r.triggered_by}
                  </Td>
                  <Td className="text-[12px]">
                    <RelativeTime value={r.created_at} />
                  </Td>
                  <Td className="font-data text-[12px] text-muted max-md:hidden">
                    {r.finished_at
                      ? formatDuration(
                          (new Date(r.finished_at).getTime() - new Date(r.created_at).getTime()) / 1000,
                        )
                      : "—"}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </div>
      )}
      {filtered.length > limit && (
        <Button className="self-start" onClick={() => setLimit((l) => l + PAGE)}>
          Show {Math.min(PAGE, filtered.length - limit)} more
        </Button>
      )}
    </div>
  );
}
