import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { X } from "lucide-react";
import {
  notificationsApi,
  type HookScope,
  type NotificationKind,
  type NotificationState,
} from "@/api/resources";
import { Button, Card, Field, Select, TextInput } from "@/components/ui";

const STATES: NotificationState[] = ["unconfirmed", "finished", "failed"];

export function NotificationsPanel({ scope, id }: { scope: HookScope; id: string }) {
  const qc = useQueryClient();
  const key = ["notifications", scope, id];
  const { data } = useQuery({ queryKey: key, queryFn: () => notificationsApi.list(scope, id) });
  const [form, setForm] = useState({
    name: "",
    kind: "slack" as NotificationKind,
    url: "",
    on_states: ["unconfirmed", "failed"] as NotificationState[],
  });

  const create = useMutation({
    mutationFn: () => notificationsApi.create(scope, id, form),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: key });
      setForm({ ...form, name: "", url: "" });
    },
  });
  const toggle = useMutation({
    mutationFn: (t: { id: string; enabled: boolean }) =>
      notificationsApi.update(scope, id, t.id, { enabled: !t.enabled }),
    onSuccess: () => qc.invalidateQueries({ queryKey: key }),
  });
  const remove = useMutation({
    mutationFn: (targetId: string) => notificationsApi.remove(scope, id, targetId),
    onSuccess: () => qc.invalidateQueries({ queryKey: key }),
  });
  const [tested, setTested] = useState<Record<string, "ok" | "fail">>({});
  const sendTest = useMutation({
    mutationFn: (targetId: string) => notificationsApi.test(scope, id, targetId),
    onSuccess: (_r, targetId) => setTested((m) => ({ ...m, [targetId]: "ok" })),
    onError: (_e, targetId) => setTested((m) => ({ ...m, [targetId]: "fail" })),
  });

  const flipState = (s: NotificationState) =>
    setForm((f) => ({
      ...f,
      on_states: f.on_states.includes(s)
        ? f.on_states.filter((x) => x !== s)
        : [...f.on_states, s],
    }));

  return (
    <Card>
      <div className="mb-1 text-[13px] font-medium">Notifications</div>
      <div className="mb-2 text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
        Outbound webhooks fired on run events — close the approval loop without watching the UI.
      </div>
      <div className="flex flex-col gap-1">
        {(data ?? []).map((t) => (
          <div key={t.id} className="font-data flex items-center gap-2 text-[12px]">
            <span>{t.name}</span>
            <span style={{ color: "var(--color-text-secondary)" }}>{t.kind}</span>
            <span style={{ color: "var(--color-text-secondary)" }}>{t.on_states.join(", ")}</span>
            {!t.enabled && (
              <span style={{ color: "var(--color-state-unconfirmed)" }}>(disabled)</span>
            )}
            <button
              type="button"
              className="ui-btn"
              onClick={() => toggle.mutate({ id: t.id, enabled: t.enabled })}
              style={{ color: "var(--color-text-secondary)" }}
            >
              {t.enabled ? "disable" : "enable"}
            </button>
            <button
              type="button"
              className="ui-btn"
              onClick={() => sendTest.mutate(t.id)}
              disabled={sendTest.isPending}
              style={{ color: "var(--color-accent)" }}
            >
              test
            </button>
            {tested[t.id] === "ok" && (
              <span style={{ color: "var(--color-state-finished)" }}>sent ✓</span>
            )}
            {tested[t.id] === "fail" && (
              <span style={{ color: "var(--color-state-failed)" }}>failed</span>
            )}
            <button
              type="button"
              aria-label="Delete notification target"
              className="ui-btn"
              onClick={() => remove.mutate(t.id)}
              style={{ color: "var(--color-text-secondary)" }}
            >
              <X size={13} strokeWidth={1.75} aria-hidden />
            </button>
          </div>
        ))}
        {data && data.length === 0 && (
          <span className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
            No notification targets.
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
        <Field label="Name">
          <TextInput value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
        </Field>
        <Field label="Kind">
          <Select
            value={form.kind}
            onChange={(e) => setForm({ ...form, kind: e.target.value as NotificationKind })}
          >
            <option value="slack">slack</option>
            <option value="webhook">webhook</option>
          </Select>
        </Field>
        <Field label="Webhook URL">
          <TextInput value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} required />
        </Field>
        <Field label="On states">
          <div className="flex items-center gap-2 text-[12px]">
            {STATES.map((s) => (
              <label key={s} className="font-data flex items-center gap-1">
                <input
                  type="checkbox"
                  checked={form.on_states.includes(s)}
                  onChange={() => flipState(s)}
                />
                {s}
              </label>
            ))}
          </div>
        </Field>
        <Button type="submit" disabled={create.isPending || form.on_states.length === 0}>
          Add target
        </Button>
      </form>
    </Card>
  );
}
