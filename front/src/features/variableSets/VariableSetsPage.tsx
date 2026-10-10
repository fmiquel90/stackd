import { useState } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { stacks, tiers, variableSets, type Attachment, type AttachmentTarget } from "@/api/resources";
import type { Environment, Stack, TierDef, VariableSet } from "@/api/types";
import { useSpaces } from "@/app/SpaceContext";
import { LabelsEditor } from "@/components/ui";
import { VariablesEditor } from "@/components/VariablesEditor";
import { Button } from "@/ui/Button";
import { ConfirmPopover } from "@/ui/ConfirmPopover";
import { Dialog, DialogContent, DialogTrigger } from "@/ui/overlays";
import {
  Badge,
  Banner,
  Checkbox,
  EmptyState,
  Field,
  Input,
  NativeSelect,
  Skeleton,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/ui/primitives";
import { TypeNameDialog } from "@/ui/TypeNameDialog";

// Variable sets (DESIGN §5.10): one row per set with how it reaches environments; each set has its
// own page with Variables / Attachments / Settings tabs (in the URL). Deleting asks for the name.

function ruleOf(set: VariableSet): string {
  return Object.entries(set.selector ?? {})
    .map(([k, v]) => `${k}=${String(v)}`)
    .join(", ");
}

/** How the set reaches environments, in badges — never colour alone. */
function Reach({ set, attachments }: { set: VariableSet; attachments: number | null }) {
  const rule = ruleOf(set);
  return (
    <span className="flex flex-wrap items-center gap-1.5">
      {set.auto_attach && <Badge>auto-attach · whole space</Badge>}
      {rule && <Badge className="normal-case tracking-normal">rule: {rule}</Badge>}
      {attachments !== null && (
        <span className="font-data text-[12px] text-muted">
          {attachments} attachment{attachments === 1 ? "" : "s"}
        </span>
      )}
      {!set.auto_attach && !rule && attachments === 0 && (
        <span className="font-data text-[12px] text-muted">· reaches no environment yet</span>
      )}
    </span>
  );
}

function useSpaceSets() {
  const { current } = useSpaces();
  const list = useQuery({ queryKey: ["variable-sets"], queryFn: variableSets.list });
  const sets = (list.data ?? [])
    .filter((s) => s.space_id === current?.id)
    .sort((a, b) => a.name.localeCompare(b.name));
  return { sets, loading: list.isLoading, error: list.error as Error | null };
}

function NewSetDialog() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const { current, spacePath } = useSpaces();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const create = useMutation({
    mutationFn: () =>
      variableSets.create({ name, description: description || undefined, space_id: current?.id }),
    onSuccess: (s) => {
      qc.invalidateQueries({ queryKey: ["variable-sets"] });
      setOpen(false);
      navigate(spacePath(`/variable-sets/${encodeURIComponent(s.name)}`));
    },
  });
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="primary">New variable set</Button>
      </DialogTrigger>
      <DialogContent title={`New variable set in ${current?.name ?? "this space"}`}>
        <form
          className="flex flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            create.mutate();
          }}
        >
          <Field label="Name">
            <Input
              value={name}
              onChange={(e) => setName(e.target.value.trim())}
              placeholder="common-aws"
              spellCheck={false}
              required
              autoFocus
            />
          </Field>
          <Field label="Description (optional)">
            <Input
              className="font-sans"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </Field>
          <p className="text-[12px] text-muted">
            Add variables, then attach the set to stacks, environments or tiers — or turn on auto-attach.
          </p>
          {create.isError && (
            <Banner tone="failed" role="alert" title="The API refused">
              {(create.error as Error).message}
            </Banner>
          )}
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" disabled={!name} loading={create.isPending}>
              Create set
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function VariableSetsPage() {
  const { spacePath, current } = useSpaces();
  const { sets, loading, error } = useSpaceSets();
  const counts = useQueries({
    queries: sets.map((s) => ({
      queryKey: ["attachments", s.id],
      queryFn: () => variableSets.attachments(s.id),
    })),
  });
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-title font-semibold">Variable sets</h1>
        <NewSetDialog />
      </div>
      <p className="max-w-[720px] text-[12px] text-muted">
        Shared variables for {current?.name ?? "this space"}. A set reaches an environment through an
        attachment (stack, environment or tier), a label rule, or auto-attach; the environment&apos;s own
        values still win (SPECS §3.4).
      </p>
      {error && (
        <Banner tone="failed" role="alert" title="The variable sets could not be loaded.">
          {error.message}
        </Banner>
      )}
      {loading ? (
        <Skeleton className="h-32 w-full max-w-[960px]" />
      ) : sets.length === 0 ? (
        <EmptyState title="No variable set in this space yet." />
      ) : (
        <ul className="flex max-w-[960px] flex-col gap-2">
          {sets.map((s, i) => (
            <li
              key={s.id}
              className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-base border border-line bg-surface px-3 py-2"
            >
              <Link
                to={spacePath(`/variable-sets/${encodeURIComponent(s.name)}`)}
                className="font-data font-semibold underline-offset-2 hover:underline"
              >
                {s.name}
              </Link>
              {s.description && <span className="text-[12px] text-muted">{s.description}</span>}
              <span className="ml-auto">
                <Reach set={s} attachments={counts[i]?.data?.length ?? null} />
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/* ---------- One set ---------- */

interface Names {
  stacks: Stack[];
  envs: Environment[];
  tiers: TierDef[];
}

function useNames(): Names {
  const stackList = useQuery({ queryKey: ["stacks"], queryFn: stacks.list });
  const tierList = useQuery({ queryKey: ["tiers"], queryFn: tiers.list });
  const envQueries = useQueries({
    queries: (stackList.data ?? []).map((s) => ({
      queryKey: ["environments", s.id],
      queryFn: () => stacks.environments(s.id),
    })),
  });
  return {
    stacks: stackList.data ?? [],
    envs: envQueries.flatMap((q) => q.data ?? []),
    tiers: tierList.data ?? [],
  };
}

function targetLabel(a: Pick<Attachment, "target_kind" | "target_id">, n: Names): string {
  if (a.target_kind === "stack")
    return n.stacks.find((s) => s.id === a.target_id)?.name ?? a.target_id.slice(0, 8);
  if (a.target_kind === "tier")
    return n.tiers.find((t) => t.id === a.target_id)?.name ?? a.target_id.slice(0, 8);
  const env = n.envs.find((e) => e.id === a.target_id);
  const stack = env && n.stacks.find((s) => s.id === env.stack_id);
  return env && stack ? `${stack.name}/${env.name}` : a.target_id.slice(0, 8);
}

function Attachments({ set }: { set: VariableSet }) {
  const qc = useQueryClient();
  const names = useNames();
  const key = ["attachments", set.id];
  const { data, isLoading } = useQuery({ queryKey: key, queryFn: () => variableSets.attachments(set.id) });
  const [kind, setKind] = useState<AttachmentTarget>("environment");
  const [targetId, setTargetId] = useState("");
  const attach = useMutation({
    mutationFn: () => variableSets.attach(set.id, { target_kind: kind, target_id: targetId }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: key });
      setTargetId("");
    },
  });
  const detach = useMutation({
    mutationFn: (id: string) => variableSets.detach(set.id, id),
    onSuccess: () => qc.invalidateQueries({ queryKey: key }),
  });
  const spaceStacks = names.stacks
    .filter((s) => s.space_id === set.space_id)
    .sort((a, b) => a.name.localeCompare(b.name));

  return (
    <div className="flex max-w-[720px] flex-col gap-3">
      {isLoading ? (
        <Skeleton className="h-20 w-full" />
      ) : !data?.length ? (
        <p className="text-muted">
          Not attached anywhere{set.auto_attach ? " — auto-attach already reaches every environment" : ""}.
        </p>
      ) : (
        <ul className="flex flex-col gap-2">
          {data.map((a) => {
            const label = targetLabel(a, names);
            return (
              <li
                key={a.id}
                className="flex items-center gap-3 rounded-base border border-line bg-surface px-3 py-2"
              >
                <Badge>{a.target_kind}</Badge>
                <span className="font-data">{label}</span>
                <span className="ml-auto">
                  <ConfirmPopover
                    trigger={
                      <Button size="sm" variant="ghost">
                        Detach…
                      </Button>
                    }
                    question={`${set.name} stops applying to ${label} at its next run.`}
                    confirmLabel={`Detach from ${label}`}
                    onConfirm={() => detach.mutate(a.id)}
                  />
                </span>
              </li>
            );
          })}
        </ul>
      )}
      {detach.isError && (
        <Banner tone="failed" role="alert" title="The API refused">
          {(detach.error as Error).message}
        </Banner>
      )}
      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          attach.mutate();
        }}
      >
        <Field label="Attach to">
          <NativeSelect
            value={kind}
            onChange={(e) => {
              setKind(e.target.value as AttachmentTarget);
              setTargetId("");
            }}
          >
            <option value="environment">environment</option>
            <option value="stack">stack</option>
            <option value="tier">tier</option>
          </NativeSelect>
        </Field>
        <Field label={kind === "environment" ? "Environment" : kind === "stack" ? "Stack" : "Tier"}>
          <NativeSelect value={targetId} onChange={(e) => setTargetId(e.target.value)} required>
            <option value="">Pick…</option>
            {kind === "stack" &&
              spaceStacks.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            {kind === "tier" &&
              names.tiers.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            {kind === "environment" &&
              spaceStacks.map((s) => (
                <optgroup key={s.id} label={s.name}>
                  {names.envs
                    .filter((e) => e.stack_id === s.id)
                    .map((e) => (
                      <option key={e.id} value={e.id}>
                        {e.name}
                      </option>
                    ))}
                </optgroup>
              ))}
          </NativeSelect>
        </Field>
        <Button type="submit" disabled={!targetId} loading={attach.isPending}>
          Attach
        </Button>
      </form>
      {attach.isError && (
        <Banner tone="failed" role="alert" title="The API refused">
          {(attach.error as Error).message}
        </Banner>
      )}
    </div>
  );
}

function SetSettings({
  set,
  onRenamed,
  onDeleted,
}: {
  set: VariableSet;
  onRenamed: (n: string) => void;
  onDeleted: () => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState(set.name);
  const [description, setDescription] = useState(set.description ?? "");
  const [autoAttach, setAutoAttach] = useState(set.auto_attach);
  const [selector, setSelector] = useState<Record<string, string>>(
    Object.fromEntries(Object.entries(set.selector ?? {}).map(([k, v]) => [k, String(v)])),
  );
  const save = useMutation({
    mutationFn: () =>
      variableSets.update(set.id, {
        name,
        description: description || null,
        auto_attach: autoAttach,
        selector: Object.keys(selector).length ? selector : null,
      }),
    onSuccess: (s) => {
      qc.invalidateQueries({ queryKey: ["variable-sets"] });
      if (s.name !== set.name) onRenamed(s.name);
    },
  });
  return (
    <div className="flex max-w-[560px] flex-col gap-6">
      <form
        className="flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <Field label="Name">
          <Input value={name} onChange={(e) => setName(e.target.value.trim())} spellCheck={false} required />
        </Field>
        <Field label="Description">
          <Input className="font-sans" value={description} onChange={(e) => setDescription(e.target.value)} />
        </Field>
        <label className="flex items-center gap-2 text-ui">
          <Checkbox checked={autoAttach} onChange={(e) => setAutoAttach(e.target.checked)} />
          Auto-attach to every stack and environment of the space
        </label>
        <div className="flex flex-col gap-1">
          <span className="eyebrow text-muted">Label rule</span>
          <p className="text-[12px] text-muted">
            Attach automatically to any stack or environment whose labels match all of these.
          </p>
          <LabelsEditor
            value={selector}
            onChange={setSelector}
            keyPlaceholder="team"
            valuePlaceholder="payments"
          />
        </div>
        {save.isError && (
          <Banner tone="failed" role="alert" title="The API refused">
            {(save.error as Error).message}
          </Banner>
        )}
        {save.isSuccess && !save.isPending && (
          <p role="status" className="text-[12px] text-muted">
            Saved.
          </p>
        )}
        <div>
          <Button type="submit" variant="primary" loading={save.isPending}>
            Save settings
          </Button>
        </div>
      </form>
      <section
        aria-labelledby="set-danger"
        className="flex flex-col gap-2 rounded-base border border-st-failed p-3"
      >
        <h2 id="set-danger" className="text-section font-semibold">
          Danger zone
        </h2>
        <p className="text-[12px] text-muted">
          The API refuses while the set is attached anywhere: detach it first.
        </p>
        <div>
          <TypeNameDialog
            trigger={<Button variant="danger">Delete variable set {set.name}</Button>}
            title={`Delete ${set.name}`}
            consequence="Its variables stop applying to every environment it reaches, from their next run."
            name={set.name}
            confirmLabel={`Delete ${set.name}`}
            onConfirm={() =>
              variableSets.remove(set.id).then(() => {
                qc.invalidateQueries({ queryKey: ["variable-sets"] });
                onDeleted();
              })
            }
          />
        </div>
      </section>
    </div>
  );
}

export function VariableSetPage() {
  const { set: setName } = useParams();
  const navigate = useNavigate();
  const { spacePath } = useSpaces();
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") ?? "variables";
  const { sets, loading } = useSpaceSets();
  const set = sets.find((s) => s.name === setName);
  const attachments = useQuery({
    queryKey: ["attachments", set?.id],
    queryFn: () => variableSets.attachments(set!.id),
    enabled: !!set,
  });

  if (loading) return <Skeleton className="h-40 w-full" />;
  if (!set)
    return (
      <EmptyState
        title={`No variable set named “${setName}” in this space.`}
        action={
          <Link to={spacePath("/variable-sets")} className="link">
            All variable sets
          </Link>
        }
      />
    );
  return (
    <div className="flex flex-col gap-4">
      <header className="flex flex-col gap-1">
        <h1 className="font-data text-title font-semibold">{set.name}</h1>
        {set.description && <p className="text-muted">{set.description}</p>}
        <Reach set={set} attachments={attachments.data?.length ?? null} />
      </header>
      <Tabs
        value={tab}
        onValueChange={(v) => {
          const next = new URLSearchParams(params);
          if (v === "variables") next.delete("tab");
          else next.set("tab", v);
          setParams(next, { replace: true });
        }}
      >
        <TabsList>
          <TabsTrigger value="variables">Variables</TabsTrigger>
          <TabsTrigger value="attachments">Attachments</TabsTrigger>
          <TabsTrigger value="settings">Settings</TabsTrigger>
        </TabsList>
        <TabsContent value="variables" className="max-w-[960px] pt-3">
          <VariablesEditor
            queryKey={["variable-set-vars", set.id]}
            list={() => variableSets.variables(set.id)}
            add={(body) => variableSets.addVariable(set.id, body)}
            update={(varId, body) => variableSets.updateVariable(set.id, varId, body)}
            remove={(varId) => variableSets.removeVariable(set.id, varId)}
          />
        </TabsContent>
        <TabsContent value="attachments" className="pt-3">
          <Attachments set={set} />
        </TabsContent>
        <TabsContent value="settings" className="pt-3">
          <SetSettings
            key={set.id}
            set={set}
            onRenamed={(n) =>
              navigate(spacePath(`/variable-sets/${encodeURIComponent(n)}?tab=settings`), { replace: true })
            }
            onDeleted={() => navigate(spacePath("/variable-sets"))}
          />
        </TabsContent>
      </Tabs>
    </div>
  );
}
