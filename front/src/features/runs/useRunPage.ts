import { useEffect, useMemo, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { comments, environments, graphApi, queue, runs, stacks, tiers, users } from "@/api/resources";
import { useLiveSignals } from "@/api/stream";
import type { LogChunk } from "@/api/types";
import { useAnnounce } from "@/app/announcer";
import { useSession } from "@/auth/session";
import { downstreamOf } from "@/lib/cascade";
import { mergeChunks, planLines } from "@/lib/logModel";
import { isTerminal } from "@/lib/runOutcome";
import { parsePlan } from "@/lib/planParse";
import type { CascadeItem } from "@/identity/TypeToConfirm";

// Everything the run page shows (DESIGN §5.2). Queries are the source of truth; WS signals invalidate
// them — except logs, which are appended incrementally with `after_seq` (DESIGN §6, SPECS §5.3).

export function useRunPage(runId: string) {
  const qc = useQueryClient();
  const announce = useAnnounce();
  const me = useSession().data ?? null;

  const run = useQuery({
    queryKey: ["run", runId],
    queryFn: () => runs.get(runId),
    // Fallback while the socket is down; the WS signal is what normally refreshes it.
    refetchInterval: (q) => (q.state.data && !isTerminal(q.state.data.state) ? 15_000 : false),
  });
  const envId = run.data?.environment_id;
  const env = useQuery({
    queryKey: ["environment", envId],
    queryFn: () => environments.get(envId!),
    enabled: !!envId,
  });
  const stack = useQuery({
    queryKey: ["stack", env.data?.stack_id],
    queryFn: () => stacks.get(env.data!.stack_id),
    enabled: !!env.data?.stack_id,
  });
  const catalog = useQuery({ queryKey: ["tiers"], queryFn: tiers.list });
  const people = useQuery({ queryKey: ["mentionable"], queryFn: users.mentionable, staleTime: 5 * 60_000 });
  const logs = useQuery({ queryKey: ["run-logs", runId], queryFn: () => runs.logs(runId) });
  const threads = useQuery({ queryKey: ["run-comments", runId], queryFn: () => comments.list(runId) });
  const graph = useQuery({ queryKey: ["graph"], queryFn: graphApi.get, staleTime: 60_000 });
  const stackList = useQuery({ queryKey: ["stacks"], queryFn: stacks.list });
  const queued = run.data?.state === "queued" || run.data?.state === "confirmed";
  const queueList = useQuery({
    queryKey: ["queue"],
    queryFn: queue.list,
    enabled: queued,
    refetchInterval: queued ? 10_000 : false,
  });

  // Incremental log fetch: one in flight per phase, from the highest seq we hold.
  const inflight = useRef(new Set<string>());
  const fetchLogs = async (phase: string) => {
    if (inflight.current.has(phase)) return;
    inflight.current.add(phase);
    try {
      const have = qc.getQueryData<LogChunk[]>(["run-logs", runId]) ?? [];
      const after = Math.max(-1, ...have.filter((c) => c.phase === phase).map((c) => c.seq));
      const fresh = await runs.logsAfter(runId, phase, after);
      qc.setQueryData<LogChunk[]>(["run-logs", runId], (old) => mergeChunks(old ?? [], fresh));
    } finally {
      inflight.current.delete(phase);
    }
  };

  useLiveSignals([`run:${runId}`], (s) => {
    if (!("run_id" in s) || s.run_id !== runId) return;
    if (s.kind === "log" && typeof s.phase === "string") void fetchLogs(s.phase);
    else if (s.kind === "comment_event") qc.invalidateQueries({ queryKey: ["run-comments", runId] });
    else if (s.kind === "run_event") {
      qc.invalidateQueries({ queryKey: ["run", runId] });
      if (envId) {
        qc.invalidateQueries({ queryKey: ["environment", envId] });
        qc.invalidateQueries({ queryKey: ["env-runs", envId] });
      }
    }
  });

  // Polite announcement of state changes (DESIGN §7) — never a focus move.
  const lastState = useRef<string | null>(null);
  useEffect(() => {
    const st = run.data?.state;
    if (!st) return;
    if (lastState.current && lastState.current !== st) announce(`Run is now ${st}.`);
    lastState.current = st;
  }, [run.data?.state, announce]);

  const emailOf = useMemo(() => {
    const m = new Map((people.data ?? []).map((u) => [u.id, u.email]));
    return (id: string | null | undefined) => (id ? (m.get(id) ?? null) : null);
  }, [people.data]);

  const planText = useMemo(() => {
    const lines = planLines(logs.data ?? []);
    return lines.length ? lines : null;
  }, [logs.data]);
  const parsed = useMemo(() => (planText ? parsePlan(planText) : null), [planText]);

  const cascade = useMemo<CascadeItem[]>(() => {
    if (!graph.data || !envId) return [];
    const nodes = new Map(graph.data.nodes.map((n) => [n.id, n]));
    const stackName = new Map((stackList.data ?? []).map((s) => [s.id, s.name]));
    return downstreamOf(envId, graph.data.edges).map((step) => {
      const n = nodes.get(step.envId);
      return {
        label: n ? `${stackName.get(n.stack_id) ?? "?"}/${n.name}` : step.envId.slice(0, 8),
        likelihood: step.likelihood,
        mock: step.mockDefined,
      };
    });
  }, [graph.data, envId, stackList.data]);

  const queueEntry = queueList.data?.find((q) => q.run_id === runId) ?? null;

  return {
    me,
    run: run.data ?? null,
    runError: run.error,
    env: env.data ?? null,
    stack: stack.data ?? null,
    tier: catalog.data?.find((t) => t.name === env.data?.tier),
    tiersLoaded: catalog.isSuccess,
    logs: logs.data ?? [],
    threads: threads.data ?? [],
    planText,
    parsed,
    cascade,
    queueEntry,
    emailOf,
  };
}
