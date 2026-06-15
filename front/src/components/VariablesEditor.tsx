import { useState } from "react";
import { type QueryKey, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil } from "lucide-react";
import { type NewVariable, type VariablePatch } from "@/api/resources";
import type { Variable, VariableKind } from "@/api/types";
import { Badge, Button, DeleteButton, Field, ItemTile, Select, TextInput } from "@/components/ui";

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
      {(data ?? []).map((v) =>
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
                <Badge>{v.kind}</Badge>
                {v.hcl && <Badge>hcl</Badge>}
                {v.sensitive && <Badge color="var(--color-mock)">sensitive</Badge>}
              </div>
              <div className="flex shrink-0 items-center gap-1">
                {update && (
                  <button
                    type="button"
                    aria-label={`Edit ${v.name}`}
                    className="ui-btn rounded-base px-1.5 py-1"
                    onClick={() => setEditingId(v.id)}
                    style={{ color: "var(--color-text-secondary)" }}
                  >
                    <Pencil size={14} strokeWidth={1.75} aria-hidden />
                  </button>
                )}
                <DeleteButton label={`Delete ${v.name}`} onClick={() => removeMut.mutate(v.id)} />
              </div>
            </div>
            <div className="font-data mt-2 truncate text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
              {v.sensitive ? "•••" : (v.value ?? "")}
            </div>
          </ItemTile>
        ),
      )}
      {data && data.length === 0 && (
        <span className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
          No variables yet.
        </span>
      )}

      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          addMut.mutate();
        }}
      >
        <Field label="Kind">
          <Select
            value={form.kind}
            onChange={(e) => setForm({ ...form, kind: e.target.value as VariableKind })}
          >
            <option value="terraform">terraform</option>
            <option value="environment">environment</option>
          </Select>
        </Field>
        <Field label="Name">
          <TextInput value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
        </Field>
        <Field label="Value">
          <TextInput
            type={form.sensitive ? "password" : "text"}
            value={form.value}
            onChange={(e) => setForm({ ...form, value: e.target.value })}
            required
          />
        </Field>
        <label className="flex items-center gap-2 pb-1.5 text-[13px]">
          <input
            type="checkbox"
            checked={form.sensitive}
            onChange={(e) => setForm({ ...form, sensitive: e.target.checked })}
          />
          sensitive
        </label>
        <Button type="submit" disabled={addMut.isPending}>
          Add variable
        </Button>
      </form>
      {(addMut.isError || removeMut.isError || updateMut.isError) && (
        <div className="font-data text-[12px]" style={{ color: "var(--color-state-failed)" }}>
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
        <Field label={sensitive ? "New value (write-only)" : "Value"}>
          <TextInput
            type={sensitive ? "password" : "text"}
            value={value}
            placeholder={variable.sensitive ? "leave blank to keep" : undefined}
            onChange={(e) => setValue(e.target.value)}
          />
        </Field>
        <label className="flex items-center gap-2 pb-1.5 text-[13px]">
          <input type="checkbox" checked={sensitive} onChange={(e) => setSensitive(e.target.checked)} />
          sensitive
        </label>
        <label className="flex items-center gap-2 pb-1.5 text-[13px]">
          <input type="checkbox" checked={hcl} onChange={(e) => setHcl(e.target.checked)} />
          hcl
        </label>
        <Button type="submit" variant="accent" disabled={pending}>
          Save
        </Button>
        <Button type="button" onClick={onCancel}>
          Cancel
        </Button>
      </form>
      {error && (
        <div className="font-data mt-2 text-[12px]" style={{ color: "var(--color-state-failed)" }}>
          {error}
        </div>
      )}
    </ItemTile>
  );
}
