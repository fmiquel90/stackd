import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { X } from "lucide-react";
import { type NewSecretSource, type SecretProvider, secretSourcesApi } from "@/api/resources";
import { Button, Card, Field, Select, TextInput } from "@/components/ui";

// Per-provider specifics: the credential's name and the reference (locator) format both differ —
// e.g. Proton Pass uses pass://… while Vault/AWS would use their own. Add an entry per provider.
const PROVIDERS: Record<SecretProvider, { label: string; credential: string; refExample: string }> = {
  proton_pass: {
    label: "Proton Pass",
    credential: "Proton PAT / AI Access Token",
    refExample: "pass://vault/item/field",
  },
};

// Manage the external secret sources of a space (SPECS §15.1). Sources are space-scoped; variables
// in any stack of the space reference them. The bootstrap credential is write-only — never shown.
export function SecretSourcesPanel({ spaceId }: { spaceId: string }) {
  const qc = useQueryClient();
  const key = ["secret-sources", spaceId];
  const { data } = useQuery({ queryKey: key, queryFn: () => secretSourcesApi.list(spaceId) });
  const [form, setForm] = useState<NewSecretSource>({
    name: "",
    provider: "proton_pass",
    bootstrap_secret: "",
  });

  const create = useMutation({
    mutationFn: () => secretSourcesApi.create(spaceId, form),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: key });
      setForm((f) => ({ ...f, name: "", bootstrap_secret: "" }));
    },
  });
  const remove = useMutation({
    mutationFn: (srcId: string) => secretSourcesApi.remove(spaceId, srcId),
    onSuccess: () => qc.invalidateQueries({ queryKey: key }),
  });

  return (
    <Card>
      <div className="mb-1 text-[13px] font-medium">Secret sources</div>
      <div className="mb-2 text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
        Inject variable values straight from a secrets manager. A variable points at a source via a
        provider-specific reference (for {PROVIDERS[form.provider].label}:{" "}
        <span className="font-data">{PROVIDERS[form.provider].refExample}</span>); the value is
        fetched live at run time and never stored here.
      </div>

      <div className="flex flex-col gap-1">
        {(data ?? []).map((s) => (
          <div key={s.id} className="font-data flex items-center gap-2 text-[12px]">
            <span>{s.name}</span>
            <span style={{ color: "var(--color-text-secondary)" }}>· {s.provider}</span>
            <button
              type="button"
              aria-label={`Delete ${s.name}`}
              style={{ color: "var(--color-state-failed)" }}
              onClick={() => remove.mutate(s.id)}
            >
              <X size={13} strokeWidth={1.75} aria-hidden />
            </button>
          </div>
        ))}
        {!data && (
          <span className="text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
            Loading…
          </span>
        )}
        {data && data.length === 0 && (
          <span className="text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
            No secret source yet.
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
          <TextInput
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            required
          />
        </Field>
        <Field label="Provider">
          <Select
            value={form.provider}
            onChange={(e) =>
              setForm({ ...form, provider: e.target.value as NewSecretSource["provider"] })
            }
          >
            {(Object.entries(PROVIDERS) as [SecretProvider, (typeof PROVIDERS)[SecretProvider]][]).map(
              ([value, meta]) => (
                <option key={value} value={value}>
                  {meta.label}
                </option>
              ),
            )}
          </Select>
        </Field>
        <Field label="Bootstrap token (write-only)">
          <TextInput
            type="password"
            value={form.bootstrap_secret}
            placeholder={PROVIDERS[form.provider].credential}
            onChange={(e) => setForm({ ...form, bootstrap_secret: e.target.value })}
            required
          />
        </Field>
        <Button type="submit" variant="accent" disabled={create.isPending}>
          Add source
        </Button>
      </form>

      {(create.isError || remove.isError) && (
        <div className="mt-2 font-data text-[12px]" style={{ color: "var(--color-state-failed)" }}>
          {((create.error ?? remove.error) as Error).message}
        </div>
      )}
    </Card>
  );
}
