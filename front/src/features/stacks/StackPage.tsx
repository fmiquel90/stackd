import { useState } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { Plus } from "lucide-react";
import { ApiError } from "@/api/client";
import { runs, stacks, tiers, type NewEnvironment } from "@/api/resources";
import { useLiveSignals } from "@/api/stream";
import { useSpaces } from "@/app/SpaceContext";
import { useSession } from "@/auth/session";
import { Checkbox } from "@/components/ui";
import { HooksPanel } from "@/components/HooksPanel";
import { NotificationsPanel } from "@/components/NotificationsPanel";
import { SecretSourcesPanel } from "@/components/SecretSourcesPanel";
import { StackGeneralPanel } from "@/components/StackGeneralPanel";
import { VariablesEditor } from "@/components/VariablesEditor";
import { EnvCell } from "@/identity/EnvCell";
import { summarizeEnv } from "@/lib/envSummary";
import { needsYouOn } from "@/lib/permissions";
import { Button } from "@/ui/Button";
import { Dialog, DialogContent, DialogTrigger } from "@/ui/overlays";
import {
  Badge,
  Banner,
  EmptyState,
  Field,
  Input,
  NativeSelect,
  Sha,
  Skeleton,
  Table,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  Td,
  Th,
} from "@/ui/primitives";

// The stack page (DESIGN §5.8): its environments (each summarised by the §2.3 rule) and its settings.

type Tab = "environments" | "settings";
type Sub = "general" | "variables" | "hooks" | "notifications" | "secrets";
const SUBS: { key: Sub; label: string }[] = [
  { key: "general", label: "General" },
  { key: "variables", label: "Variables" },
  { key: "hooks", label: "Hooks" },
  { key: "notifications", label: "Notifications" },
  { key: "secrets", label: "Secret sources" },
];

