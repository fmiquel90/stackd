import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { hooksApi, type HookScope, type HookStage } from "@/api/resources";
import { Badge, Button, Card, DeleteButton, Field, ItemTile, Select, TextInput } from "@/components/ui";

const STAGES: HookStage[] = [
  "before_init",
  "after_init",
  "before_plan",
  "after_plan",
  "before_apply",
  "after_apply",
];

export function HooksPanel({ scope, id }: { scope: HookScope; id: string }) {
  const qc = useQueryClient();
  const key = ["hooks", scope, id];
  const { data } = useQuery({ queryKey: key, queryFn: () => hooksApi.list(scope, id) });
  const [form, setForm] = useState({
    stage: "after_plan" as HookStage,
    name: "",
    command: "",
    on_failure: "fail" as "fail" | "warn",
  });
  const create = useMutation({
    mutationFn: () => hooksApi.create(scope, id, form),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: key });
      setForm({ ...form, name: "", command: "" });
    },
  });
  const remove = useMutation({
    mutationFn: (hookId: string) => hooksApi.remove(scope, id, hookId),
    onSuccess: () => qc.invalidateQueries({ queryKey: key }),
  });

  return (
    <Card>
      <div className="mb-2 text-[13px] font-medium">Platform hooks</div>
      <div className="flex flex-col gap-2">
        {(data ?? []).map((h) => (
          <ItemTile key={h.id}>
            <div className="flex items-start justify-between gap-3">
              <div className="flex min-w-0 flex-wrap items-center gap-2">
                <Badge>{h.stage}</Badge>
                <span className="text-[13px] font-medium">{h.name}</span>
                <Badge
                  color={
                    h.on_failure === "warn"
                      ? "var(--color-state-unconfirmed)"
                      : "var(--color-state-failed)"
                  }
                >
                  on fail: {h.on_failure}
                </Badge>
              </div>
              <DeleteButton label="Delete hook" onClick={() => remove.mutate(h.id)} />
            </div>
            <div className="font-data mt-2 truncate text-[11px]" style={{ color: "var(--color-text-secondary)" }} title={h.command}>
              {h.command}
            </div>
          </ItemTile>
        ))}
        {data && data.length === 0 && (
          <span className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
            No platform hooks. These are non-bypassable by a PR.
          </span>
        )}
      </div>
      <form
        className="mt-3 flex items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <Field label="Stage">
          <Select value={form.stage} onChange={(e) => setForm({ ...form, stage: e.target.value as HookStage })}>
            {STAGES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Name">
          <TextInput value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
        </Field>
        <Field label="Command">
          <TextInput value={form.command} onChange={(e) => setForm({ ...form, command: e.target.value })} required />
        </Field>
        <Field label="On failure">
          <Select value={form.on_failure} onChange={(e) => setForm({ ...form, on_failure: e.target.value as "fail" | "warn" })}>
            <option value="fail">fail</option>
            <option value="warn">warn</option>
          </Select>
        </Field>
        <Button type="submit" disabled={create.isPending}>
          Add hook
        </Button>
      </form>
    </Card>
  );
}
