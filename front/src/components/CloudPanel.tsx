import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import { cloudApi } from "@/api/resources";
import { useIsAdmin } from "@/auth/session";
import { Button, Card, Field, TextInput } from "@/components/ui";

export function CloudPanel({ envId }: { envId: string }) {
  const qc = useQueryClient();
  const isAdmin = useIsAdmin();
  const key = ["cloud-integration", envId];
  const { data, isError } = useQuery({
    queryKey: key,
    queryFn: () => cloudApi.get(envId),
    retry: false,
  });
  const [form, setForm] = useState({ plan_role_arn: "", apply_role_arn: "", region: "" });
  const loaded = useRef(false);
  useEffect(() => {
    // Hydrate the form once; a background refetch must not clobber in-progress edits.
    if (data && !loaded.current) {
      loaded.current = true;
      setForm({ plan_role_arn: data.plan_role_arn, apply_role_arn: data.apply_role_arn, region: data.region ?? "" });
    }
  }, [data]);

  const save = useMutation({
    mutationFn: () => cloudApi.put(envId, { ...form, region: form.region || null }),
    onSuccess: () => qc.invalidateQueries({ queryKey: key }),
  });
  const test = useMutation({ mutationFn: () => cloudApi.test(envId) });
  const remove = useMutation({
    mutationFn: () => cloudApi.remove(envId),
    onSuccess: () => qc.invalidateQueries({ queryKey: key }),
  });

  const configured = Boolean(data) && !isError;

  return (
    <Card>
      <div className="mb-2 flex items-center gap-3">
        <span className="text-[13px] font-medium">Cloud integration (OIDC)</span>
        <span
          className="font-data rounded-badge px-1.5 text-[12px]"
          style={{ color: configured ? "var(--color-state-finished)" : "var(--color-text-secondary)", border: "1px solid var(--color-border)" }}
        >
          {configured ? "dynamic credentials" : "not configured (static fallback)"}
        </span>
      </div>

      {/* Editing OIDC roles is admin-only (server enforces it on PUT/DELETE/test). Others see it read-only. */}
      {!isAdmin ? (
        <div className="font-data flex flex-col gap-1 text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
          {configured ? (
            <>
              <div>plan role: {data!.plan_role_arn}</div>
              <div>apply role: {data!.apply_role_arn}</div>
              {data!.region && <div>region: {data!.region}</div>}
            </>
          ) : (
            <div>No cloud integration configured.</div>
          )}
          <div className="mt-1">Editing cloud integration requires the admin role.</div>
        </div>
      ) : (
        <>
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <Field label="Plan role ARN">
          <TextInput value={form.plan_role_arn} onChange={(e) => setForm({ ...form, plan_role_arn: e.target.value })} required />
        </Field>
        <Field label="Apply role ARN">
          <TextInput value={form.apply_role_arn} onChange={(e) => setForm({ ...form, apply_role_arn: e.target.value })} required />
        </Field>
        <Field label="Region">
          <TextInput value={form.region} onChange={(e) => setForm({ ...form, region: e.target.value })} />
        </Field>
        <Button type="submit" variant="accent" disabled={save.isPending}>
          Save
        </Button>
        {configured && (
          <>
            <Button type="button" onClick={() => test.mutate()} disabled={test.isPending}>
              Test AssumeRole
            </Button>
            <Button type="button" onClick={() => remove.mutate()}>
              Remove
            </Button>
          </>
        )}
      </form>
      {test.data && (
        <p className="font-data mt-2 text-[12px]" style={{ color: "var(--color-state-finished)" }}>
          assumed: {test.data.assumed_role}
        </p>
      )}
      {test.error instanceof ApiError && (
        <p className="font-data mt-2 text-[12px]" style={{ color: "var(--color-state-failed)" }}>
          {test.error.detail ?? test.error.title}
        </p>
      )}
      <p className="mt-2 text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
        Plan and apply assume different roles; the token <span className="font-data">sub=run:tier:stack:phase</span>.
      </p>
        </>
      )}
    </Card>
  );
}
