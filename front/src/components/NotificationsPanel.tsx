import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { MessageSquare, Webhook } from "lucide-react";
import {
  notificationsApi,
  type HookScope,
  type NotificationKind,
  type NotificationState,
} from "@/api/resources";
import { Badge, Button, Card, DeleteButton, Field, ItemTile, Select, TextInput } from "@/components/ui";

const STATES: NotificationState[] = ["unconfirmed", "finished", "failed"];

// Each fire-on state pairs a token color with its label (color is never the only signal).
const STATE_COLOR: Record<NotificationState, string> = {
  unconfirmed: "var(--color-state-unconfirmed)",
  finished: "var(--color-state-finished)",
  failed: "var(--color-state-failed)",
};

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
      <div className="flex flex-col gap-2">
        {(data ?? []).map((t) => (
          <ItemTile key={t.id} dimmed={!t.enabled}>
            <div className="flex items-start justify-between gap-3">
              <div className="flex min-w-0 flex-wrap items-center gap-2">
                <span
                  aria-hidden
                  style={{
                    width: 8,
                    height: 8,
                    borderRadius: 4,
                    backgroundColor: t.enabled
                      ? "var(--color-state-finished)"
                      : "var(--color-state-queued)",
                  }}
                />
                <span className="font-data text-[13px] font-medium">{t.name}</span>
                <Badge icon={t.kind === "slack" ? MessageSquare : Webhook}>{t.kind}</Badge>
                <span className="font-data text-[11px]" style={{ color: "var(--color-text-secondary)" }}>
                  {t.enabled ? "enabled" : "disabled"}
                </span>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                {tested[t.id] === "ok" && (
                  <span className="text-[12px]" style={{ color: "var(--color-state-finished)" }}>sent ✓</span>
                )}
                {tested[t.id] === "fail" && (
                  <span className="text-[12px]" style={{ color: "var(--color-state-failed)" }}>failed</span>
                )}
                <button
                  type="button"
                  className="ui-btn rounded-base px-2 py-1 text-[12px]"
                  onClick={() => sendTest.mutate(t.id)}
                  disabled={sendTest.isPending}
                  style={{ border: "1px solid var(--color-border)", color: "var(--color-accent)" }}
                >
                  Test
                </button>
                <button
                  type="button"
                  className="ui-btn rounded-base px-2 py-1 text-[12px]"
                  onClick={() => toggle.mutate({ id: t.id, enabled: t.enabled })}
                  style={{ border: "1px solid var(--color-border)", color: "var(--color-text-secondary)" }}
                >
                  {t.enabled ? "Disable" : "Enable"}
                </button>
                <DeleteButton label="Delete notification target" onClick={() => remove.mutate(t.id)} />
              </div>
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <span className="font-data text-[11px]" style={{ color: "var(--color-text-secondary)" }}>
                fires on
              </span>
              {t.on_states.map((s) => (
                <Badge key={s} color={STATE_COLOR[s]}>
                  {s}
                </Badge>
              ))}
            </div>
            <div
              className="font-data mt-1 truncate text-[11px]"
              style={{ color: "var(--color-text-secondary)" }}
              title={t.url}
            >
              {t.url}
            </div>
          </ItemTile>
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
