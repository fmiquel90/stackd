import { useInfiniteQuery, useMutation, useQueries, useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { audit, observability, stacks, users } from "@/api/resources";
import type { AuditEvent } from "@/api/types";
import { useSpaces } from "@/app/SpaceContext";
import { useIsAdmin } from "@/auth/session";
import { isoLocal, relative } from "@/lib/time";
import { Button } from "@/ui/Button";
import { cn } from "@/ui/cn";
import {
  Badge,
  Banner,
  Checkbox,
  EmptyState,
  Input,
  NativeSelect,
  Skeleton,
  Table,
  Td,
  Th,
} from "@/ui/primitives";
import {
  actionGroups,
  apiParams,
  contextSummary,
  envPath,
  isConsequential,
  isSignIn,
  targetOf,
  type AuditFilters,
  type Lookup,
} from "./auditModel";

// The audit journal (DESIGN §5.6), admin-only. Every filter lives in the URL and maps 1:1 to the
// API's; "Load older" pages with the `to` bound. Sign-ins are hidden unless asked for.

const LIMIT = 200;
const KINDS = [
  "stack",
  "environment",
  "run",
  "variable_set",
  "variable",
  "hook",
  "worker",
  "worker_pool",
  "user",
  "tier",
];
const FILTER_KEYS = ["actor", "action", "kind", "target", "from", "to"] as const;

export function AuditPage() {
  const isAdmin = useIsAdmin();
  if (!isAdmin)
    return (
      <div className="flex flex-col gap-4">
        <h1 className="text-title font-semibold">Audit</h1>
        <EmptyState title="The audit journal is restricted to administrators.">
          Ask an admin for the events you need: every action stays recorded.
        </EmptyState>
      </div>
    );
  return <AuditJournal />;
}

function AuditJournal() {
  const [params, setParams] = useSearchParams();
  const f: AuditFilters = {
    actor: params.get("actor") ?? "",
    action: params.get("action") ?? "",
    kind: params.get("kind") ?? "",
    target: params.get("target") ?? "",
    from: params.get("from") ?? "",
    to: params.get("to") ?? "",
  };
  const signIns = params.get("signins") === "1" || isSignIn(f.action);
  const set = (patch: Partial<Record<(typeof FILTER_KEYS)[number] | "signins", string>>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v) next.set(k, v);
      else next.delete(k);
    }
    setParams(next, { replace: true });
  };
  const filtered = FILTER_KEYS.some((k) => f[k]);

  const { list: spaces } = useSpaces();
  const userList = useQuery({ queryKey: ["users"], queryFn: users.list });
  const stackList = useQuery({ queryKey: ["stacks"], queryFn: stacks.list });
  const health = useQuery({ queryKey: ["health"], queryFn: observability.health });
  const envQueries = useQueries({
    queries: (stackList.data ?? []).map((s) => ({
      queryKey: ["environments", s.id],
      queryFn: () => stacks.environments(s.id),
    })),
  });
  const lookup: Lookup = {
    stacks: stackList.data ?? [],
    envs: envQueries.flatMap((q) => q.data ?? []),
    spaces,
    workers: health.data?.workers.items ?? [],
    users: userList.data ?? [],
  };

  const journal = useInfiniteQuery({
    queryKey: ["audit", f],
    queryFn: ({ pageParam }) => audit.list({ ...apiParams(f, pageParam), limit: String(LIMIT) }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => (last.length === LIMIT ? last[last.length - 1].created_at : undefined),
  });
  // The `to` bound is inclusive: a page repeats its boundary event.
  const events: AuditEvent[] = [];
  const ids = new Set<string>();
  for (const e of journal.data?.pages.flat() ?? []) {
    if (ids.has(e.id)) continue;
    ids.add(e.id);
    events.push(e);
  }
  const shown = signIns ? events : events.filter((e) => !isSignIn(e.action));
  const hiddenSignIns = events.length - shown.length;

  const exportCsv = useMutation({
    mutationFn: () => audit.exportCsv(apiParams(f)),
    onSuccess: (blob) => {
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "audit.csv";
      a.click();
      URL.revokeObjectURL(url);
    },
  });

  const sortedEnvs = lookup.envs
    .map((e) => ({ id: e.id, label: envPath(e, lookup).label }))
    .sort((a, b) => a.label.localeCompare(b.label));

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-title font-semibold">Audit</h1>
        <Button onClick={() => exportCsv.mutate()} loading={exportCsv.isPending}>
          Export CSV{filtered ? " (filtered)" : ""}
        </Button>
      </div>
      {exportCsv.isError && (
        <Banner tone="failed" role="alert" title="The export failed.">
          {(exportCsv.error as Error).message}
        </Banner>
      )}

      <form
        role="search"
        aria-label="Filter the journal"
        className="grid grid-cols-2 gap-3 md:grid-cols-[repeat(3,minmax(0,1fr))] xl:grid-cols-[repeat(6,minmax(0,1fr))]"
        onSubmit={(e) => e.preventDefault()}
      >
        <label className="flex flex-col gap-1">
          <span className="eyebrow text-muted">Actor</span>
          <NativeSelect value={f.actor} onChange={(e) => set({ actor: e.target.value })}>
            <option value="">Anyone</option>
            {(userList.data ?? []).map((u) => (
              <option key={u.id} value={u.id}>
                {u.email}
              </option>
            ))}
          </NativeSelect>
        </label>
        <label className="flex flex-col gap-1">
          <span className="eyebrow text-muted">Action</span>
          <NativeSelect value={f.action} onChange={(e) => set({ action: e.target.value })}>
            <option value="">Any action</option>
            {actionGroups(events.map((e) => e.action)).map(([domain, actions]) => (
              <optgroup key={domain} label={domain}>
                {actions.map((a) => (
                  <option key={a} value={a}>
                    {a}
                  </option>
                ))}
              </optgroup>
            ))}
          </NativeSelect>
        </label>
        <label className="flex flex-col gap-1">
          <span className="eyebrow text-muted">Target kind</span>
          <NativeSelect value={f.kind} onChange={(e) => set({ kind: e.target.value, target: "" })}>
            <option value="">Any target</option>
            {KINDS.map((k) => (
              <option key={k} value={k}>
                {k.replace(/_/g, " ")}
              </option>
            ))}
          </NativeSelect>
        </label>
        <label className="flex flex-col gap-1">
          <span className="eyebrow text-muted">Target</span>
          {f.kind === "stack" ? (
            <NativeSelect value={f.target} onChange={(e) => set({ target: e.target.value })}>
              <option value="">Any stack</option>
              {[...lookup.stacks]
                .sort((a, b) => a.name.localeCompare(b.name))
                .map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
            </NativeSelect>
          ) : f.kind === "environment" ? (
            <NativeSelect value={f.target} onChange={(e) => set({ target: e.target.value })}>
              <option value="">Any environment</option>
              {sortedEnvs.map((e) => (
                <option key={e.id} value={e.id}>
                  {e.label}
                </option>
              ))}
            </NativeSelect>
          ) : (
            <Input
              value={f.target}
              disabled={!f.kind}
              placeholder={f.kind ? `${f.kind.replace(/_/g, " ")} ID` : "pick a kind first"}
              spellCheck={false}
              onChange={(e) => set({ target: e.target.value.trim() })}
            />
          )}
        </label>
        <label className="flex flex-col gap-1">
          <span className="eyebrow text-muted">From</span>
          <Input
            type="date"
            value={f.from}
            max={f.to || undefined}
            onChange={(e) => set({ from: e.target.value })}
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="eyebrow text-muted">To</span>
          <Input
            type="date"
            value={f.to}
            min={f.from || undefined}
            onChange={(e) => set({ to: e.target.value })}
          />
        </label>
      </form>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <label className="flex items-center gap-2 text-ui">
          <Checkbox
            checked={signIns}
            disabled={isSignIn(f.action)}
            onChange={(e) => set({ signins: e.target.checked ? "1" : "" })}
          />
          Show sign-ins
        </label>
        <span className="font-data text-[12px] text-muted" aria-live="polite">
          {shown.length} event{shown.length === 1 ? "" : "s"}
          {hiddenSignIns > 0 && ` · ${hiddenSignIns} sign-in${hiddenSignIns === 1 ? "" : "s"} hidden`}
        </span>
        {(filtered || params.get("signins")) && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setParams(new URLSearchParams(), { replace: true })}
          >
            Clear filters
          </Button>
        )}
      </div>

      {journal.isError && (
        <Banner tone="failed" role="alert" title="The journal could not be loaded.">
          {(journal.error as Error).message}
        </Banner>
      )}

      {journal.isLoading ? (
        <Skeleton className="h-64 w-full" />
      ) : shown.length === 0 ? (
        <EmptyState title={filtered ? "No event matches these filters." : "No event recorded yet."}>
          {hiddenSignIns > 0 && "Only sign-ins match: tick “Show sign-ins” to see them."}
        </EmptyState>
      ) : (
        <>
          <div className="overflow-x-auto max-md:hidden">
            <Table>
              <thead>
                <tr>
                  <Th>When</Th>
                  <Th>Actor</Th>
                  <Th>Action</Th>
                  <Th>Target</Th>
                  <Th>Context</Th>
                </tr>
              </thead>
              <tbody>
                {shown.map((e) => (
                  <EventRow key={e.id} e={e} lookup={lookup} />
                ))}
              </tbody>
            </Table>
          </div>
          <ul className="flex flex-col gap-2 md:hidden">
            {shown.map((e) => (
              <EventCard key={e.id} e={e} lookup={lookup} />
            ))}
          </ul>
        </>
      )}

      {journal.hasNextPage && (
        <Button
          className="self-start"
          loading={journal.isFetchingNextPage}
          onClick={() => journal.fetchNextPage()}
        >
          Load older events
        </Button>
      )}
    </div>
  );
}

