import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { environments, runs, stacks, type NewEnvironment } from "@/api/resources";
import type { Tier } from "@/api/types";
import { StateBadge } from "@/components/StateBadge";
import { ProvenanceBadge, parseProvenance } from "@/components/ProvenanceBadge";
import { CloudPanel } from "@/components/CloudPanel";
import { DependenciesPanel } from "@/components/DependenciesPanel";
import { CommandPanel } from "@/components/CommandPanel";
import { HooksPanel } from "@/components/HooksPanel";
import { NotificationsPanel } from "@/components/NotificationsPanel";
import { StatePanel } from "@/components/StatePanel";
import { Button, Card, Field, PageTitle, Select, TextInput } from "@/components/ui";

function ResolvedVariables({ envId }: { envId: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ["resolved-variables", envId],
    queryFn: () => environments.resolvedVariables(envId),
  });
  if (isLoading) return <span className="font-data text-[12px]">Loading…</span>;
  if (!data || data.length === 0)
    return <span className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>No variables resolved.</span>;
  return (
    <table className="w-full text-left">
      <tbody>
        {data.map((v) => (
          <tr key={`${v.kind}:${v.name}`}>
            <td className="font-data py-1 pr-3 text-[12px]">{v.injected_name}</td>
            <td className="font-data py-1 pr-3 text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
              {v.value ?? "•••"}
            </td>
            <td className="py-1">
              <ProvenanceBadge provenance={parseProvenance(v.provenance)} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function CreateEnvForm({ stackId, onDone }: { stackId: string; onDone: () => void }) {
  const qc = useQueryClient();
  const [form, setForm] = useState<NewEnvironment>({ name: "", tier: "dev", branch: "main" });
  const create = useMutation({
    mutationFn: () => stacks.createEnvironment(stackId, form),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["environments", stackId] });
      onDone();
    },
  });
  return (
    <Card>
      <form
        className="flex items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <Field label="Name">
          <TextInput value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
        </Field>
        <Field label="Tier">
          <Select value={form.tier} onChange={(e) => setForm({ ...form, tier: e.target.value as Tier })}>
            <option value="dev">dev</option>
            <option value="staging">staging</option>
            <option value="prod">prod</option>
          </Select>
        </Field>
        <Field label="Branch">
          <TextInput value={form.branch} onChange={(e) => setForm({ ...form, branch: e.target.value })} />
        </Field>
        <Button type="submit" variant="accent" disabled={create.isPending}>
          Add environment
        </Button>
      </form>
    </Card>
  );
}

function LatestRunBadge({ envId }: { envId: string }) {
  const { data } = useQuery({
    queryKey: ["env-runs", envId],
    queryFn: () => runs.list(envId),
    refetchInterval: 4000,
  });
  const latest = data?.[0];
  if (!latest)
    return (
      <span className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
        no runs
      </span>
    );
  return (
    <Link to={`/runs/${latest.id}`}>
      <StateBadge state={latest.state} mocked={latest.used_mocks} />
    </Link>
  );
}

function PlanButton({ envId }: { envId: string }) {
  const navigate = useNavigate();
  const trigger = useMutation({
    mutationFn: () => runs.trigger(envId),
    onSuccess: (run) => navigate(`/runs/${run.id}`),
  });
  return (
    <Button variant="accent" disabled={trigger.isPending} onClick={() => trigger.mutate()}>
      Plan
    </Button>
  );
}

type EnvTab = "inputs" | "hooks" | "deps" | "state" | "cloud" | "notify" | "command";

function EnvTabButton({ active, label, onClick }: { active: boolean; label: string; onClick: () => void }) {
  return (
    <Button onClick={onClick}>
      <span style={{ color: active ? "var(--color-accent)" : undefined }}>{label}</span>
    </Button>
  );
}

export function StackDetailPage() {
  const { stackId = "" } = useParams();
  const [creating, setCreating] = useState(false);
  const [open, setOpen] = useState<{ envId: string; tab: EnvTab } | null>(null);
  const [showStackHooks, setShowStackHooks] = useState(false);
  const [showStackNotifs, setShowStackNotifs] = useState(false);
  const stack = useQuery({ queryKey: ["stack", stackId], queryFn: () => stacks.get(stackId) });
  const envs = useQuery({ queryKey: ["environments", stackId], queryFn: () => stacks.environments(stackId) });

  if (!stack.data) return <p className="font-data text-[12px]">Loading…</p>;

  const toggle = (envId: string, tab: EnvTab) =>
    setOpen(open?.envId === envId && open.tab === tab ? null : { envId, tab });

  return (
    <div className="flex flex-col gap-4">
      <div>
        <PageTitle>{stack.data.name}</PageTitle>
        <p className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
          {stack.data.repo_url} · {stack.data.tool} {stack.data.tool_version}
        </p>
      </div>

      <div className="flex items-center justify-between">
        <h2 className="text-[15px] font-semibold">Stack hooks</h2>
        <Button onClick={() => setShowStackHooks((v) => !v)}>{showStackHooks ? "Hide" : "Manage hooks"}</Button>
      </div>
      {showStackHooks && <HooksPanel scope="stacks" id={stackId} />}

      <div className="flex items-center justify-between">
        <h2 className="text-[15px] font-semibold">Stack notifications</h2>
        <Button onClick={() => setShowStackNotifs((v) => !v)}>{showStackNotifs ? "Hide" : "Manage notifications"}</Button>
      </div>
      {showStackNotifs && <NotificationsPanel scope="stacks" id={stackId} />}

      <div className="flex items-center justify-between">
        <h2 className="text-[15px] font-semibold">Environments</h2>
        {!creating && <Button onClick={() => setCreating(true)}>Add environment</Button>}
      </div>
      {creating && <CreateEnvForm stackId={stackId} onDone={() => setCreating(false)} />}

      <div className="flex flex-col gap-2">
        {(envs.data ?? []).map((env) => (
          <Card key={env.id}>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <span className="text-[14px] font-medium">{env.name}</span>
                <span className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
                  tier={env.tier} · branch={env.branch}
                  {env.protected ? " · protected" : ""}
                </span>
                <LatestRunBadge envId={env.id} />
              </div>
              <div className="flex items-center gap-2">
                <PlanButton envId={env.id} />
                <EnvTabButton active={open?.envId === env.id && open.tab === "inputs"} label="Inputs" onClick={() => toggle(env.id, "inputs")} />
                <EnvTabButton active={open?.envId === env.id && open.tab === "hooks"} label="Hooks" onClick={() => toggle(env.id, "hooks")} />
                <EnvTabButton active={open?.envId === env.id && open.tab === "deps"} label="Deps" onClick={() => toggle(env.id, "deps")} />
                <EnvTabButton active={open?.envId === env.id && open.tab === "state"} label="State" onClick={() => toggle(env.id, "state")} />
                <EnvTabButton active={open?.envId === env.id && open.tab === "cloud"} label="Cloud" onClick={() => toggle(env.id, "cloud")} />
                <EnvTabButton active={open?.envId === env.id && open.tab === "notify"} label="Notify" onClick={() => toggle(env.id, "notify")} />
                <EnvTabButton active={open?.envId === env.id && open.tab === "command"} label="Command" onClick={() => toggle(env.id, "command")} />
              </div>
            </div>
            {open?.envId === env.id && (
              <div className="mt-3">
                {open.tab === "inputs" && <ResolvedVariables envId={env.id} />}
                {open.tab === "hooks" && <HooksPanel scope="environments" id={env.id} />}
                {open.tab === "deps" && <DependenciesPanel envId={env.id} />}
                {open.tab === "state" && <StatePanel envId={env.id} />}
                {open.tab === "cloud" && <CloudPanel envId={env.id} />}
                {open.tab === "notify" && <NotificationsPanel scope="environments" id={env.id} />}
                {open.tab === "command" && <CommandPanel envId={env.id} />}
              </div>
            )}
          </Card>
        ))}
        {envs.data && envs.data.length === 0 && (
          <p className="text-[13px]" style={{ color: "var(--color-text-secondary)" }}>
            No environments yet.
          </p>
        )}
      </div>
    </div>
  );
}
