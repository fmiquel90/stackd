import { useMemo } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { ApiError } from "@/api/client";
import { graphApi, runs, tiers, users } from "@/api/resources";
import { useSpaces } from "@/app/SpaceContext";
import { useSession } from "@/auth/session";
import { useSpaceEnvs, type EnvEntry } from "@/features/data/spaceEnvs";
import { EnvCell } from "@/identity/EnvCell";
import { ModifierChips, RunStateBadge } from "@/identity/StateBadge";
import { downstreamOf, type CascadeStep } from "@/lib/cascade";
import { Button } from "@/ui/Button";
import { Banner, EmptyState, Input, NativeSelect, RelativeTime, Sha, Skeleton } from "@/ui/primitives";
import { AttentionZone } from "./AttentionZone";
import { attentionOf, envColumns, matchesState, STATE_FILTERS, type StateFilter } from "./attention";
import { Matrix, type Selection } from "./Matrix";
import { NewStackDialog } from "./NewStackDialog";

// The overview (DESIGN §5.1): attention zone, then the stack × environment matrix. Filters, the
// selected cell and the cascade preview live in the URL (?q, ?state, ?tier, ?sel, ?cascade).

export function OverviewPage() {
  const { current: space, list: spaces } = useSpaces();
  const me = useSession().data ?? null;
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const data = useSpaceEnvs(space?.id, me);
  const catalog = useQuery({ queryKey: ["tiers"], queryFn: tiers.list });
  const people = useQuery({ queryKey: ["mentionable"], queryFn: users.mentionable, staleTime: 5 * 60_000 });
  const cascadeOn = params.get("cascade") === "1";
  const graph = useQuery({
    queryKey: ["graph"],
    queryFn: graphApi.get,
    staleTime: 60_000,
    enabled: cascadeOn,
  });

  const set = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null || v === "" || v === "all") next.delete(k);
      else next.set(k, v);
    }
    setParams(next, { replace: true });
  };

  const spaceBase = space ? `/s/${encodeURIComponent(space.name)}` : "/";
  const envHref = (stack: string, env: string) =>
    `${spaceBase}/stacks/${encodeURIComponent(stack)}/envs/${encodeURIComponent(env)}`;
  const emailOf = (id: string | null | undefined) =>
    id ? (people.data?.find((u) => u.id === id)?.email ?? null) : null;

  const q = (params.get("q") ?? "").trim().toLowerCase();
  const stateFilter = (params.get("state") ?? "all") as StateFilter;
  const tierFilter = params.get("tier") ?? "";

  const attention = data.entries
    .map((e) => attentionOf(e, envHref(e.stack.name, e.env.name)))
    .filter((x) => x !== null);

  const visibleEntries = data.entries.filter((e) => !tierFilter || e.env.tier === tierFilter);
  const columns = envColumns(visibleEntries, catalog.data);
  const byKey = new Map(visibleEntries.map((e) => [`${e.stack.name}/${e.env.name}`, e]));
  const rows = data.stacks.filter((s) => {
    const mine = visibleEntries.filter((e) => e.stack.id === s.id);
    const textOk =
      !q || s.name.toLowerCase().includes(q) || mine.some((e) => e.env.name.toLowerCase().includes(q));
    const stateOk =
      stateFilter === "all" || mine.some((e) => matchesState(e, stateFilter, envHref(s.name, e.env.name)));
    return textOk && stateOk;
  });
  const fourEyesColumns = new Set(
    columns.filter((c) => {
      const inCol = visibleEntries.filter((e) => e.env.name === c);
      return inCol.length > 0 && inCol.every((e) => e.tier?.requires_four_eyes);
    }),
  );

  const sel: Selection | null = (() => {
    const v = params.get("sel");
    if (!v || !v.includes("/")) return null;
    const [stack, env] = v.split("/");
    return { stack, env };
  })();
  const selectedEntry = sel ? byKey.get(`${sel.stack}/${sel.env}`) : undefined;
  const downstream = useMemo(() => {
    if (!cascadeOn || !selectedEntry || !graph.data) return new Map<string, CascadeStep>();
    return new Map(downstreamOf(selectedEntry.env.id, graph.data.edges).map((s) => [s.envId, s]));
  }, [cascadeOn, selectedEntry, graph.data]);

  if (!space) return <OverviewSkeleton />;

  const tierNames = (catalog.data ?? []).map((t) => t.name);
  const canWrite = me && me.role !== "reader";
  const otherSpacesWithStacks = spaces.filter((s) => s.id !== space.id);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-title font-semibold">
          Overview <span className="font-data text-[13px] font-normal text-muted">{space.name}</span>
        </h1>
        <div className="ml-auto flex items-center gap-2">
          {canWrite && <NewStackDialog spaceId={space.id} spaceName={space.name} />}
        </div>
      </div>

      {data.loading && !data.entries.length ? (
        <OverviewSkeleton />
      ) : data.stacks.length === 0 ? (
        <EmptyState
          title={`No stack in ${space.name} yet.`}
          action={canWrite ? <NewStackDialog spaceId={space.id} spaceName={space.name} /> : undefined}
        >
          Connect a repository to get started.
          {otherSpacesWithStacks.length > 0 && (
            <span>
              {" "}
              Other spaces:{" "}
              {otherSpacesWithStacks.map((s, i) => (
                <span key={s.id}>
                  {i > 0 && ", "}
                  <Link className="link font-data" to={`/s/${encodeURIComponent(s.name)}`}>
                    {s.name}
                  </Link>
                </span>
              ))}
            </span>
          )}
        </EmptyState>
      ) : (
        <>
          <AttentionZone items={attention} spaceName={space.name} emailOf={emailOf} />

          <div className="flex flex-wrap items-end gap-2" role="search" aria-label="Filter the matrix">
            <label className="flex flex-col gap-1">
              <span className="eyebrow text-muted">Filter</span>
              <Input
                value={params.get("q") ?? ""}
                onChange={(e) => set({ q: e.target.value })}
                placeholder="stack or environment"
                className="w-56"
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="eyebrow text-muted">State</span>
              <NativeSelect value={stateFilter} onChange={(e) => set({ state: e.target.value })}>
                {STATE_FILTERS.map((f) => (
                  <option key={f.value} value={f.value}>
                    {f.label}
                  </option>
                ))}
              </NativeSelect>
            </label>
            <label className="flex flex-col gap-1">
              <span className="eyebrow text-muted">Tier</span>
              <NativeSelect value={tierFilter} onChange={(e) => set({ tier: e.target.value })}>
                <option value="">All tiers</option>
                {tierNames.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </NativeSelect>
            </label>
            <span className="ml-auto self-center font-data text-[12px] text-muted max-md:hidden">
              h/j/k/l move · ⏎ open · r run · s downstream
            </span>
          </div>

          {rows.length === 0 ? (
            <EmptyState title="No stack matches these filters.">
              <button
                type="button"
                className="link"
                onClick={() => setParams(new URLSearchParams(), { replace: true })}
              >
                Clear the filters
              </button>
            </EmptyState>
          ) : (
            <>
              <div className="max-md:hidden">
                <Matrix
                  stacks={rows}
                  columns={columns}
                  byKey={byKey}
                  selected={sel}
                  onSelect={(s) => set({ sel: `${s.stack}/${s.env}` })}
                  downstream={downstream}
                  envHref={envHref}
                  onToggleCascade={() => set({ cascade: cascadeOn ? null : "1" })}
                  fourEyesColumns={fourEyesColumns}
                />
              </div>
              <MobileList rows={rows} entries={visibleEntries} envHref={envHref} />
              {selectedEntry && (
                <SelectionDetails
                  entry={selectedEntry}
                  envHref={envHref(selectedEntry.stack.name, selectedEntry.env.name)}
                  emailOf={emailOf}
                  canWrite={!!canWrite}
                  cascadeOn={cascadeOn}
                  onToggleCascade={() => set({ cascade: cascadeOn ? null : "1" })}
                  downstream={[...downstream.values()]}
                  labelOf={(envId) => {
                    const e = data.entries.find((x) => x.env.id === envId);
                    return e ? `${e.stack.name}/${e.env.name}` : envId.slice(0, 8);
                  }}
                  onPlanned={(id) => navigate(`/runs/${id}`)}
                />
              )}
            </>
          )}
        </>
      )}
    </div>
  );
}

