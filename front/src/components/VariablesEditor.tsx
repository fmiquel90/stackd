import { useId, useState } from "react";
import { type QueryKey, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil } from "lucide-react";
import { type NewVariable, type VariablePatch } from "@/api/resources";
import type { Variable, VariableKind } from "@/api/types";
import { Badge, Button, Checkbox, DeleteButton, Field, ItemTile, TextInput } from "@/components/ui";

// The two kinds of variable (SPECS §3.3), named by what they become in the run. "environment" is
// the API value, but in the UI it would read as a Stackd environment (dev/prod) — which a variable
// set attaches to on the same page — so the UI always says "shell environment variable".
export const KIND_META: Record<
  VariableKind,
  { label: string; heading: string; help: string; reference: (name: string) => string }
> = {
  terraform: {
    label: "Terraform variable",
    heading: "Terraform variables",
    help: "Passed to OpenTofu as an input: your code reads it as var.<name>.",
    reference: (name) => `var.${name || "<name>"}`,
  },
  environment: {
    label: "Shell environment variable",
    heading: "Shell environment variables",
    help: "Exported in the run's shell, e.g. AWS_REGION for a provider, and available to hooks. Your code does not see it as var.<name>.",
    reference: (name) => `$${name || "<NAME>"}`,
  },
};

const KINDS: VariableKind[] = ["terraform", "environment"];

// Reusable view+add+edit+remove for a list of variables (stack-level, env-level or variable-set
// members). All carry the same layered-resolution semantics (SPECS §3.4); sensitive values stay
// masked (write-only) — editing one means typing a new value, the old one is never returned.
export function VariablesEditor({
  queryKey,
  list,
  add,
  update,
  remove,
}: {
  queryKey: QueryKey;
  list: () => Promise<Variable[]>;
  add: (body: NewVariable) => Promise<unknown>;
  update?: (varId: string, body: VariablePatch) => Promise<unknown>;
  remove: (varId: string) => Promise<unknown>;
}) {
  const qc = useQueryClient();
  const { data } = useQuery({ queryKey, queryFn: list });
  const [form, setForm] = useState<NewVariable>({ kind: "terraform", name: "", value: "", sensitive: false });
  const [editingId, setEditingId] = useState<string | null>(null);
  const uid = useId();
  const invalidate = () => qc.invalidateQueries({ queryKey });

  const addMut = useMutation({
    mutationFn: () => add(form),
    onSuccess: () => {
      invalidate();
      setForm((f) => ({ ...f, name: "", value: "", sensitive: false }));
    },
  });
  const updateMut = useMutation({
    mutationFn: (args: { varId: string; body: VariablePatch }) => update!(args.varId, args.body),
    onSuccess: () => {
      invalidate();
      setEditingId(null);
    },
  });
  const removeMut = useMutation({ mutationFn: (varId: string) => remove(varId), onSuccess: invalidate });

  return (
    <div className="flex flex-col gap-2">
      {KINDS.map((kind) => {
        const vars = (data ?? []).filter((v) => v.kind === kind);
        if (vars.length === 0) return null;
        return (
          <section key={kind} className="flex flex-col gap-2" aria-label={KIND_META[kind].heading}>
            <div>
              <h3 className="eyebrow text-muted">{KIND_META[kind].heading}</h3>
              <p className="text-[12px] text-muted">{KIND_META[kind].help}</p>
            </div>
            {vars.map((v) =>
              editingId === v.id ? (
                <EditRow
                  key={v.id}
                  variable={v}
                  pending={updateMut.isPending}
                  onCancel={() => setEditingId(null)}
                  onSave={(body) => updateMut.mutate({ varId: v.id, body })}
                />
              ) : (
                <ItemTile key={v.id}>
                  <div className="flex items-center justify-between gap-3">
                    <div className="flex min-w-0 flex-wrap items-center gap-2">
                      <span className="font-data text-[13px] font-medium">{v.name}</span>
                      <Badge>{KIND_META[v.kind].reference(v.name)}</Badge>
                      {v.hcl && <Badge>hcl</Badge>}
                      {v.sensitive && <Badge color="var(--color-mock)">sensitive</Badge>}
                    </div>
                    <div className="flex shrink-0 items-center gap-1">
                      {update && (
                        <Button onClick={() => setEditingId(v.id)}>
                          <Pencil size={13} strokeWidth={1.75} aria-hidden /> Edit
                        </Button>
                      )}
                      <DeleteButton label={`Delete ${v.name}`} onClick={() => removeMut.mutate(v.id)} />
                    </div>
                  </div>
                  <div className="font-data mt-2 truncate text-[12px] text-muted">
                    {v.sensitive ? "•••" : (v.value ?? "")}
                  </div>
                </ItemTile>
              ),
            )}
          </section>
        );
      })}
      {data && data.length === 0 && (
        <span className="font-data text-[12px] text-muted">No variables yet.</span>
      )}

      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          addMut.mutate();
        }}
      >
        <fieldset className="flex w-full flex-col gap-1">
          <legend className="eyebrow mb-1 text-muted">Type</legend>
          <div className="flex flex-wrap gap-4">
            {KINDS.map((kind) => (
              <label key={kind} className="flex items-center gap-2 text-ui">
                <input
                  type="radio"
                  name={`${uid}-kind`}
                  className="size-4 accent-ring"
                  checked={form.kind === kind}
                  onChange={() => setForm({ ...form, kind })}
                />
                {KIND_META[kind].label}
              </label>
            ))}
          </div>
          <span className="text-[12px] text-muted">{KIND_META[form.kind].help}</span>
        </fieldset>
        <Field label="Name">
          <TextInput
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            aria-describedby={`${uid}-reference`}
            required
          />
        </Field>
        <Field label="Value">
          <TextInput
            type={form.sensitive ? "password" : "text"}
            value={form.value}
            onChange={(e) => setForm({ ...form, value: e.target.value })}
            required
          />
        </Field>
        <Checkbox
          className="pb-1.5"
          checked={form.sensitive ?? false}
          onChange={(v) => setForm({ ...form, sensitive: v })}
          label="sensitive"
        />
        <Button type="submit" disabled={addMut.isPending}>
          Add {KIND_META[form.kind].label.toLowerCase()}
        </Button>
        <span id={`${uid}-reference`} className="font-data w-full text-[12px] text-muted">
          Used as <span className="text-primary">{KIND_META[form.kind].reference(form.name)}</span>
        </span>
      </form>
      {(addMut.isError || removeMut.isError || updateMut.isError) && (
        <div className="font-data text-[12px] text-st-failed">
          {((addMut.error ?? removeMut.error ?? updateMut.error) as Error).message}
        </div>
      )}
    </div>
  );
}

