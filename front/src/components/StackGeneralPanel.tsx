import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { type StackPatch, stacks } from "@/api/resources";
import type { RepoAuthKind, Tool } from "@/api/types";
import { useIsAdmin } from "@/auth/session";
import { Button, Card, Field, Select, TextInput } from "@/components/ui";

// Deleting a stack cascades its environments, runs and state — high impact, so we gate it behind a
// type-the-name confirmation (friction proportional to risk, DESIGN §5.2).
function DeleteStackPanel({ stackId, name }: { stackId: string; name: string }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [typed, setTyped] = useState("");
  const remove = useMutation({
    mutationFn: () => stacks.remove(stackId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["stacks"] });
      navigate("/stacks");
    },
  });
  return (
    <Card>
      <div className="mb-1 text-[13px] font-medium" style={{ color: "var(--color-state-failed)" }}>
        Danger zone
      </div>
      <div className="mb-2 text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
        Deleting this stack removes all its environments, runs, state history and config. This cannot
        be undone.
      </div>
      <div className="flex items-end gap-2">
        <Field label={`Type "${name}" to confirm`}>
          <TextInput value={typed} onChange={(e) => setTyped(e.target.value)} />
        </Field>
        <button
          type="button"
          className="ui-btn rounded-base px-3 py-1.5 text-[13px] font-medium disabled:opacity-50"
          style={{ border: "1px solid var(--color-state-failed)", color: "var(--color-state-failed)" }}
          disabled={typed !== name || remove.isPending}
          onClick={() => remove.mutate()}
        >
          Delete stack
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

interface FormState {
  name: string;
  description: string;
  repo_url: string;
  project_root: string;
  tool: Tool;
  tool_version: string;
  repo_auth_kind: RepoAuthKind;
  repo_secret: string; // empty = leave the stored credential untouched
  webhook_secret: string; // empty = leave the stored HMAC secret untouched
}

// Edit a stack's identity & source: repo URL/auth, project root, and the IaC tool + version. The
// repo credential is write-only (only sent when a new value is typed).
export function StackGeneralPanel({ stackId }: { stackId: string }) {
  const qc = useQueryClient();
  const isAdmin = useIsAdmin();
  const { data: stack } = useQuery({ queryKey: ["stack", stackId], queryFn: () => stacks.get(stackId) });
  const [form, setForm] = useState<FormState | null>(null);

  useEffect(() => {
    // Initialize once from the loaded stack; don't clobber in-progress edits on a background refetch.
    if (!stack || form !== null) return;
    setForm({
      name: stack.name,
      description: stack.description ?? "",
      repo_url: stack.repo_url,
      project_root: stack.project_root,
      tool: stack.tool,
      tool_version: stack.tool_version,
      repo_auth_kind: stack.repo_auth_kind,
      repo_secret: "",
      webhook_secret: "",
    });
  }, [stack]);

  const save = useMutation({
    mutationFn: (body: StackPatch) => stacks.update(stackId, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["stack", stackId] });
      qc.invalidateQueries({ queryKey: ["stacks"] });
    },
  });
  const check = useMutation({ mutationFn: () => stacks.checkRepo(stackId) });

  if (!form || !stack) return <span className="font-data text-[12px]">Loading…</span>;
  const stackName = stack.name;

  const submit = () => {
    const body: StackPatch = {
      name: form.name,
      description: form.description || null,
      repo_url: form.repo_url,
      project_root: form.project_root,
      tool: form.tool,
      tool_version: form.tool_version,
      repo_auth_kind: form.repo_auth_kind,
    };
    if (form.repo_secret) body.repo_secret = form.repo_secret; // omit → keep the stored secret
    if (isAdmin && form.webhook_secret) body.webhook_secret = form.webhook_secret;
    save.mutate(body);
  };

  const set = (patch: Partial<FormState>) => setForm((f) => (f ? { ...f, ...patch } : f));

  return (
    <div className="flex flex-col gap-4">
    <Card>
      <form
        className="flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <div className="flex flex-wrap items-end gap-3">
          <Field label="Name">
            <TextInput value={form.name} onChange={(e) => set({ name: e.target.value })} required />
          </Field>
          <Field label="Tool">
            <Select value={form.tool} onChange={(e) => set({ tool: e.target.value as Tool })}>
              <option value="opentofu">opentofu</option>
              <option value="terraform">terraform</option>
            </Select>
          </Field>
          <Field label="Tool version">
            <TextInput value={form.tool_version} onChange={(e) => set({ tool_version: e.target.value })} required />
          </Field>
        </div>

        <Field label="Description">
          <TextInput value={form.description} onChange={(e) => set({ description: e.target.value })} />
        </Field>

        <div className="flex flex-wrap items-end gap-3">
          <Field label="Repository URL">
            <TextInput value={form.repo_url} onChange={(e) => set({ repo_url: e.target.value })} required />
          </Field>
          <Field label="Project root">
            <TextInput value={form.project_root} onChange={(e) => set({ project_root: e.target.value })} />
          </Field>
        </div>

        <div className="flex flex-wrap items-end gap-3">
          <Field label="Repo auth">
            <Select
              value={form.repo_auth_kind}
              onChange={(e) => set({ repo_auth_kind: e.target.value as RepoAuthKind })}
            >
              <option value="none">none</option>
              <option value="token">token</option>
              <option value="deploy_key">deploy_key</option>
            </Select>
          </Field>
          {form.repo_auth_kind !== "none" && (
            <Field label={`Credential${stack.has_repo_secret ? " (set — leave blank to keep)" : ""}`}>
              <TextInput
                type="password"
                value={form.repo_secret}
                placeholder={stack.has_repo_secret ? "••••••••" : "token / deploy key"}
                onChange={(e) => set({ repo_secret: e.target.value })}
              />
            </Field>
          )}
        </div>

        {/* Webhook HMAC secret (§5) — admin-only. Write-only: blank keeps the stored value. */}
        {isAdmin && (
          <Field label={`Webhook secret${stack.has_webhook_secret ? " (set — leave blank to keep)" : ""}`}>
            <TextInput
              type="password"
              value={form.webhook_secret}
              placeholder={stack.has_webhook_secret ? "••••••••" : "HMAC secret for GitHub webhooks"}
              onChange={(e) => set({ webhook_secret: e.target.value })}
            />
          </Field>
        )}

        <div className="flex items-center gap-2">
          <Button type="submit" variant="accent" disabled={save.isPending}>
            Save
          </Button>
          <Button type="button" onClick={() => check.mutate()} disabled={check.isPending}>
            Check repository
          </Button>
          {save.isSuccess && (
            <span className="font-data text-[12px]" style={{ color: "var(--color-state-finished)" }}>
              saved
            </span>
          )}
        </div>
      </form>

      {save.isError && (
        <div className="mt-2 font-data text-[12px]" style={{ color: "var(--color-state-failed)" }}>
          {(save.error as Error).message}
        </div>
      )}
      {check.data && (
        <div className="mt-2 font-data text-[12px]">
          <span style={{ color: check.data.ok ? "var(--color-state-finished)" : "var(--color-state-failed)" }}>
            {check.data.ok ? "reachable" : "unreachable"}
          </span>
          {check.data.branches.length > 0 && (
            <span style={{ color: "var(--color-text-secondary)" }}> · {check.data.branches.length} branches</span>
          )}
          {check.data.detail && <span style={{ color: "var(--color-text-secondary)" }}> · {check.data.detail}</span>}
        </div>
      )}
    </Card>
      {isAdmin && <DeleteStackPanel stackId={stackId} name={stackName} />}
    </div>
  );
}
