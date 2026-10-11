import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ChevronDown, RefreshCw } from "lucide-react";
import { ApiError } from "@/api/client";
import { environments, runs, stacks, tiers, users } from "@/api/resources";
import { useLiveSignals } from "@/api/stream";
import { useSpaces } from "@/app/SpaceContext";
import { useSession } from "@/auth/session";
import { CommandPanel } from "@/components/CommandPanel";
import { DependenciesPanel } from "@/components/DependenciesPanel";
import { HooksPanel } from "@/components/HooksPanel";
import { NotificationsPanel } from "@/components/NotificationsPanel";
import { CloudPanel } from "@/components/CloudPanel";
import { PromotePanel } from "@/components/PromotePanel";
import { EnvCell } from "@/identity/EnvCell";
import { ModifierChips } from "@/identity/StateBadge";
import { summarizeEnv } from "@/lib/envSummary";
import { needsYouOn } from "@/lib/permissions";
import { Button } from "@/ui/Button";
import { Dialog, DialogContent, Menu, MenuContent, MenuItem, MenuTrigger } from "@/ui/overlays";
import {
  Badge,
  Banner,
  EmptyState,
  RelativeTime,
  Sha,
  Skeleton,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/ui/primitives";
import { EnvInputs, EnvOutputs } from "./EnvInputs";
import { EnvSettings } from "./EnvSettings";
import { EnvState } from "./EnvState";
import { RunsHistory, type RunKind } from "./RunsHistory";

// The environment page (DESIGN §5.8, new in v2): what is deployed versus the branch head, the run
// history, and everything to operate and configure the env. Tabs and filters live in the URL.

type Tab = "runs" | "inputs" | "outputs" | "dependencies" | "state" | "settings";
const TABS: Tab[] = ["runs", "inputs", "outputs", "dependencies", "state", "settings"];

export function EnvPage() {
  const { stack: stackName = "", env: envName = "" } = useParams();
  const { current: space } = useSpaces();
  const me = useSession().data;
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [dialog, setDialog] = useState<null | "promote" | "command" | "destroy">(null);

  const stackList = useQuery({ queryKey: ["stacks"], queryFn: stacks.list });
  const stack = stackList.data?.find((s) => s.space_id === space?.id && s.name === stackName);
  const envs = useQuery({
    queryKey: ["environments", stack?.id],
    queryFn: () => stacks.environments(stack!.id),
    enabled: !!stack,
  });
  const env = envs.data?.find((e) => e.name === envName);
  const runList = useQuery({
    queryKey: ["env-runs", env?.id],
    queryFn: () => runs.list(env!.id),
    enabled: !!env,
    refetchInterval: 60_000,
  });
  const catalog = useQuery({ queryKey: ["tiers"], queryFn: tiers.list });
  const people = useQuery({ queryKey: ["mentionable"], queryFn: users.mentionable, staleTime: 5 * 60_000 });

  useLiveSignals(env ? [`environment:${env.id}`] : [], (s) => {
    if (s.kind !== "run_event" || !env) return;
    qc.invalidateQueries({ queryKey: ["env-runs", env.id] });
    qc.invalidateQueries({ queryKey: ["environments", env.stack_id] });
  });

  const plan = useMutation({
    mutationFn: (opts: { type?: "tracked" | "destroy"; downstream?: boolean }) =>
      runs.trigger(env!.id, opts.type ?? "tracked", !!opts.downstream),
    onSuccess: (r) => navigate(`/runs/${r.id}`),
  });
  const refresh = useMutation({
    mutationFn: () => environments.refreshHead(env!.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["environments", env!.stack_id] }),
  });

  if (stackList.isLoading || envs.isLoading) return <Skeleton className="h-64 w-full" />;
  if (!space || !stack || !env)
    return (
      <EmptyState title="Environment not found">
        No environment <span className="font-data">{envName}</span> in stack{" "}
        <span className="font-data">{stackName}</span>.{" "}
        <Link to="/" className="link">
          Go to the overview
        </Link>
      </EmptyState>
    );

  const tab = (TABS as string[]).includes(params.get("tab") ?? "") ? (params.get("tab") as Tab) : "runs";
  const setParam = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null || v === "" || v === "all") next.delete(k);
      else next.set(k, v);
    }
    setParams(next, { replace: true });
  };
  const summary = summarizeEnv(env, runList.data ?? []);
  const tier = catalog.data?.find((t) => t.name === env.tier);
  const canWrite = !!me && !me.disabled && me.role !== "reader";
  const isAdmin = me?.role === "admin";
  const stackPath = `/s/${encodeURIComponent(space.name)}/stacks/${encodeURIComponent(stack.name)}`;
  const emailOf = (id: string | null) => (id ? (people.data?.find((u) => u.id === id)?.email ?? null) : null);
  const siblings = (envs.data ?? []).filter((e) => e.id !== env.id).map((e) => ({ id: e.id, name: e.name }));
  const error = [plan, refresh].find((m) => m.error)?.error;

  return (
    <div className="flex flex-col gap-4">
      <header className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-title font-semibold">
            <Link to={stackPath} className="hover:underline">
              {stack.name}
            </Link>{" "}
            / {env.name}
          </h1>
          <Badge>tier {env.tier}</Badge>
          {tier?.requires_four_eyes && <Badge>four-eyes</Badge>}
          {env.protected && <Badge>protected</Badge>}
          {env.require_second_pair_of_eyes && <Badge>second pair of eyes</Badge>}
          {env.autodeploy && <Badge>autodeploy</Badge>}
          {env.managed_state && <Badge>managed state</Badge>}
          <span className="ml-auto flex items-center gap-2">
            {canWrite && (
              <>
                <Button
                  variant="primary"
                  loading={plan.isPending && !plan.variables?.type}
                  onClick={() => plan.mutate({})}
                >
                  Plan
                </Button>
                <Menu>
                  <MenuTrigger asChild>
                    <Button icon={<ChevronDown size={14} aria-hidden />}>More</Button>
                  </MenuTrigger>
                  <MenuContent align="end">
                    <MenuItem onSelect={() => plan.mutate({ downstream: true })}>
                      Plan with downstream
                    </MenuItem>
                    <MenuItem onSelect={() => setDialog("promote")}>Promote from a sibling…</MenuItem>
                    <MenuItem onSelect={() => setDialog("command")}>Run a command…</MenuItem>
                    {me?.can_destroy && (
                      <MenuItem onSelect={() => setDialog("destroy")}>Plan a destroy…</MenuItem>
                    )}
                  </MenuContent>
                </Menu>
              </>
            )}
          </span>
        </div>

        <div className="flex flex-wrap items-center gap-x-6 gap-y-2 rounded-base border border-line bg-surface px-3 py-2">
          <div className="min-w-[220px]">
            <div className="eyebrow mb-1 text-muted">{summary.active ? "Active run" : "Last outcome"}</div>
            <EnvCell summary={summary} needsYou={needsYouOn(summary.primary, env, tier, me)} />
          </div>
          <dl className="grid grid-cols-[auto_auto] items-center gap-x-3 gap-y-1 text-[12px]">
            <dt className="text-muted">Branch</dt>
            <dd className="font-data">{env.branch}</dd>
            <dt className="text-muted">Deployed</dt>
            <dd>
              <Sha value={summary.deployedSha} copy />
            </dd>
            <dt className="text-muted">Head</dt>
            <dd className="flex items-center gap-2">
              <Sha value={env.head_sha} copy />
              <ModifierChips modifiers={{ stale: summary.modifiers.stale }} />
              {canWrite && (
                <Button
                  size="sm"
                  variant="ghost"
                  loading={refresh.isPending}
                  onClick={() => refresh.mutate()}
                  icon={<RefreshCw size={12} aria-hidden />}
                >
                  Refresh
                </Button>
              )}
            </dd>
          </dl>
          <div className="flex flex-col gap-1 text-[12px]">
            {summary.modifiers.drift ? (
              <span className="flex items-center gap-2">
                <ModifierChips modifiers={{ drift: summary.modifiers.drift }} />
                {env.drift_run_id && (
                  <Link to={`/runs/${env.drift_run_id}?tab=plan`} className="underline underline-offset-2">
                    see the drift
                  </Link>
                )}
              </span>
            ) : (
              <span className="text-muted">
                {env.drift_check_enabled ? "No drift detected" : "Drift detection off"}
                {env.last_drift_checked_at && (
                  <>
                    {" "}
                    · checked <RelativeTime value={env.last_drift_checked_at} />
                  </>
                )}
              </span>
            )}
            {env.locked && <ModifierChips modifiers={{ locked: true }} />}
          </div>
        </div>
        {error && (
          <Banner tone="failed" role="alert" title="The API refused the action">
            {error instanceof ApiError ? (error.detail ?? error.title) : String(error)}
          </Banner>
        )}
      </header>

      <Tabs value={tab} onValueChange={(v) => setParam({ tab: v === "runs" ? null : v })}>
        <TabsList>
          <TabsTrigger value="runs">
            Runs <span className="font-data text-[12px] text-muted">{runList.data?.length ?? ""}</span>
          </TabsTrigger>
          <TabsTrigger value="inputs">Inputs</TabsTrigger>
          <TabsTrigger value="outputs">Outputs</TabsTrigger>
          <TabsTrigger value="dependencies">Dependencies</TabsTrigger>
          <TabsTrigger value="state">State</TabsTrigger>
          <TabsTrigger value="settings">Settings</TabsTrigger>
        </TabsList>
        <TabsContent value="runs" className="pt-3">
          {runList.isLoading ? (
            <Skeleton className="h-40 w-full" />
          ) : (
            <RunsHistory
              runs={runList.data ?? []}
              kind={(params.get("kind") as RunKind) ?? "all"}
              onKind={(k) => setParam({ kind: k })}
              emailOf={emailOf}
            />
          )}
        </TabsContent>
        <TabsContent value="inputs" className="pt-3">
          <EnvInputs envId={env.id} canWrite={canWrite} />
        </TabsContent>
        <TabsContent value="outputs" className="pt-3">
          <EnvOutputs envId={env.id} />
        </TabsContent>
        <TabsContent value="dependencies" className="max-w-[960px] pt-3">
          <DependenciesPanel envId={env.id} />
        </TabsContent>
        <TabsContent value="state" className="pt-3">
          <EnvState env={env} isAdmin={isAdmin} />
        </TabsContent>
        <TabsContent value="settings" className="flex flex-col gap-6 pt-3">
          <EnvSettings
            key={env.id}
            env={env}
            canWrite={canWrite}
            isAdmin={isAdmin}
            onRenamed={(name) =>
              navigate(`${stackPath}/envs/${encodeURIComponent(name)}?tab=settings`, { replace: true })
            }
            onDeleted={() => navigate(stackPath)}
          />
          <section className="flex max-w-[960px] flex-col gap-3">
            <h2 className="text-section font-semibold">Hooks</h2>
            <HooksPanel scope="environments" id={env.id} />
            <h2 className="text-section font-semibold">Notifications</h2>
            <NotificationsPanel scope="environments" id={env.id} />
            <h2 className="text-section font-semibold">Cloud credentials</h2>
            <CloudPanel envId={env.id} />
          </section>
        </TabsContent>
      </Tabs>

      <Dialog open={dialog === "promote"} onOpenChange={(v) => setDialog(v ? "promote" : null)}>
        <DialogContent
          title={`Promote to ${env.name}`}
          description="Re-deploy the commit currently applied on a sibling environment. The apply is confirmed as usual."
        >
          <PromotePanel envId={env.id} siblings={siblings} />
        </DialogContent>
      </Dialog>
      <Dialog open={dialog === "command"} onOpenChange={(v) => setDialog(v ? "command" : null)}>
        <DialogContent
          wide
          title={`Run a command on ${env.name}`}
          description="One allowlisted subcommand. Mutating commands need the apply permission."
        >
          <CommandPanel envId={env.id} />
        </DialogContent>
      </Dialog>
      <Dialog open={dialog === "destroy"} onOpenChange={(v) => setDialog(v ? "destroy" : null)}>
        <DialogContent title={`Plan a destroy of ${env.name}`}>
          <p className="mb-3">
            This plans the destruction of every resource of the environment. Nothing is destroyed until
            someone with the destroy permission confirms it — by typing{" "}
            <span className="font-data">{env.name}</span>.
          </p>
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setDialog(null)}>
              Cancel
            </Button>
            <Button
              variant="danger"
              loading={plan.isPending && plan.variables?.type === "destroy"}
              onClick={() => plan.mutate({ type: "destroy" })}
            >
              Plan the destroy
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