function SelectionDetails({
  entry,
  envHref,
  emailOf,
  canWrite,
  cascadeOn,
  onToggleCascade,
  downstream,
  labelOf,
  onPlanned,
}: {
  entry: EnvEntry;
  envHref: string;
  emailOf: (id: string | null | undefined) => string | null;
  canWrite: boolean;
  cascadeOn: boolean;
  onToggleCascade: () => void;
  downstream: CascadeStep[];
  labelOf: (envId: string) => string;
  onPlanned: (runId: string) => void;
}) {
  const { env, stack, summary } = entry;
  const run = summary.primary;
  const plan = useMutation({ mutationFn: () => runs.trigger(env.id), onSuccess: (r) => onPlanned(r.id) });
  const who = run?.triggered_by === "manual" ? emailOf(run.trigger_user_id) : run?.triggered_by;
  return (
    <section
      aria-label={`${stack.name} / ${env.name}`}
      className="flex flex-col gap-2 rounded-base border border-line bg-surface p-3 max-md:hidden"
    >
      <div className="flex flex-wrap items-center gap-3">
        <Link to={envHref} className="font-data text-[13px] font-semibold hover:underline">
          {stack.name} / {env.name}
        </Link>
        <span className="font-data text-[12px] text-muted">
          tier {env.tier} · branch {env.branch}
        </span>
        <ModifierChips modifiers={summary.modifiers} />
        <span className="ml-auto flex gap-2">
          <Button size="sm" variant="ghost" onClick={onToggleCascade} aria-pressed={cascadeOn}>
            {cascadeOn ? "Hide downstream" : "Show downstream"}
          </Button>
          {run && (
            <Link
              to={`/runs/${run.id}`}
              className="inline-flex h-7 items-center rounded-base border border-control px-2.5 text-[12px] hover:bg-raised"
            >
              Open run
            </Link>
          )}
          <Link
            to={envHref}
            className="inline-flex h-7 items-center rounded-base border border-control px-2.5 text-[12px] hover:bg-raised"
          >
            Open environment
          </Link>
          {canWrite && !summary.active && (
            <Button size="sm" variant="primary" loading={plan.isPending} onClick={() => plan.mutate()}>
              Plan
            </Button>
          )}
        </span>
      </div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[12px] sm:grid-cols-[auto_1fr_auto_1fr]">
        <dt className="text-muted">Deployed commit</dt>
        <dd>
          <Sha value={summary.deployedSha} copy />
        </dd>
        <dt className="text-muted">Branch head</dt>
        <dd>
          <Sha value={env.head_sha} copy />{" "}
          {summary.modifiers.stale && <span className="text-muted">(ahead of deployed)</span>}
        </dd>
        <dt className="text-muted">{summary.active ? "Active run" : "Last outcome"}</dt>
        <dd className="flex flex-wrap items-center gap-2">
          {run ? (
            <>
              <RunStateBadge
                run={run}
                decision={
                  run.state === "unconfirmed" ? (entry.rights?.needsYou ? "needsYou" : "waiting") : undefined
                }
              />
              <span className="text-muted">
                {who ? `${who} · ` : ""}
                <RelativeTime value={run.finished_at ?? run.created_at} />
              </span>
            </>
          ) : (
            <span className="text-muted">never run</span>
          )}
        </dd>
        {entry.whoMayConfirm && (
          <>
            <dt className="text-muted">Confirm needs</dt>
            <dd>{entry.whoMayConfirm}</dd>
          </>
        )}
      </dl>
      {cascadeOn && (
        <div className="text-[12px]">
          <span className="eyebrow mr-2 text-muted">Downstream</span>
          {downstream.length === 0
            ? "nothing — no dependency starts from this environment."
            : downstream.map((s, i) => (
                <span key={s.envId} className="font-data">
                  {i > 0 && ", "}
                  {labelOf(s.envId)}
                  <span className="text-muted">
                    {s.likelihood === "will_plan" ? " (will plan)" : " (if outputs change)"}
                  </span>
                </span>
              ))}
        </div>
      )}
      {plan.error && (
        <Banner tone="failed" role="alert" title="The API refused the plan">
          {plan.error instanceof ApiError ? (plan.error.detail ?? plan.error.title) : String(plan.error)}
        </Banner>
      )}
    </section>
  );
}