export function StackPage({ stackId }: { stackId: string }) {
  const { current: space } = useSpaces();
  const me = useSession().data;
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const stack = useQuery({ queryKey: ["stack", stackId], queryFn: () => stacks.get(stackId) });
  const envs = useQuery({ queryKey: ["environments", stackId], queryFn: () => stacks.environments(stackId) });
  const catalog = useQuery({ queryKey: ["tiers"], queryFn: tiers.list });
  const envList = [...(envs.data ?? [])].sort(
    (a, b) => a.position - b.position || a.name.localeCompare(b.name),
  );
  const runQueries = useQueries({
    queries: envList.map((e) => ({
      queryKey: ["env-runs", e.id],
      queryFn: () => runs.list(e.id),
      refetchInterval: 60_000,
    })),
  });
  useLiveSignals(
    envList.map((e) => `environment:${e.id}`),
    (s) => {
      if (s.kind === "run_event") qc.invalidateQueries({ queryKey: ["env-runs"] });
    },
  );

  const tab: Tab = params.get("tab") === "settings" ? "settings" : "environments";
  const sub = (SUBS.find((s) => s.key === params.get("sub"))?.key ?? "general") as Sub;
  const setParam = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (!v) next.delete(k);
      else next.set(k, v);
    }
    setParams(next, { replace: true });
  };

  if (!stack.data || !space) return <Skeleton className="h-64 w-full" />;
  const s = stack.data;
  const canWrite = !!me && !me.disabled && me.role !== "reader";
  const base = `/s/${encodeURIComponent(space.name)}/stacks/${encodeURIComponent(s.name)}`;

  return (
    <div className="flex flex-col gap-4">
      <header className="flex flex-wrap items-center gap-3">
        <div className="min-w-0">
          <h1 className="text-title font-semibold">{s.name}</h1>
          <p className="font-data text-[12px] break-all text-muted">
            {s.repo_url}
            {s.project_root && s.project_root !== "." ? ` · ${s.project_root}` : ""} · {s.tool}{" "}
            {s.tool_version}
          </p>
        </div>
      </header>

      <Tabs value={tab} onValueChange={(v) => setParam({ tab: v === "environments" ? null : v, sub: null })}>
        <TabsList>
          <TabsTrigger value="environments">
            Environments <span className="font-data text-[12px] text-muted">{envList.length}</span>
          </TabsTrigger>
          <TabsTrigger value="settings">Settings</TabsTrigger>
        </TabsList>

        <TabsContent value="environments" className="flex flex-col gap-3 pt-3">
          {canWrite && (
            <div>
              <NewEnvironmentDialog stackId={stackId} base={base} />
            </div>
          )}
          {envs.isLoading ? (
            <Skeleton className="h-40 w-full" />
          ) : envList.length === 0 ? (
            <EmptyState title="No environment yet.">
              Add one to plan this stack (dev, staging, prod…).
            </EmptyState>
          ) : (
            <div className="overflow-x-auto rounded-base border border-line bg-surface">
              <Table>
                <thead>
                  <tr>
                    <Th>Environment</Th>
                    <Th>State</Th>
                    <Th>Tier</Th>
                    <Th className="max-md:hidden">Branch</Th>
                    <Th className="max-md:hidden">Deployed</Th>
                    <Th className="max-md:hidden">Protections</Th>
                  </tr>
                </thead>
                <tbody>
                  {envList.map((e, i) => {
                    const summary = summarizeEnv(e, runQueries[i]?.data ?? []);
                    const tierDef = catalog.data?.find((t) => t.name === e.tier);
                    const four = tierDef?.requires_four_eyes;
                    return (
                      <tr key={e.id} className="hover:bg-raised">
                        <Td>
                          <Link
                            to={`${base}/envs/${encodeURIComponent(e.name)}`}
                            className="font-data text-[13px] underline underline-offset-2"
                          >
                            {e.name}
                          </Link>
                        </Td>
                        <Td className="min-w-[220px]">
                          <EnvCell summary={summary} needsYou={needsYouOn(summary.primary, e, tierDef, me)} />
                        </Td>
                        <Td className="font-data text-[12px]">{e.tier}</Td>
                        <Td className="font-data text-[12px] max-md:hidden">{e.branch}</Td>
                        <Td className="max-md:hidden">
                          <Sha value={summary.deployedSha} />
                        </Td>
                        <Td className="max-md:hidden">
                          <span className="flex flex-wrap gap-1">
                            {four && <Badge>four-eyes</Badge>}
                            {e.protected && <Badge>protected</Badge>}
                            {e.require_second_pair_of_eyes && <Badge>2nd eyes</Badge>}
                            {e.autodeploy && <Badge>autodeploy</Badge>}
                          </span>
                        </Td>
                      </tr>
                    );
                  })}
                </tbody>
              </Table>
            </div>
          )}
        </TabsContent>

        <TabsContent value="settings" className="flex flex-col gap-3 pt-3">
          <nav aria-label="Stack settings" className="flex flex-wrap gap-1">
            {SUBS.map((x) => (
              <Button
                key={x.key}
                size="sm"
                variant={x.key === sub ? "secondary" : "ghost"}
                aria-current={x.key === sub ? "page" : undefined}
                onClick={() => setParam({ sub: x.key })}
              >
                {x.label}
              </Button>
            ))}
          </nav>
          <div className="max-w-[960px]">
            {sub === "general" && <StackGeneralPanel stackId={stackId} />}
            {sub === "variables" && (
              <div className="flex flex-col gap-2">
                <p className="text-[12px] text-muted">
                  Common to every environment of this stack; an environment variable of the same name
                  overrides it.
                </p>
                <VariablesEditor
                  queryKey={["stack-vars", stackId]}
                  list={() => stacks.variables(stackId)}
                  add={(body) => stacks.addVariable(stackId, body)}
                  update={(varId, body) => stacks.updateVariable(stackId, varId, body)}
                  remove={(varId) => stacks.removeVariable(stackId, varId)}
                />
              </div>
            )}
            {sub === "hooks" && <HooksPanel scope="stacks" id={stackId} />}
            {sub === "notifications" && <NotificationsPanel scope="stacks" id={stackId} />}
            {sub === "secrets" && <SecretSourcesPanel spaceId={s.space_id} />}
          </div>
        </TabsContent>
      </Tabs>
    </div>
  );
}

