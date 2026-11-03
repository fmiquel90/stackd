import { useQueries, useQuery } from "@tanstack/react-query";
import { observability, queue, runs, stacks } from "@/api/resources";
import type { Environment, HealthWorker, QueueEntry, Run, Stack } from "@/api/types";

// The execution queue (DESIGN §5.5): `/queue` returns ids only, so names come from the stack and
// environment lists, worker names from `/health`, and ages from one `runs.get` per row.

export const EXECUTING = ["preparing", "planning", "checking", "applying", "running"] as const;
export const WAITING = ["queued", "confirmed"] as const;
/** A waiting run older than this counts in the nav badge. */
export const STUCK_AFTER_S = 120;

export type Group = "executing" | "waiting" | "human";

export function groupOf(state: string): Group | null {
  if ((EXECUTING as readonly string[]).includes(state)) return "executing";
  if ((WAITING as readonly string[]).includes(state)) return "waiting";
  if (state === "unconfirmed") return "human";
  return null;
}

export interface QueueRow {
  entry: QueueEntry;
  group: Group;
  run: Run | undefined;
  stack: Stack | undefined;
  env: Environment | undefined;
  worker: HealthWorker | undefined;
}

export interface Blocker {
  /** The reason in words (DESIGN §5.5) — never a bare code. */
  text: string;
  /** Where to look next. */
  link?: { to: string; label: string };
}

/** Why a waiting run has not started, in words. `rows` lets "another run is active" name that run. */
export function blockerOf(row: QueueRow, rows: QueueRow[], onlineWorkers: number | null): Blocker {
  const { entry } = row;
  switch (entry.blocking_reason) {
    case "active_run": {
      const holder = rows.find(
        (r) =>
          r.entry.environment_id === entry.environment_id &&
          r.entry.run_id !== entry.run_id &&
          r.group !== "waiting",
      );
      return {
        // An unconfirmed plan holds the environment too (SPECS §3.5): the wait is on a person.
        text:
          holder?.group === "human"
            ? "a plan on this environment awaits a human decision"
            : "another run is active on this environment",
        link: holder
          ? { to: `/runs/${holder.entry.run_id}`, label: `run ${holder.entry.run_id.slice(0, 8)}` }
          : undefined,
      };
    }
    case "env_locked":
      return { text: "the environment's state is locked" };
    case "no_compatible_worker":
      return { text: "no worker matches this run's labels", link: { to: "/workers", label: "workers" } };
    case "apply_affinity_hold":
      return {
        text: row.worker
          ? `apply affinity reservation (worker ${row.worker.name})`
          : "apply affinity reservation (the planning worker gets the apply first)",
      };
    case null:
      if (onlineWorkers === 0)
        return { text: "no worker is online", link: { to: "/workers", label: "workers" } };
      return { text: "claimable — waiting for the next worker poll" };
    default:
      return { text: entry.blocking_reason };
  }
}

/** Seconds a waiting run has waited (from its creation), null while its run is not loaded. */
export function waitedSeconds(row: QueueRow, now: Date = new Date()): number | null {
  if (!row.run) return null;
  return Math.max(0, (now.getTime() - new Date(row.run.created_at).getTime()) / 1000);
}

export function matches(row: QueueRow, q: string): boolean {
  if (!q) return true;
  const hay = [
    row.stack?.name,
    row.env?.name,
    row.stack && row.env ? `${row.stack.name}/${row.env.name}` : null,
    row.entry.run_id,
    row.worker?.name,
    row.worker?.pool,
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return q
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((t) => hay.includes(t));
}

export function useQueueRows(pollMs = 3_000) {
  const list = useQuery({ queryKey: ["queue"], queryFn: queue.list, refetchInterval: pollMs });
  const stackList = useQuery({ queryKey: ["stacks"], queryFn: stacks.list });
  const health = useQuery({ queryKey: ["health"], queryFn: observability.health, refetchInterval: 15_000 });
  const entries = (list.data ?? []).filter((e) => groupOf(e.state) !== null);
  const stackIds = [...new Set((stackList.data ?? []).map((s) => s.id))];
  const envQueries = useQueries({
    queries: stackIds.map((id) => ({
      queryKey: ["environments", id],
      queryFn: () => stacks.environments(id),
    })),
  });
  // Runs that will not show in a table (the human group is a count only) are not fetched.
  const runQueries = useQueries({
    queries: entries
      .filter((e) => groupOf(e.state) !== "human")
      .map((e) => ({ queryKey: ["run", e.run_id], queryFn: () => runs.get(e.run_id), staleTime: 10_000 })),
  });
  const envs = envQueries.flatMap((q) => q.data ?? []);
  const runById = new Map(runQueries.flatMap((q) => (q.data ? [[q.data.id, q.data] as const] : [])));
  const workers = health.data?.workers.items ?? [];
  const rows: QueueRow[] = entries.map((entry) => {
    const env = envs.find((e) => e.id === entry.environment_id);
    const run = runById.get(entry.run_id);
    const workerId = entry.worker_id ?? run?.worker_id ?? null;
    return {
      entry,
      group: groupOf(entry.state)!,
      run,
      env,
      stack: env ? stackList.data?.find((s) => s.id === env.stack_id) : undefined,
      worker: workerId ? workers.find((w) => w.id === workerId) : undefined,
    };
  });
  return {
    rows,
    loading: list.isLoading,
    error: list.error as Error | null,
    onlineWorkers: health.data ? health.data.workers.online : null,
  };
}
