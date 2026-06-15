import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { X } from "lucide-react";
import { dependenciesApi, graphApi, stacks } from "@/api/resources";
import { Button, Card, Field, Select, TextInput } from "@/components/ui";

export function DependenciesPanel({ envId }: { envId: string }) {
  const qc = useQueryClient();
  const key = ["dependencies", envId];
  const { data: deps } = useQuery({ queryKey: key, queryFn: () => dependenciesApi.list(envId) });
  const graph = useQuery({ queryKey: ["graph"], queryFn: graphApi.get });
  const stackList = useQuery({ queryKey: ["stacks"], queryFn: stacks.list });

  const stackName = (sid: string) => stackList.data?.find((s) => s.id === sid)?.name ?? sid.slice(0, 6);
  const envOptions = (graph.data?.nodes ?? [])
    .filter((n) => n.id !== envId)
    .map((n) => ({ id: n.id, label: `${stackName(n.stack_id)}/${n.name}` }));
  const labelFor = (id: string) => envOptions.find((o) => o.id === id)?.label ?? id.slice(0, 8);

  const [form, setForm] = useState({
    upstream_env_id: "",
    output_name: "",
    input_name: "",
    mock_value: "",
    trigger_policy: "on_output_change",
  });
  const create = useMutation({
    mutationFn: () =>
      dependenciesApi.create(envId, {
        upstream_env_id: form.upstream_env_id,
        trigger_policy: form.trigger_policy,
        references: [
          {
            output_name: form.output_name,
            input_name: form.input_name,
            mock_value: form.mock_value || null,
          },
        ],
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: key });
      qc.invalidateQueries({ queryKey: ["graph"] });
      setForm({ ...form, output_name: "", input_name: "", mock_value: "" });
    },
  });
  const remove = useMutation({
    mutationFn: (depId: string) => dependenciesApi.remove(depId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: key });
      qc.invalidateQueries({ queryKey: ["graph"] });
    },
  });

  return (
    <Card>
      <div className="mb-2 text-[13px] font-medium">Dependencies (upstream → this env)</div>
      <div className="flex flex-col gap-1">
        {(deps ?? []).map((d) => (
          <div key={d.id} className="font-data flex items-center gap-2 text-[12px]">
            <span style={{ color: "var(--color-accent)" }}>{labelFor(d.upstream_env_id)}</span>
            <span style={{ color: "var(--color-text-secondary)" }}>{d.trigger_policy}</span>
            <span style={{ color: "var(--color-text-secondary)" }}>
              {d.references.map((r) => `${r.output_name}→${r.input_name}${r.has_mock ? " (mock)" : ""}`).join(", ")}
            </span>
            <button type="button" aria-label="Delete dependency" onClick={() => remove.mutate(d.id)} style={{ color: "var(--color-text-secondary)" }}>
              <X size={13} strokeWidth={1.75} aria-hidden />
            </button>
          </div>
        ))}
        {deps && deps.length === 0 && (
          <span className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
            No dependencies.
          </span>
        )}
      </div>
      <form
        className="mt-3 flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <Field label="Upstream env">
          <Select
            value={form.upstream_env_id}
            onChange={(e) => setForm({ ...form, upstream_env_id: e.target.value })}
            required
          >
            <option value="">select…</option>
            {envOptions.map((o) => (
              <option key={o.id} value={o.id}>
                {o.label}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Output">
          <TextInput value={form.output_name} onChange={(e) => setForm({ ...form, output_name: e.target.value })} required />
        </Field>
        <Field label="→ Input (TF_VAR)">
          <TextInput value={form.input_name} onChange={(e) => setForm({ ...form, input_name: e.target.value })} required />
        </Field>
        <Field label="Mock (optional)">
          <TextInput value={form.mock_value} onChange={(e) => setForm({ ...form, mock_value: e.target.value })} />
        </Field>
        <Field label="Trigger">
          <Select value={form.trigger_policy} onChange={(e) => setForm({ ...form, trigger_policy: e.target.value })}>
            <option value="on_output_change">on_output_change</option>
            <option value="always">always</option>
            <option value="never">never</option>
          </Select>
        </Field>
        <Button type="submit" disabled={create.isPending || !form.upstream_env_id}>
          Add dependency
        </Button>
      </form>
    </Card>
  );
}