/** < 768 px: one block per stack, its environments as full-width rows (DESIGN §5.1). */
function MobileList({
  rows,
  entries,
  envHref,
}: {
  rows: { id: string; name: string }[];
  entries: EnvEntry[];
  envHref: (s: string, e: string) => string;
}) {
  return (
    <ul className="flex flex-col gap-3 md:hidden">
      {rows.map((s) => (
        <li key={s.id} className="rounded-base border border-line bg-surface">
          <div className="border-b border-line px-3 py-2 font-data text-[13px]">{s.name}</div>
          <ul>
            {entries
              .filter((e) => e.stack.id === s.id)
              .map((e) => (
                <li key={e.env.id} className="border-b border-line last:border-b-0">
                  <Link
                    to={envHref(s.name, e.env.name)}
                    className="flex min-h-11 items-center gap-3 px-2 py-1 hover:bg-raised"
                  >
                    <span className="w-16 shrink-0 font-data text-[12px]">{e.env.name}</span>
                    <EnvCell summary={e.summary} needsYou={!!e.rights?.needsYou} className="min-w-0 flex-1" />
                  </Link>
                </li>
              ))}
          </ul>
        </li>
      ))}
    </ul>
  );
}

function OverviewSkeleton() {
  return (
    <div className="flex flex-col gap-3" aria-busy="true">
      <span className="sr-only">Loading the overview…</span>
      <Skeleton className="h-24 w-full" />
      <Skeleton className="h-8 w-96 max-w-full" />
      <Skeleton className="h-64 w-full" />
    </div>
  );
}
