import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ChevronDown, ChevronRight, RefreshCw } from "lucide-react";
import { environments, runs, stacks, tiers, type NewEnvironment } from "@/api/resources";
import type { Environment } from "@/api/types";
import { useIsAdmin } from "@/auth/session";
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
import { Button, Card, Checkbox, Field, PageTitle, Select, Tabs, TextInput } from "@/components/ui";

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

// Outputs this environment publishes after a successful apply (sensitive ones masked). Feeds the
// inputs of downstream environments via dependencies (SPECS §9.1).
function EnvOutputs({ envId }: { envId: string }) {
  const { data } = useQuery({ queryKey: ["env-outputs", envId], queryFn: () => environments.outputs(envId) });
  if (!data || data.length === 0)
    return (
      <span className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
        No outputs yet — they appear after a successful apply.
      </span>
    );
  return (
    <table className="w-full text-left">
      <tbody>
        {data.map((o) => (
          <tr key={o.name}>
            <td className="font-data py-1 pr-3 text-[12px]">{o.name}</td>
            <td className="font-data py-1 text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
              {o.sensitive ? "•••" : (o.value ?? "")}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// The Inputs tab: resolved values (read-only, with provenance), env-level overrides (editable), and
// the env's published outputs. Env-level variables override the stack-level value of the same name.
function EnvInputs({ envId }: { envId: string }) {
  return (
    <div className="flex flex-col gap-4">
      <div>
        <div className="mb-1 text-[13px] font-medium">Resolved</div>
        <ResolvedVariables envId={envId} />
      </div>
      <div>
        <div className="mb-1 text-[13px] font-medium">Environment overrides</div>
        <div className="mb-2 text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
          Specific to this environment — overrides the stack-level variable of the same name.
        </div>
        <VariablesEditor
          queryKey={["env-vars", envId]}
          list={() => environments.variables(envId)}
          add={(body) => environments.addVariable(envId, body)}
          update={(varId, body) => environments.updateVariable(envId, varId, body)}
          remove={(varId) => environments.removeVariable(envId, varId)}
        />
      </div>
      <div>
        <div className="mb-1 text-[13px] font-medium">Outputs</div>
        <EnvOutputs envId={envId} />
      </div>
    </div>
  );
}

function CreateEnvForm({ stackId, onDone }: { stackId: string; onDone: () => void }) {
  const qc = useQueryClient();
  const catalog = useQuery({ queryKey: ["tiers"], queryFn: tiers.list });
  const tierNames = (catalog.data ?? []).map((t) => t.name);
  const [form, setForm] = useState<NewEnvironment>({ name: "", tier: "", branch: "main", managed_state: true });
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
        <Checkbox
          className="pb-1.5"
          checked={form.managed_state ?? true}
          onChange={(v) => setForm({ ...form, managed_state: v })}
          label="managed state"
        />
        <Button type="submit" variant="accent" disabled={create.isPending || !form.tier}>
          Add environment
        </Button>
      </form>
      <div className="mt-2 text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
        Managed state: Terraform talks to the platform's HTTP backend (state stored & locked by Stackd,
        SPECS §11). Uncheck to keep your own backend configured in the repo.
      </div>
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

// Force the platform to re-read the tracked branch's remote HEAD (refreshes the stale/commits-ahead
// indicators without waiting for the periodic poll). The API enforces writer rights.
function RefreshHeadButton({ env }: { env: Environment }) {
  const qc = useQueryClient();
  const refresh = useMutation({
    mutationFn: () => environments.refreshHead(env.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["environments", env.stack_id] }),
  });
  return (
    <button
      type="button"
      aria-label="Refresh branch HEAD"
      title="Refresh branch HEAD from the remote"
      className="ui-btn rounded-base px-1.5 py-1 disabled:opacity-50"
      onClick={() => refresh.mutate()}
      disabled={refresh.isPending}
      style={{ border: "1px solid var(--color-border)", color: "var(--color-text-secondary)" }}
    >
      <RefreshCw size={14} strokeWidth={1.75} aria-hidden className={refresh.isPending ? "animate-spin" : undefined} />
    </button>
  );
}

type EnvTab =
  | "inputs"
  | "deps"
  | "hooks"
  | "notify"
  | "cloud"
  | "state"
  | "command"
  | "promote"
  | "danger";

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

// Deleting an environment cascades its runs and state — type-the-name confirmation (DESIGN §5.2).
function DeleteEnvPanel({ env }: { env: Environment }) {
  const qc = useQueryClient();
  const [typed, setTyped] = useState("");
  const remove = useMutation({
    mutationFn: () => environments.remove(env.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["environments", env.stack_id] }),
  });
  return (
    <Card>
      <div className="mb-1 text-[13px] font-medium" style={{ color: "var(--color-state-failed)" }}>
        Delete environment
      </div>
      <div className="mb-2 text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
        Removes this environment and all its runs and state history. This cannot be undone.
      </div>
      <div className="flex items-end gap-2">
        <Field label={`Type "${env.name}" to confirm`}>
          <TextInput value={typed} onChange={(e) => setTyped(e.target.value)} />
        </Field>
        <button
          type="button"
          className="ui-btn rounded-base px-3 py-1.5 text-[13px] font-medium disabled:opacity-50"
          style={{ border: "1px solid var(--color-state-failed)", color: "var(--color-state-failed)" }}
          disabled={typed !== env.name || remove.isPending}
          onClick={() => remove.mutate()}
        >
          Delete environment
        </button>
      </div>
      {remove.isError && (
        <div className="mt-2 font-data text-[12px]" style={{ color: "var(--color-state-failed)" }}>
          {(remove.error as Error).message}
        </div>
      )}
    </Card>
  );
}

// One environment row. The daily action (Plan) is the sole accent CTA; the eight config sections are
// folded behind a single "Configure" disclosure that reveals a tab bar (progressive disclosure —
// only one panel shown at a time) instead of cramming nine buttons on the row.
function EnvCard({ env, siblings }: { env: Environment; siblings: { id: string; name: string }[] }) {
  const isAdmin = useIsAdmin();
  const [expanded, setExpanded] = useState(false);
  const [tab, setTab] = useState<EnvTab>("inputs");
  const Chevron = expanded ? ChevronDown : ChevronRight;
  // Deleting an env is admin-only in the UI (the API allows writer); appended as a Danger tab.
  const tabs = isAdmin ? [...ENV_TABS, { key: "danger" as EnvTab, label: "Danger" }] : ENV_TABS;

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
          <RefreshHeadButton env={env} />
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
          <Tabs tabs={tabs} active={tab} onChange={setTab} />
          {tab === "inputs" && <EnvInputs envId={env.id} />}
          {tab === "deps" && <DependenciesPanel envId={env.id} />}
          {tab === "hooks" && <HooksPanel scope="environments" id={env.id} />}
          {tab === "notify" && <NotificationsPanel scope="environments" id={env.id} />}
          {tab === "cloud" && <CloudPanel envId={env.id} />}
          {tab === "state" && <StatePanel envId={env.id} />}
          {tab === "command" && <CommandPanel envId={env.id} />}
          {tab === "promote" && <PromotePanel envId={env.id} siblings={siblings} />}
          {tab === "danger" && isAdmin && <DeleteEnvPanel env={env} />}
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
            update={(varId, body) => stacks.updateVariable(stackId, varId, body)}
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
