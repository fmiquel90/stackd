import { useEffect, useRef, useState } from "react";
import { Plus, TriangleAlert } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import { cloudApi, type CloudProfile } from "@/api/resources";
import { useIsAdmin } from "@/auth/session";
import { Button, Card, Field, TextInput } from "@/components/ui";
import { Button as UiButton } from "@/ui/Button";

/** One editable row of the named profiles (SPECS §10.6); `region` is "" when unset. */
export interface ProfileRow {
  name: string;
  plan_role_arn: string;
  apply_role_arn: string;
  region: string;
}

export function rowsFromProfiles(profiles: Record<string, CloudProfile> | undefined): ProfileRow[] {
  return Object.entries(profiles ?? {})
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([name, p]) => ({ name, ...p, region: p.region ?? "" }));
}

export function profilesFromRows(rows: ProfileRow[]): Record<string, CloudProfile> {
  return Object.fromEntries(
    rows.map((r) => [
      r.name.trim(),
      {
        plan_role_arn: r.plan_role_arn.trim(),
        apply_role_arn: r.apply_role_arn.trim(),
        region: r.region.trim() || null,
      },
    ]),
  );
}

/** What blocks Save before the API's own validation: a JSON map would silently keep one of two
 * rows of the same name, and `default` is the integration's default role. */
export function profileRowsError(rows: ProfileRow[]): string | null {
  const names = rows.map((r) => r.name.trim());
  if (names.some((n) => !n)) return "Every profile needs a name.";
  if (names.includes("default")) return "`default` is reserved: it is the default role above.";
  const dup = names.find((n, i) => names.indexOf(n) !== i);
  return dup ? `Profile ${dup} is listed twice.` : null;
}

