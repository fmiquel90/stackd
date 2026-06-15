import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ChevronDown, ChevronRight } from "lucide-react";
import { environments, runs, stacks, tiers, type NewEnvironment } from "@/api/resources";
import type { Environment } from "@/api/types";
import { StateBadge } from "@/components/StateBadge";
import { ProvenanceBadge, parseProvenance } from "@/components/ProvenanceBadge";
import { CloudPanel } from "@/components/CloudPanel";
import { DependenciesPanel } from "@/components/DependenciesPanel";
import { CommandPanel } from "@/components/CommandPanel";
import { HooksPanel } from "@/components/HooksPanel";
import { NotificationsPanel } from "@/components/NotificationsPanel";
import { PromotePanel } from "@/components/PromotePanel";
import { SecretSourcesPanel } from "@/components/SecretSourcesPanel";
import { StackGeneralPanel } from "@/components/StackGeneralPanel";
import { StatePanel } from "@/components/StatePanel";
import { VariablesEditor } from "@/components/VariablesEditor";
import { Button, Card, Field, PageTitle, Select, Tabs, TextInput } from "@/components/ui";

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
  const catalog = useQuery({ queryKey: ["tiers"], queryFn: tiers.list });
  const tierNames = (catalog.data ?? []).map((t) => t.name);
  const [form, setForm] = useState<NewEnvironment>({ name: "", tier: "", branch: "main" });
  // Default the tier to the first catalog entry once it loads.
  useEffect(() => {
    if (!form.tier && tierNames.length > 0) setForm((f) => ({ ...f, tier: tierNames[0] }));
  }, [tierNames, form.tier]);
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
          <Select value={form.tier} onChange={(e) => setForm({ ...form, tier: e.target.value })} required>
            {tierNames.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Branch">
          <TextInput value={form.branch} onChange={(e) => setForm({ ...form, branch: e.target.value })} />
        </Field>
        <Button type="submit" variant="accent" disabled={create.isPending || !form.tier}>
          Add environment
        </Button>
      </form>
      {create.isError && (
        <div className="mt-2 font-data text-[12px]" style={{ color: "var(--color-state-failed)" }}>
          {(create.error as Error).message}
        </div>
      )}
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

type EnvTab = "inputs" | "deps" | "hooks" | "notify" | "cloud" | "state" | "command" | "promote";

const ENV_TABS: { key: EnvTab; label: string }[] = [
  { key: "inputs", label: "Inputs" },
  { key: "deps", label: "Dependencies" },
  { key: "hooks", label: "Hooks" },
  { key: "notify", label: "Notifications" },
  { key: "cloud", label: "Cloud" },
  { key: "state", label: "State" },
  { key: "command", label: "Command" },
  { key: "promote", label: "Promote" },
];

// One environment row. The daily action (Plan) is the sole accent CTA; the eight config sections are
// folded behind a single "Configure" disclosure that reveals a tab bar (progressive disclosure —
// only one panel shown at a time) instead of cramming nine buttons on the row.
function EnvCard({ env, siblings }: { env: Environment; siblings: { id: string; name: string }[] }) {
  const [expanded, setExpanded] = useState(false);
  const [tab, setTab] = useState<EnvTab>("inputs");
  const Chevron = expanded ? ChevronDown : ChevronRight;

  return (
    <Card>
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            aria-expanded={expanded}
            className="ui-btn cursor-pointer text-[14px] font-medium hover:underline"
            style={{ background: "transparent" }}
          >
            {env.name}
          </button>
          <span className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
            tier={env.tier} · branch={env.branch}
            {env.protected ? " · protected" : ""}
          </span>
          <LatestRunBadge envId={env.id} />
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <PlanButton envId={env.id} />
          <Button onClick={() => setExpanded((v) => !v)} aria-expanded={expanded}>
            <span className="inline-flex items-center gap-1.5">
              <Chevron size={14} strokeWidth={1.75} aria-hidden />
              Configure
            </span>
          </Button>
        </div>
      </div>
      {expanded && (
        <div className="mt-3 flex flex-col gap-3">
          <Tabs tabs={ENV_TABS} active={tab} onChange={setTab} />
          {tab === "inputs" && <ResolvedVariables envId={env.id} />}
          {tab === "deps" && <DependenciesPanel envId={env.id} />}
          {tab === "hooks" && <HooksPanel scope="environments" id={env.id} />}
          {tab === "notify" && <NotificationsPanel scope="environments" id={env.id} />}
          {tab === "cloud" && <CloudPanel envId={env.id} />}
          {tab === "state" && <StatePanel envId={env.id} />}
          {tab === "command" && <CommandPanel envId={env.id} />}
          {tab === "promote" && <PromotePanel envId={env.id} siblings={siblings} />}
        </div>
      )}
    </Card>
  );
}

type TopTab = "environments" | "settings";
type SettingTab = "general" | "variables" | "hooks" | "notifications" | "secrets";

const SETTING_TABS: { key: SettingTab; label: string }[] = [
  { key: "general", label: "General" },
  { key: "variables", label: "Variables" },
  { key: "hooks", label: "Hooks" },
  { key: "notifications", label: "Notifications" },
  { key: "secrets", label: "Secret sources" },
];

function EnvironmentsTab({ stackId }: { stackId: string }) {
  const [creating, setCreating] = useState(false);
  const envs = useQuery({ queryKey: ["environments", stackId], queryFn: () => stacks.environments(stackId) });

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <h2 className="text-[15px] font-semibold">Environments</h2>
        {!creating && <Button onClick={() => setCreating(true)}>Add environment</Button>}
      </div>
      {creating && <CreateEnvForm stackId={stackId} onDone={() => setCreating(false)} />}

      <div className="flex flex-col gap-2">
        {(envs.data ?? []).map((env) => (
          <EnvCard
            key={env.id}
            env={env}
            siblings={(envs.data ?? [])
              .filter((e) => e.id !== env.id)
              .map((e) => ({ id: e.id, name: e.name }))}
          />
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

function SettingsTab({ stackId, spaceId }: { stackId: string; spaceId: string }) {
  const [tab, setTab] = useState<SettingTab>("general");
  return (
    <div className="flex flex-col gap-3">
      <Tabs tabs={SETTING_TABS} active={tab} onChange={setTab} />
      {tab === "general" && <StackGeneralPanel stackId={stackId} />}
      {tab === "variables" && (
        <Card>
          <div className="mb-2 text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
            Common to every environment of this stack (an env-level variable of the same name
            overrides it — see resolution order in each env's Inputs tab).
          </div>
          <VariablesEditor
            queryKey={["stack-vars", stackId]}
            list={() => stacks.variables(stackId)}
            add={(body) => stacks.addVariable(stackId, body)}
            remove={(varId) => stacks.removeVariable(stackId, varId)}
          />
        </Card>
      )}
      {tab === "hooks" && <HooksPanel scope="stacks" id={stackId} />}
      {tab === "notifications" && <NotificationsPanel scope="stacks" id={stackId} />}
      {tab === "secrets" && <SecretSourcesPanel spaceId={spaceId} />}
    </div>
  );
}

export function StackDetailPage() {
  const { stackId = "" } = useParams();
  const [tab, setTab] = useState<TopTab>("environments");
  const stack = useQuery({ queryKey: ["stack", stackId], queryFn: () => stacks.get(stackId) });

  if (!stack.data) return <p className="font-data text-[12px]">Loading…</p>;

  return (
    <div className="flex flex-col gap-4">
      <div>
        <PageTitle>{stack.data.name}</PageTitle>
        <p className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
          {stack.data.repo_url} · {stack.data.tool} {stack.data.tool_version}
        </p>
      </div>

      <Tabs
        tabs={[
          { key: "environments", label: "Environments" },
          { key: "settings", label: "Settings" },
        ]}
        active={tab}
        onChange={setTab}
      />

      {tab === "environments" ? (
        <EnvironmentsTab stackId={stackId} />
      ) : (
        <SettingsTab stackId={stackId} spaceId={stack.data.space_id} />
      )}
    </div>
  );
}