function When({ at }: { at: string }) {
  return (
    <time dateTime={at} title={relative(at)} className="font-data text-[12px] whitespace-nowrap">
      {isoLocal(at)}
    </time>
  );
}

function Actor({ e }: { e: AuditEvent }) {
  return <span className="font-data text-[12px]">{e.actor_email ?? e.actor_kind}</span>;
}

function Action({ e }: { e: AuditEvent }) {
  return <Badge className="normal-case tracking-normal">{e.action}</Badge>;
}

function TargetCell({ e, lookup }: { e: AuditEvent; lookup: Lookup }) {
  const t = targetOf(e, lookup);
  if (!t) return <span className="text-muted">—</span>;
  return (
    <span className="font-data text-[12px]">
      {t.to ? (
        <Link to={t.to} className="underline underline-offset-2">
          {t.label}
        </Link>
      ) : (
        t.label
      )}
      {t.gone && <span className="text-muted"> (deleted)</span>}
    </span>
  );
}

function Context({ e, lookup }: { e: AuditEvent; lookup: Lookup }) {
  const parts = contextSummary(e, targetOf(e, lookup));
  if (!parts.length) return <span className="text-muted">—</span>;
  return <span className="font-data text-[12px] text-muted">{parts.join(" · ")}</span>;
}

function EventRow({ e, lookup }: { e: AuditEvent; lookup: Lookup }) {
  return (
    <tr className={cn(isConsequential(e.action) && "bg-raised")}>
      <Td>
        <When at={e.created_at} />
      </Td>
      <Td>
        <Actor e={e} />
      </Td>
      <Td>
        <Action e={e} />
      </Td>
      <Td>
        <TargetCell e={e} lookup={lookup} />
      </Td>
      <Td>
        <Context e={e} lookup={lookup} />
      </Td>
    </tr>
  );
}

function EventCard({ e, lookup }: { e: AuditEvent; lookup: Lookup }) {
  return (
    <li
      className={cn(
        "flex flex-col gap-1 rounded-base border border-line px-3 py-2",
        isConsequential(e.action) ? "bg-raised" : "bg-surface",
      )}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Action e={e} />
        <When at={e.created_at} />
      </div>
      <Actor e={e} />
      <TargetCell e={e} lookup={lookup} />
      <Context e={e} lookup={lookup} />
    </li>
  );
}