const EMPTY_ROW: ProfileRow = { name: "", plan_role_arn: "", apply_role_arn: "", region: "" };

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
  const [rows, setRows] = useState<ProfileRow[]>([]);
  const loaded = useRef(false);
  useEffect(() => {
    // Hydrate the form once; a background refetch must not clobber in-progress edits.
    if (data && !loaded.current) {
      loaded.current = true;
      setForm({
        plan_role_arn: data.plan_role_arn,
        apply_role_arn: data.apply_role_arn,
        region: data.region ?? "",
      });
      setRows(rowsFromProfiles(data.profiles));
    }
  }, [data]);

  const rowsError = profileRowsError(rows);
  const save = useMutation({
    mutationFn: () =>
      cloudApi.put(envId, { ...form, region: form.region || null, profiles: profilesFromRows(rows) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: key }),
  });
  const test = useMutation({ mutationFn: () => cloudApi.test(envId) });
  const remove = useMutation({
    mutationFn: () => cloudApi.remove(envId),
    onSuccess: () => qc.invalidateQueries({ queryKey: key }),
  });

  const configured = Boolean(data) && !isError;
  const setRow = (i: number, patch: Partial<ProfileRow>) =>
    setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));

  return (
    <Card>
      <div className="mb-2 flex items-center gap-3">
        <span className="text-[13px] font-medium">Cloud integration (OIDC)</span>
        <span
          className={`rounded-badge border border-control px-1.5 font-data text-[12px] ${configured ? "text-primary" : "text-muted"}`}
        >
          {configured ? "dynamic credentials" : "not configured (static fallback)"}
        </span>
      </div>

      {configured &&
        data!.warnings.map((w) => (
          <p key={w.code} role="alert" className="font-data mb-2 text-[12px] text-st-decision">
            <TriangleAlert size={12} strokeWidth={1.5} aria-hidden className="mr-1 inline align-[-1px]" />
            Not passed to runs: {w.variables.join(", ")}. {w.detail}
          </p>
        ))}

      {/* Editing OIDC roles is admin-only (server enforces it on PUT/DELETE/test). Others see it read-only. */}
      {!isAdmin ? (
        <div className="font-data flex flex-col gap-1 text-[12px] text-muted">
          {configured ? (
            <>
              <div>plan role: {data!.plan_role_arn}</div>
              <div>apply role: {data!.apply_role_arn}</div>
              {data!.region && <div>region: {data!.region}</div>}
              {rowsFromProfiles(data!.profiles).map((p) => (
                <div key={p.name}>
                  profile {p.name}: plan {p.plan_role_arn} · apply {p.apply_role_arn}
                  {p.region && ` · region ${p.region}`}
                </div>
              ))}
            </>
          ) : (
            <div>No cloud integration configured.</div>
          )}
          <div className="mt-1">Editing cloud integration requires the admin role.</div>
        </div>
      ) : (
        <>
          <form
            className="flex flex-col gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              save.mutate();
            }}
          >
            <div className="flex flex-wrap items-end gap-2">
              <Field label="Plan role ARN">
                <TextInput
                  value={form.plan_role_arn}
                  onChange={(e) => setForm({ ...form, plan_role_arn: e.target.value })}
                  required
                />
              </Field>
              <Field label="Apply role ARN">
                <TextInput
                  value={form.apply_role_arn}
                  onChange={(e) => setForm({ ...form, apply_role_arn: e.target.value })}
                  required
                />
              </Field>
              <Field label="Region">
                <TextInput
                  value={form.region}
                  onChange={(e) => setForm({ ...form, region: e.target.value })}
                />
              </Field>
            </div>

            <fieldset className="flex flex-col gap-2">
              <legend className="eyebrow mb-1 text-muted">Named AWS profiles</legend>
              <p className="text-[12px] text-muted">
                For Terraform that names a profile (<span className="font-data">profile = "…"</span> in a
                provider, a backend config or a remote state): each name assumes its own plan or apply role,
                with the run&apos;s token. Code that names no profile uses the roles above.
              </p>
              {rows.map((r, i) => (
                <div key={i} className="flex flex-wrap items-end gap-2">
                  <Field label="Profile name">
                    <TextInput
                      value={r.name}
                      onChange={(e) => setRow(i, { name: e.target.value })}
                      required
                      className="font-data"
                    />
                  </Field>
                  <Field label="Plan role ARN">
                    <TextInput
                      value={r.plan_role_arn}
                      onChange={(e) => setRow(i, { plan_role_arn: e.target.value })}
                      required
                    />
                  </Field>
                  <Field label="Apply role ARN">
                    <TextInput
                      value={r.apply_role_arn}
                      onChange={(e) => setRow(i, { apply_role_arn: e.target.value })}
                      required
                    />
                  </Field>
                  <Field label="Region">
                    <TextInput value={r.region} onChange={(e) => setRow(i, { region: e.target.value })} />
                  </Field>
                  <UiButton
                    type="button"
                    size="sm"
                    variant="ghost"
                    onClick={() => setRows(rows.filter((_, j) => j !== i))}
                  >
                    Remove <span className="sr-only">profile {r.name || i + 1}</span>
                  </UiButton>
                </div>
              ))}
              <div>
                <UiButton
                  type="button"
                  size="sm"
                  variant="secondary"
                  icon={<Plus size={13} strokeWidth={1.75} aria-hidden />}
                  onClick={() => setRows([...rows, EMPTY_ROW])}
                >
                  Add profile
                </UiButton>
              </div>
              {rows.length > 0 && rowsError && (
                <p role="alert" className="font-data text-[12px] text-st-failed">
                  {rowsError}
                </p>
              )}
            </fieldset>

            <div className="flex flex-wrap gap-2">
              <Button type="submit" variant="accent" disabled={save.isPending || Boolean(rowsError)}>
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
            </div>
          </form>
          {save.error instanceof ApiError && (
            <p role="alert" className="font-data mt-2 text-[12px] text-st-failed">
              {save.error.detail ?? save.error.title}
            </p>
          )}
          {test.data && (
            <p className="font-data mt-2 text-[12px] text-st-finished">assumed: {test.data.assumed_role}</p>
          )}
          {test.error instanceof ApiError && (
            <p className="font-data mt-2 text-[12px] text-st-failed">
              {test.error.detail ?? test.error.title}
            </p>
          )}
          <p className="mt-2 text-[12px] text-muted">
            Plan and apply assume different roles; the token{" "}
            <span className="font-data">sub=run:tier:stack:phase</span>.
          </p>
        </>
      )}
    </Card>
  );
}
