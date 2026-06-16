import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { graphApi, stacks, variableSets, type AttachmentTarget } from "@/api/resources";
import type { VariableSet } from "@/api/types";
import {
  Badge,
  Button,
  Card,
  Checkbox,
  DeleteButton,
  Field,
  ItemTile,
  PageTitle,
  Select,
  TextInput,
} from "@/components/ui";
import { VariablesEditor } from "@/components/VariablesEditor";

function CreateForm({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [autoAttach, setAutoAttach] = useState(false);
  const create = useMutation({
    mutationFn: () => variableSets.create({ name, auto_attach: autoAttach }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["variable-sets"] });
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
          <TextInput value={name} onChange={(e) => setName(e.target.value)} placeholder="common-aws" required />
        </Field>
        <Checkbox className="pb-1.5" checked={autoAttach} onChange={setAutoAttach} label="auto-attach" />
        <Button type="submit" variant="accent" disabled={create.isPending}>
          Create set
        </Button>
      </form>
    </Card>
  );
}

// Rename + auto-attach toggle (auto_attach makes the set apply to every stack/env of the space,
// SPECS §3.4 — strongest-but-weakest layer, no explicit attachment needed).
function SetSettings({ set }: { set: VariableSet }) {
  const qc = useQueryClient();
  const [name, setName] = useState(set.name);
  const [autoAttach, setAutoAttach] = useState(set.auto_attach);
  const save = useMutation({
    mutationFn: () => variableSets.update(set.id, { name, auto_attach: autoAttach }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["variable-sets"] }),
  });
  const dirty = name !== set.name || autoAttach !== set.auto_attach;
  return (
    <form
      className="flex flex-wrap items-end gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate();
      }}
    >
      <Field label="Name">
        <TextInput value={name} onChange={(e) => setName(e.target.value)} required />
      </Field>
      <Checkbox className="pb-1.5" checked={autoAttach} onChange={setAutoAttach} label="auto-attach" />
      <Button type="submit" variant="accent" disabled={!dirty || save.isPending}>
        Save
      </Button>
      {save.isError && (
        <span className="font-data text-[12px]" style={{ color: "var(--color-state-failed)" }}>
          {(save.error as Error).message}
        </span>
      )}
    </form>
  );
}

// Attach the set to a specific stack or environment (or both). Listing resolves target names via
// the stacks list (stack targets) and the dependency graph nodes (env targets).
function AttachmentsPanel({ setId }: { setId: string }) {
  const qc = useQueryClient();
  const key = ["attachments", setId];
  const { data } = useQuery({ queryKey: key, queryFn: () => variableSets.attachments(setId) });
  const stackList = useQuery({ queryKey: ["stacks"], queryFn: stacks.list });
  const graph = useQuery({ queryKey: ["graph"], queryFn: graphApi.get });

  const [kind, setKind] = useState<AttachmentTarget>("environment");
  const [targetId, setTargetId] = useState("");

  const stackName = (sid: string) => stackList.data?.find((s) => s.id === sid)?.name ?? sid.slice(0, 6);
  const envOptions = (graph.data?.nodes ?? []).map((n) => ({ id: n.id, label: `${stackName(n.stack_id)}/${n.name}` }));
  const stackOptions = (stackList.data ?? []).map((s) => ({ id: s.id, label: s.name }));
  const options = kind === "stack" ? stackOptions : envOptions;
  const labelFor = (targetKind: AttachmentTarget, id: string) =>
    targetKind === "stack" ? stackName(id) : (envOptions.find((o) => o.id === id)?.label ?? id.slice(0, 8));

  const attach = useMutation({
    mutationFn: () => variableSets.attach(setId, { target_kind: kind, target_id: targetId }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: key });
      setTargetId("");
    },
  });
  const detach = useMutation({
    mutationFn: (aid: string) => variableSets.detach(setId, aid),
    onSuccess: () => qc.invalidateQueries({ queryKey: key }),
  });

  return (
    <div className="flex flex-col gap-2">
      {(data ?? []).map((a) => (
        <ItemTile key={a.id}>
          <div className="flex items-center justify-between gap-3">
            <div className="flex min-w-0 items-center gap-2">
              <Badge>{a.target_kind}</Badge>
              <span className="font-data text-[13px] font-medium">{labelFor(a.target_kind, a.target_id)}</span>
            </div>
            <DeleteButton label="Detach" onClick={() => detach.mutate(a.id)} />
          </div>
        </ItemTile>
      ))}
      {data && data.length === 0 && (
        <span className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
          Not attached anywhere. Attach it to a stack or environment below (or enable auto-attach).
        </span>
      )}
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          attach.mutate();
        }}
      >
        <Field label="Attach to">
          <Select
            value={kind}
            onChange={(e) => {
              setKind(e.target.value as AttachmentTarget);
              setTargetId("");
            }}
          >
            <option value="environment">environment</option>
            <option value="stack">stack</option>
          </Select>
        </Field>
        <Field label={kind === "stack" ? "Stack" : "Environment"}>
          <Select value={targetId} onChange={(e) => setTargetId(e.target.value)} required>
            <option value="">select…</option>
            {options.map((o) => (
              <option key={o.id} value={o.id}>
                {o.label}
              </option>
            ))}
          </Select>
        </Field>
        <Button type="submit" disabled={attach.isPending || !targetId}>
          Attach
        </Button>
        {attach.isError && (
          <span className="font-data text-[12px]" style={{ color: "var(--color-state-failed)" }}>
            {(attach.error as Error).message}
          </span>
        )}
      </form>
    </div>
  );
}

