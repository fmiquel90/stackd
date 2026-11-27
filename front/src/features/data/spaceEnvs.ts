import { useMemo } from "react";
import { useQueries, useQuery } from "@tanstack/react-query";
import { stacks, tiers } from "@/api/resources";
import type { Environment, Stack, TierDef, User } from "@/api/types";
import type { EnvSummary } from "@/lib/envSummary";
import { runRights, whoMayConfirm, type RunRights } from "@/lib/permissions";
import { useEnvSummaries } from "./envRuns";

// Every environment of a space with its summary (DESIGN §2.3) — the data behind the overview matrix,
// the attention zone and the run page's "next decision". Today a fan-out: stacks → environments per
// stack → a summary per env (`useEnvSummaries`); one request per space is a follow-up (AUDIT A4).

export interface EnvEntry {
  stack: Stack;
  env: Environment;
  tier: TierDef | undefined;
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

// Env fields no run event announces (the branch head, §9.6) still need a poll.
const ENV_POLL_MS = 60_000;

export function useSpaceEnvs(spaceId: string | null | undefined, me: User | null | undefined): SpaceEnvs {
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
      refetchInterval: ENV_POLL_MS,
    })),
  });
  // Plain derivations each render: a space holds tens of envs, and the queries are cached.
  const envs = spaceStacks.flatMap((s, i) => (envQueries[i]?.data ?? []).map((env) => ({ stack: s, env })));
  const summaries = useEnvSummaries(envs.map(({ env }) => env));

  const entries: EnvEntry[] = envs.map(({ stack, env }, i) => {
    const summary = summaries[i].summary;
    const tier = catalog.data?.find((t) => t.name === env.tier);
    const pending = summary.primary?.state === "unconfirmed" ? summary.primary : null;
    const rights = pending && me ? runRights(pending, env, tier, me) : null;
    return {
      stack,
      env,
      tier,
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
    loading: stackList.isLoading || envQueries.some((q) => q.isLoading) || summaries.some((s) => s.loading),
    needsYou,
  };
}
