import { Fragment, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { MessageSquarePlus, TriangleAlert } from "lucide-react";
import { ApiError } from "@/api/client";
import { comments, environments, runs, stacks, tiers } from "@/api/resources";
import { useEntityStream } from "@/api/stream";
import { useSession } from "@/auth/session";
import type { CommentAnchor, Run, RunComment, RunState } from "@/api/types";
import { AnsiText } from "@/components/ansi";
import {
  CommentComposer,
  CommentsPanel,
  CommentThread,
  commentLineKey,
  isRoot,
  lineKey,
} from "@/components/CommentsPanel";
import { PhaseRail, type Phase, type PhaseStatus } from "@/components/PhaseRail";
import { StateBadge } from "@/components/StateBadge";
import { ProvenanceBadge, parseProvenance } from "@/components/ProvenanceBadge";
import { Button, Card, TextInput } from "@/components/ui";

const TERMINAL: RunState[] = ["finished", "failed", "discarded", "canceled"];
const ORDER: Record<RunState, number> = {
  queued: 0, preparing: 1, planning: 2, checking: 3, unconfirmed: 4,
  confirmed: 5, applying: 6, finished: 7, failed: 7, discarded: 7, canceled: 7,
};

function buildPhases(run: Run): Phase[] {
  const hasChecks = Boolean(run.check_results?.checks?.length);
  const bad = ["failed", "discarded", "canceled"].includes(run.state);
  const cur = ORDER[run.state];
  const segs = [
    { key: "preparing", label: "Preparing", at: 1 },
    { key: "planning", label: "Planning", at: 2 },
    ...(hasChecks ? [{ key: "checking", label: "Checking", at: 3 }] : []),
    { key: "apply", label: "Apply", at: 6 },
    { key: "finished", label: "Done", at: 7 },
  ];
  return segs.map((s): Phase => {
    let status: PhaseStatus;
    if (bad) status = "skipped";
    else if (run.state === "unconfirmed" && s.key === "apply") status = "waiting";
    else if (cur > s.at) status = "done";
    else if (cur === s.at) status = "active";
    else status = "pending";
    return { key: s.key, label: s.label, status };
  });
}

function PlanSummary({ run }: { run: Run }) {
  const s = run.plan_summary;
  if (!s) return <span className="font-data text-[12px]">No plan yet.</span>;
  return (
    <span className="font-data text-[18px]">
      <span style={{ color: "var(--color-state-finished)" }}>+{s.add ?? 0}</span>{" "}
      <span style={{ color: "var(--color-state-unconfirmed)" }}>~{s.change ?? 0}</span>{" "}
      <span style={{ color: "var(--color-state-failed)" }}>−{s.destroy ?? 0}</span>
    </span>
  );
}

export function RunPage() {
  const { runId = "" } = useParams();
  const qc = useQueryClient();
  // Live updates via WS (DESIGN §6); the 10s poll is just the reconnection fallback.
  useEntityStream(`run:${runId}`, [
    ["run", runId],
    ["run-logs", runId],
    ["run-comments", runId],
  ]);
  const { data: run } = useQuery({
    queryKey: ["run", runId],
    queryFn: () => runs.get(runId),
    refetchInterval: (q) =>
      q.state.data && !TERMINAL.includes(q.state.data.state) ? 10000 : false,
  });
  const logs = useQuery({
    queryKey: ["run-logs", runId],
    queryFn: () => runs.logs(runId),
    refetchInterval: run && !TERMINAL.includes(run.state) ? 10000 : false,
  });

  const env = useQuery({
    queryKey: ["environment", run?.environment_id],
    queryFn: () => environments.get(run!.environment_id),
    enabled: Boolean(run?.environment_id),
  });
  const stack = useQuery({
    queryKey: ["stack", env.data?.stack_id],
    queryFn: () => stacks.get(env.data!.stack_id),
    enabled: Boolean(env.data?.stack_id),
  });
  const catalog = useQuery({ queryKey: ["tiers"], queryFn: tiers.list });
  const me = useSession().data;
  const commentList = useQuery({
    queryKey: ["run-comments", runId],
    queryFn: () => comments.list(runId),
  });
  const allComments = commentList.data ?? [];
  const openThreads = allComments.filter((c) => c.parent_id == null && !c.resolved).length;
  const repliesOf = (id: string) => allComments.filter((c) => c.parent_id === id);
  // Anchored (plan_line) roots grouped by the log line they pin to, for inline rendering.
  const threadsByLine = new Map<string, RunComment[]>();
  for (const c of allComments) {
    const k = isRoot(c) ? commentLineKey(c) : null;
    if (k) threadsByLine.set(k, [...(threadsByLine.get(k) ?? []), c]);
  }
  const [anchorDraft, setAnchorDraft] = useState<CommentAnchor | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [typed, setTyped] = useState("");

  const invalidate = () => qc.invalidateQueries({ queryKey: ["run", runId] });
  const confirm = useMutation({
    mutationFn: () => runs.confirm(runId),
    onSuccess: () => {
      setConfirming(false);
      setTyped("");
      invalidate();
    },
  });
  const discard = useMutation({ mutationFn: () => runs.discard(runId), onSuccess: invalidate });

  if (!run) return <p className="font-data text-[12px]">Loading…</p>;

  // Friction proportional to risk (DESIGN §5.2 / §9): destroy, or a tier that requires four-eyes →
  // type the env name first. Keyed to the tier's flag (not a hardcoded "prod") now tiers are custom.
  const tierDef = catalog.data?.find((t) => t.name === env.data?.tier);
  const highRisk = run.type === "destroy" || Boolean(tierDef?.requires_four_eyes);
  const onConfirmClick = () => {
    if (highRisk) setConfirming(true);
    else confirm.mutate();
  };

  return (
    <div className="flex gap-6">
      <div className="w-48 shrink-0">
        <PhaseRail phases={buildPhases(run)} />
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-4">
        {env.data && (
          <Link
            to={`/stacks/${env.data.stack_id}`}
            className="font-data text-[12px]"
            style={{ color: "var(--color-text-secondary)" }}
          >
            ← {stack.data?.name ?? "stack"}/{env.data.name}
          </Link>
        )}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <StateBadge state={run.state} mocked={run.used_mocks} fallback={run.used_secret_fallback} />
            <span className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
              {run.type} · {run.commit_sha?.slice(0, 7) ?? "—"} · via {run.triggered_by}
            </span>
          </div>
          <div className="flex items-center gap-2">
            {openThreads > 0 && (
              <span
                className="font-data inline-flex items-center gap-1 text-[12px]"
                style={{ color: "var(--color-state-unconfirmed)" }}
                title="Open discussion threads on this plan"
              >
                <TriangleAlert size={12} strokeWidth={1.75} aria-hidden />
                {openThreads} open thread{openThreads > 1 ? "s" : ""}
              </span>
            )}
            <Button
              variant="accent"
              disabled={run.state !== "unconfirmed" || confirm.isPending || !env.data}
              onClick={onConfirmClick}
            >
              Confirm
            </Button>
            <Button disabled={run.state !== "unconfirmed" || discard.isPending} onClick={() => discard.mutate()}>
              Discard
            </Button>
          </div>
        </div>

        {confirming && env.data && (
          <Card>
            <div className="text-[13px] font-medium" style={{ color: "var(--color-state-unconfirmed)" }}>
              Confirm {run.type === "destroy" ? "destroy" : "apply"} on{" "}
              <span className="font-data">{env.data.name}</span> ({env.data.tier})
            </div>
            <div className="mt-2 mb-3">
              <PlanSummary run={run} />
              {(run.plan_summary?.destroy ?? 0) > 0 && (
                <span className="ml-3 font-data text-[12px]" style={{ color: "var(--color-state-failed)" }}>
                  {run.plan_summary?.destroy} resource(s) will be destroyed
                </span>
              )}
            </div>
            <div className="flex items-end gap-2">
              <div className="flex flex-col gap-1">
                <span className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
                  Type <span style={{ color: "var(--color-text-primary)" }}>{env.data.name}</span> to confirm
                </span>
                <TextInput value={typed} onChange={(e) => setTyped(e.target.value)} autoFocus />
              </div>
              <Button
                variant="accent"
                disabled={typed !== env.data.name || confirm.isPending}
                onClick={() => confirm.mutate()}
              >
                Confirm {run.type === "destroy" ? "destroy" : "apply"}
              </Button>
              <Button onClick={() => { setConfirming(false); setTyped(""); }}>Cancel</Button>
            </div>
          </Card>
        )}
        {confirm.error instanceof ApiError && (
          <p className="text-[12px]" style={{ color: "var(--color-state-failed)" }}>
            {confirm.error.detail ?? confirm.error.title}
          </p>
        )}
        {run.error && (
          <p className="font-data text-[12px]" style={{ color: "var(--color-state-failed)" }}>
            {run.error}
          </p>
        )}

        <Card>
          <div className="mb-2 text-[13px] font-medium">Plan</div>
          <PlanSummary run={run} />
        </Card>

        {run.check_results?.checks?.length ? (
          <Card>
            <div className="mb-2 text-[13px] font-medium">Checks</div>
            {run.check_results.checks.map((c) => (
              <div key={c.name} className="font-data text-[12px]">
                {c.name}: <span style={{ color: c.status === "warn" ? "var(--color-state-unconfirmed)" : "var(--color-state-failed)" }}>{c.status}</span>{" "}
                {c.detail}
              </div>
            ))}
          </Card>
        ) : null}

        {run.variable_provenance && Object.keys(run.variable_provenance).length > 0 && (
          <Card>
            <div className="mb-2 text-[13px] font-medium">Inputs</div>
            <div className="flex flex-col gap-1">
              {Object.entries(run.variable_provenance).map(([name, prov]) => (
                <div key={name} className="flex items-center gap-2">
                  <span className="font-data text-[12px]">{name}</span>
                  <ProvenanceBadge provenance={parseProvenance(prov)} />
                </div>
              ))}
            </div>
          </Card>
        )}

        <Card>
          <div className="mb-2 text-[13px] font-medium">Logs</div>
          <div
            className="font-data max-h-[400px] overflow-auto p-2 text-[12px]"
            style={{ backgroundColor: "var(--color-bg-base)", borderRadius: 4, lineHeight: 1.5 }}
          >
            {(logs.data ?? []).flatMap((chunk) =>
              chunk.lines.map((l, i) => {
                const key = lineKey(chunk.phase, chunk.seq, i);
                const lineThreads = threadsByLine.get(key) ?? [];
                const composingHere =
                  anchorDraft?.kind === "plan_line" &&
                  anchorDraft.phase === chunk.phase &&
                  anchorDraft.seq === chunk.seq &&
                  anchorDraft.line_start === i;
                return (
                  <Fragment key={`${chunk.phase}-${chunk.seq}-${i}`}>
                    <div className="group flex items-start gap-2 whitespace-pre-wrap">
                      {/* Hover affordance: anchor a comment to this plan line (SPECS §16.2). */}
                      <button
                        type="button"
                        aria-label="Comment on this line"
                        className="ui-btn shrink-0 opacity-0 group-hover:opacity-100"
                        style={{ color: "var(--color-accent)" }}
                        onClick={() =>
                          setAnchorDraft({
                            kind: "plan_line",
                            phase: chunk.phase,
                            seq: chunk.seq,
                            line_start: i,
                            line_end: i,
                            snippet: l.msg.slice(0, 120),
                          })
                        }
                      >
                        <MessageSquarePlus size={13} strokeWidth={1.75} aria-hidden />
                      </button>
                      <span className="min-w-0">
                        <span style={{ color: "var(--color-text-secondary)" }}>{chunk.section ?? chunk.phase} </span>
                        <AnsiText text={l.msg} />
                      </span>
                    </div>
                    {/* Inline, line-level review (progressive): anchored threads + composer here. */}
                    {(lineThreads.length > 0 || composingHere) && (
                      <div
                        className="my-1 ml-6 flex flex-col gap-2 border-l-2 pl-3"
                        style={{ borderColor: "var(--color-border)", whiteSpace: "normal" }}
                      >
                        {lineThreads.map((root) => (
                          <CommentThread
                            key={root.id}
                            runId={runId}
                            root={root}
                            replies={repliesOf(root.id)}
                            meId={me?.id}
                            hideAnchor
                          />
                        ))}
                        {composingHere && (
                          <CommentComposer
                            runId={runId}
                            anchor={anchorDraft}
                            placeholder="Comment on this line…"
                            autoFocus
                            onPosted={() => setAnchorDraft(null)}
                            onCancel={() => setAnchorDraft(null)}
                          />
                        )}
                      </div>
                    )}
                  </Fragment>
                );
              }),
            )}
            {logs.data && logs.data.length === 0 && (
              <span style={{ color: "var(--color-text-secondary)" }}>No logs yet.</span>
            )}
          </div>
        </Card>

        <CommentsPanel runId={runId} />
      </div>
    </div>
  );
}