function SetCard({ set }: { set: VariableSet }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const remove = useMutation({
    mutationFn: () => variableSets.remove(set.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["variable-sets"] }),
  });

  return (
    <Card>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            className="ui-btn cursor-pointer text-[14px] font-medium hover:underline"
            style={{ background: "transparent" }}
          >
            {set.name}
          </button>
          {set.auto_attach && (
            <span className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
              auto-attach
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Button onClick={() => setOpen((v) => !v)}>{open ? "Hide" : "Configure"}</Button>
          <Button onClick={() => remove.mutate()} disabled={remove.isPending}>
            Delete
          </Button>
        </div>
      </div>
      {/* A set attached anywhere returns 409 — surface the "detach first" reason inline. */}
      {remove.isError && (
        <div className="mt-2 font-data text-[12px]" style={{ color: "var(--color-state-failed)" }}>
          {(remove.error as Error).message}
        </div>
      )}
      {open && (
        <div className="mt-3 flex flex-col gap-4">
          <div>
            <div className="mb-1 text-[13px] font-medium">Settings</div>
            <SetSettings set={set} />
          </div>
          <div>
            <div className="mb-1 text-[13px] font-medium">Attachments</div>
            <AttachmentsPanel setId={set.id} />
          </div>
          <div>
            <div className="mb-1 text-[13px] font-medium">Variables</div>
            <VariablesEditor
              queryKey={["variable-set-vars", set.id]}
              list={() => variableSets.variables(set.id)}
              add={(body) => variableSets.addVariable(set.id, body)}
              update={(varId, body) => variableSets.updateVariable(set.id, varId, body)}
              remove={(varId) => variableSets.removeVariable(set.id, varId)}
            />
          </div>
        </div>
      )}
    </Card>
  );
}

export function VariableSetsPage() {
  const [creating, setCreating] = useState(false);
  const { data: list } = useQuery({ queryKey: ["variable-sets"], queryFn: variableSets.list });

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <PageTitle>Variable Sets</PageTitle>
        {!creating && (
          <Button variant="accent" onClick={() => setCreating(true)}>
            New set
          </Button>
        )}
      </div>
      {creating && <CreateForm onDone={() => setCreating(false)} />}
      <div className="flex flex-col gap-2">
        {(list ?? []).map((s) => (
          <SetCard key={s.id} set={s} />
        ))}
        {list && list.length === 0 && (
          <p className="text-[13px]" style={{ color: "var(--color-text-secondary)" }}>
            No variable sets yet.
          </p>
        )}
      </div>
    </div>
  );
}