function NewEnvironmentDialog({ stackId, base }: { stackId: string; base: string }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const catalog = useQuery({ queryKey: ["tiers"], queryFn: tiers.list });
  const repo = useQuery({
    queryKey: ["repo-branches", stackId],
    queryFn: () => stacks.checkRepo(stackId),
    enabled: open,
    staleTime: 60_000,
    retry: false,
  });
  const [form, setForm] = useState<NewEnvironment>({ name: "", tier: "", branch: "", managed_state: true });
  const branches = repo.data?.ok ? repo.data.branches : [];
  const tier = form.tier || catalog.data?.[0]?.name || "";
  const branch = form.branch || (branches.includes("main") ? "main" : branches[0]) || "main";
  const create = useMutation({
    mutationFn: () => stacks.createEnvironment(stackId, { ...form, tier, branch }),
    onSuccess: (e) => {
      qc.invalidateQueries({ queryKey: ["environments", stackId] });
      setOpen(false);
      navigate(`${base}/envs/${encodeURIComponent(e.name)}`);
    },
  });
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="primary" icon={<Plus size={14} aria-hidden />}>
          Add environment
        </Button>
      </DialogTrigger>
      <DialogContent title="Add an environment">
        <form
          className="flex flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            create.mutate();
          }}
        >
          <Field label="Name" help="Unique in the stack: dev, staging, prod…">
            <Input
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              required
              autoFocus
            />
          </Field>
          <Field label="Tier" help="Decides who may confirm applies and whether four-eyes applies.">
            <NativeSelect value={tier} onChange={(e) => setForm({ ...form, tier: e.target.value })}>
              {(catalog.data ?? []).map((t) => (
                <option key={t.name} value={t.name}>
                  {t.name}
                  {t.requires_four_eyes ? " (four-eyes)" : ""}
                </option>
              ))}
            </NativeSelect>
          </Field>
          <Field
            label="Branch"
            help={
              repo.isLoading
                ? "Reading the repository's branches…"
                : branches.length
                  ? undefined
                  : "The repository could not be listed; type the branch."
            }
          >
            {branches.length ? (
              <NativeSelect value={branch} onChange={(e) => setForm({ ...form, branch: e.target.value })}>
                {branches.map((b) => (
                  <option key={b} value={b}>
                    {b}
                  </option>
                ))}
              </NativeSelect>
            ) : (
              <Input
                value={form.branch}
                placeholder="main"
                onChange={(e) => setForm({ ...form, branch: e.target.value })}
              />
            )}
          </Field>
          <Checkbox
            checked={form.managed_state ?? true}
            onChange={(v) => setForm({ ...form, managed_state: v })}
            label="managed state — Stackd stores and locks the state"
          />
          {create.error && (
            <Banner tone="failed" role="alert" title="The environment was not created">
              {create.error instanceof ApiError
                ? (create.error.detail ?? create.error.title)
                : String(create.error)}
            </Banner>
          )}
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" loading={create.isPending} disabled={!tier}>
              Add environment
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** `/s/:space/stacks/:stack` — resolve the name to the stack. */
export function StackRoute() {
  const { stack: name = "" } = useParams();
  const { current } = useSpaces();
  const list = useQuery({ queryKey: ["stacks"], queryFn: stacks.list });
  if (!list.data || !current) return <Skeleton className="h-64 w-full" />;
  const stack = list.data.find((s) => s.space_id === current.id && s.name === name);
  if (!stack)
    return (
      <EmptyState title="Stack not found">
        No stack <span className="font-data">{name}</span> in {current.name}.{" "}
        <Link to={`/s/${encodeURIComponent(current.name)}`} className="link">
          Go to the overview
        </Link>
      </EmptyState>
    );
  return <StackPage stackId={stack.id} />;
}