// Inline editor for one variable. `value` is left blank for sensitive vars (write-only) and is only
// submitted when non-empty, so toggling hcl/sensitive on a secret never clobbers its stored value.
function EditRow({
  variable,
  pending,
  onSave,
  onCancel,
}: {
  variable: Variable;
  pending: boolean;
  onSave: (body: VariablePatch) => void;
  onCancel: () => void;
}) {
  const wasSensitive = variable.sensitive;
  const [value, setValue] = useState(wasSensitive ? "" : (variable.value ?? ""));
  const [sensitive, setSensitive] = useState(wasSensitive);
  const [hcl, setHcl] = useState(variable.hcl);
  const [error, setError] = useState<string | null>(null);

  return (
    <ItemTile>
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          // Turning a sensitive var non-sensitive would expose its stored value in cleartext — the
          // editor can't read it (write-only), so a fresh value must be retyped (invariant #3).
          if (wasSensitive && !sensitive && value === "") {
            setError("Re-enter the value to make this variable non-sensitive.");
            return;
          }
          setError(null);
          const body: VariablePatch = { sensitive, hcl };
          // For a sensitive var the field is write-only: send the value only if a new one was typed
          // (blank = keep the stored secret). For a non-sensitive var the value is visible, so always
          // send it — that's what lets the user clear it to empty.
          if (!wasSensitive || value !== "") body.value = value;
          onSave(body);
        }}
      >
        <span className="font-data pb-1.5 text-[13px] font-medium">{variable.name}</span>
        <span className="pb-1.5">
          <Badge>{KIND_META[variable.kind].reference(variable.name)}</Badge>
        </span>
        <Field label={sensitive ? "New value (write-only)" : "Value"}>
          <TextInput
            type={sensitive ? "password" : "text"}
            value={value}
            placeholder={variable.sensitive ? "leave blank to keep" : undefined}
            onChange={(e) => setValue(e.target.value)}
          />
        </Field>
        <Checkbox className="pb-1.5" checked={sensitive} onChange={setSensitive} label="sensitive" />
        {/* hcl only shapes how a terraform variable is written to tfvars (SPECS §3.4). */}
        {(variable.kind === "terraform" || variable.hcl) && (
          <Checkbox className="pb-1.5" checked={hcl} onChange={setHcl} label="hcl" />
        )}
        <Button type="submit" variant="accent" disabled={pending}>
          Save
        </Button>
        <Button type="button" onClick={onCancel}>
          Cancel
        </Button>
      </form>
      {error && <div className="font-data mt-2 text-[12px] text-st-failed">{error}</div>}
    </ItemTile>
  );
}
