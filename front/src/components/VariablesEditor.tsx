import { useState } from "react";
import { type QueryKey, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type NewVariable } from "@/api/resources";
import type { Variable, VariableKind } from "@/api/types";
import { Badge, Button, DeleteButton, Field, ItemTile, Select, TextInput } from "@/components/ui";

// Reusable view+add+remove for a list of variables (stack-level or variable-set members). Both carry
// the same layered-resolution semantics (SPECS §3.4); sensitive values stay masked (write-only).
export function VariablesEditor({
  queryKey,
  list,
  add,
  remove,
}: {
  queryKey: QueryKey;
  list: () => Promise<Variable[]>;
  add: (body: NewVariable) => Promise<unknown>;
  remove: (varId: string) => Promise<unknown>;
}) {
  const qc = useQueryClient();
  const { data } = useQuery({ queryKey, queryFn: list });
  const [form, setForm] = useState<NewVariable>({ kind: "terraform", name: "", value: "", sensitive: false });
  const invalidate = () => qc.invalidateQueries({ queryKey });

  const addMut = useMutation({
    mutationFn: () => add(form),
    onSuccess: () => {
      invalidate();
      setForm((f) => ({ ...f, name: "", value: "", sensitive: false }));
    },
  });
  const removeMut = useMutation({ mutationFn: (varId: string) => remove(varId), onSuccess: invalidate });

  return (
    <div className="flex flex-col gap-2">
      {(data ?? []).map((v) => (
        <ItemTile key={v.id}>
          <div className="flex items-center justify-between gap-3">
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <span className="font-data text-[13px] font-medium">{v.name}</span>
              <Badge>{v.kind}</Badge>
              {v.hcl && <Badge>hcl</Badge>}
              {v.sensitive && <Badge color="var(--color-mock)">sensitive</Badge>}
            </div>
            <DeleteButton label={`Delete ${v.name}`} onClick={() => removeMut.mutate(v.id)} />
          </div>
          <div className="font-data mt-2 truncate text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
            {v.sensitive ? "•••" : (v.value ?? "")}
          </div>
        </ItemTile>
      ))}
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
      {(addMut.isError || removeMut.isError) && (
        <div className="font-data text-[12px]" style={{ color: "var(--color-state-failed)" }}>
          {((addMut.error ?? removeMut.error) as Error).message}
        </div>
      )}
    </div>
  );
}
