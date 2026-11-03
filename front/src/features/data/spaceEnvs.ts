import { useMemo } from "react";
import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { runs, stacks, tiers } from "@/api/resources";
import { useLiveSignals } from "@/api/stream";
import type { Environment, Run, Stack, TierDef, User } from "@/api/types";
import { summarizeEnv, type EnvSummary } from "@/lib/envSummary";
import { runRights, whoMayConfirm, type RunRights } from "@/lib/permissions";

// Every environment of a space with its summary (DESIGN §2.3) — the data behind the overview matrix,
// the attention zone and the run page's "next decision". Today a fan-out: stacks → environments per
// stack → runs per env (AUDIT A4 would make it one request). Kept fresh by one WebSocket subscription
// per env; the poll is only the fallback while the socket is down.

export interface EnvEntry {
  stack: Stack;
  env: Environment;
  tier: TierDef | undefined;
  runs: Run[];
  summary: EnvSummary;
  /** Rights of the viewer on the env's pending run (null when nothing is pending). */
  rights: RunRights | null;
  /** "an approver with tier prod other than …" for a pending run. */
  whoMayConfirm: string | null;
}

export interface SpaceEnvs {
  entries: EnvEntry[];
  stacks: Stack[];
  loading: boolean;
  /** Entries whose pending run the viewer may confirm, oldest first. */
  needsYou: EnvEntry[];
}

const POLL_MS = 60_000;

export function useSpaceEnvs(spaceId: string | null | undefined, me: User | null | undefined): SpaceEnvs {
  const qc = useQueryClient();
  const stackList = useQuery({ queryKey: ["stacks"], queryFn: stacks.list });
  const catalog = useQuery({ queryKey: ["tiers"], queryFn: tiers.list });
  const spaceStacks = useMemo(
    () =>
      (stackList.data ?? [])
        .filter((s) => s.space_id === spaceId)
        .sort((a, b) => a.name.localeCompare(b.name)),
    [stackList.data, spaceId],
  );
  const envQueries = useQueries({
    queries: spaceStacks.map((s) => ({
      queryKey: ["environments", s.id],
      queryFn: () => stacks.environments(s.id),
      refetchInterval: POLL_MS,
    })),
  });
  // Plain derivations each render: a space holds tens of envs, and the queries are cached.
  const envs = spaceStacks.flatMap((s, i) => (envQueries[i]?.data ?? []).map((env) => ({ stack: s, env })));
  const runQueries = useQueries({
    queries: envs.map(({ env }) => ({
      queryKey: ["env-runs", env.id],
      queryFn: () => runs.list(env.id),
      refetchInterval: POLL_MS,
    })),
  });

  // Live: a run event on any env of the space refetches that env's runs. The signal names the run,
  // not the env: refetch the env that holds the run, or every env for a run we have not seen yet.
  useLiveSignals(
    envs.map(({ env }) => `environment:${env.id}`),
    (s) => {
      if (s.kind !== "run_event" || typeof s.run_id !== "string") return;
      const holder = envs.find(({ env }) =>
        (qc.getQueryData<Run[]>(["env-runs", env.id]) ?? []).some((r) => r.id === s.run_id),
      );
      if (holder) {
        qc.invalidateQueries({ queryKey: ["env-runs", holder.env.id] });
        qc.invalidateQueries({ queryKey: ["environments", holder.stack.id] });
      } else qc.invalidateQueries({ queryKey: ["env-runs"] });
      qc.invalidateQueries({ queryKey: ["run", s.run_id] });
    },
  );

  const entries: EnvEntry[] = envs.map(({ stack, env }, i) => {
    const list = runQueries[i]?.data ?? [];
    const summary = summarizeEnv(env, list);
    const tier = catalog.data?.find((t) => t.name === env.tier);
    const pending = summary.primary?.state === "unconfirmed" ? summary.primary : null;
    const rights = pending && me ? runRights(pending, env, tier, me) : null;
    return {
      stack,
      env,
      tier,
      runs: list,
      summary,
      rights,
      whoMayConfirm: pending && rights ? whoMayConfirm(pending, env, rights) : null,
    };
  });

  const needsYou = entries
    .filter((e) => e.rights?.needsYou)
    .sort((a, b) => a.summary.primary!.created_at.localeCompare(b.summary.primary!.created_at));

  return {
    entries,
    stacks: spaceStacks,
    loading:
      stackList.isLoading || envQueries.some((q) => q.isLoading) || runQueries.some((q) => q.isLoading),
    needsYou,
  };
}
