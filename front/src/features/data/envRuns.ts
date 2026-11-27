import {
  useInfiniteQuery,
  useQueries,
  useQueryClient,
  type QueryClient,
  type QueryKey,
} from "@tanstack/react-query";
import { runs } from "@/api/resources";
import { useConnectionStatus, useLiveSignals } from "@/api/stream";
import type { Environment, Run, RunSummary } from "@/api/types";
import { isRealOutcome, summarizeEnv, type EnvSummary } from "@/lib/envSummary";
import { isTerminal } from "@/lib/runOutcome";

// The run data behind env summaries (DESIGN §2.3) — overview, stack page, env header — and the env's
// run history. A summary reads the env's latest runs in the light `view=summary`, plus its primary run
// in full: the checks draw the phase rail and name a failing check, the variable provenance colours
// the graph's edges. The WebSocket keeps them fresh (a run event refetches only the env that owns the
// run); the 60 s poll runs only while the socket is down.

const POLL_MS = 60_000;
export const RECENT_RUNS = 20;
const DEEPER_RUNS = 200;
const MAX_RUNS = 1_000;
export const HISTORY_PAGE = 50;

export const recentRunsKey = (envId: string) => ["env-runs", envId, "recent"] as const;
export const runHistoryKey = (envId: string | undefined) => ["env-runs", envId, "history"] as const;

/** The latest runs, newest first — reaching further back only while no real outcome is in sight: a
 * streak of drift checks or PR plans may push the last apply past the first page. */
export async function fetchRecentRuns(envId: string): Promise<RunSummary[]> {
  let page = await runs.summaryPage(envId, RECENT_RUNS);
  const list = [...page.runs];
  while (page.next && !list.some(isRealOutcome) && list.length < MAX_RUNS) {
    page = await runs.summaryPage(envId, DEEPER_RUNS, page.next);
    list.push(...page.runs);
  }
  return list;
}

export function usePollInterval(): number | false {
  return useConnectionStatus() === "live" ? false : POLL_MS;
}

// Every mounted summary hook (the shell, the page) hears the same signal in the same tick: one
// invalidation per key, not one per listener — each would restart the refetch.
const due = new Map<string, QueryKey>();
function invalidateOnce(qc: QueryClient, keys: QueryKey[]) {
  const first = due.size === 0;
  for (const k of keys) due.set(JSON.stringify(k), k);
  if (!first) return;
  queueMicrotask(() => {
    const batch = [...due.values()];
    due.clear();
    for (const queryKey of batch) qc.invalidateQueries({ queryKey });
  });
}

export interface EnvRunSummary {
  summary: EnvSummary;
  loading: boolean;
}

export function useEnvSummaries(envs: Environment[]): EnvRunSummary[] {
  const qc = useQueryClient();
  const poll = usePollInterval();
  const lists = useQueries({
    queries: envs.map((env) => ({
      queryKey: recentRunsKey(env.id),
      queryFn: () => fetchRecentRuns(env.id),
      refetchInterval: poll,
    })),
  });
  const light = envs.map((env, i) => summarizeEnv(env, lists[i]?.data ?? []));
  const ids = [
    ...new Set(light.flatMap((s) => (s.primary && s.primary.state !== "queued" ? [s.primary.id] : []))),
  ];
  const details = useQueries({
    queries: ids.map((id) => ({
      queryKey: ["run", id],
      queryFn: () => runs.get(id),
      refetchInterval: (q: { state: { data?: Run } }) =>
        q.state.data && isTerminal(q.state.data.state) ? false : poll,
    })),
  });
  const full = new Map(ids.map((id, i) => [id, details[i]?.data]));

  useLiveSignals(
    envs.map((e) => `environment:${e.id}`),
    (s) => {
      if (s.kind !== "run_event" || !("environment_id" in s)) return;
      const env = envs.find((e) => e.id === s.environment_id);
      if (!env) return;
      invalidateOnce(qc, [
        ["env-runs", env.id],
        ["environments", env.stack_id],
        ["run", s.run_id],
      ]);
    },
  );

  return envs.map((_, i) => {
    const s = light[i];
    const detail = s.primary ? full.get(s.primary.id) : undefined;
    const primary: Run | null = s.primary && {
      ...s.primary,
      check_results: detail?.check_results ?? null,
      variable_provenance: detail?.variable_provenance ?? null,
    };
    return { summary: { ...s, primary }, loading: !!lists[i]?.isLoading };
  });
}

/** The env's run history, `HISTORY_PAGE` full runs per page, older pages on demand. */
export function useRunHistory(envId: string | undefined, enabled = true) {
  const poll = usePollInterval();
  return useInfiniteQuery({
    queryKey: runHistoryKey(envId),
    queryFn: ({ pageParam }) => runs.page(envId!, HISTORY_PAGE, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next,
    enabled: !!envId && enabled,
    refetchInterval: poll,
  });
}
