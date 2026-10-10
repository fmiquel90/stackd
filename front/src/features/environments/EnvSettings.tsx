import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import { environments, tiers, type EnvironmentPatch } from "@/api/resources";
import type { Environment } from "@/api/types";
import { Checkbox, LabelsEditor } from "@/components/ui";
import { Button } from "@/ui/Button";
import { Banner, Field, Input, NativeSelect } from "@/ui/primitives";
import { TypeNameDialog } from "@/ui/TypeNameDialog";

// Environment settings (DESIGN §5.8) with friction on the safety switches (§5.7): turning off
// `protected` / four-eyes, or turning on mock / fallback apply, asks for the env name with the
// consequence spelled out. Turning a safety switch on has no friction.

type Form = EnvironmentPatch & { backend_config: Record<string, string>; labels: Record<string, string> };

const toStrings = (o: Record<string, unknown> | null) =>
  Object.fromEntries(Object.entries(o ?? {}).map(([k, v]) => [k, String(v)]));

/** Changes that weaken a safety control, with what they mean (DESIGN §5.7). */
function weakenings(env: Environment, f: Form, fourEyesTier: boolean): string[] {
  const out: string[] = [];
  if (env.protected && f.protected === false)
    out.push("not protected: autodeploy becomes possible and four-eyes no longer comes from the protection");
  if (
    env.require_second_pair_of_eyes &&
    f.require_second_pair_of_eyes === false &&
    !fourEyesTier &&
    !f.protected
  )
    out.push("no second pair of eyes: whoever triggers a run will be able to confirm it");
  if (!env.allow_mock_apply && f.allow_mock_apply)
    out.push("mocked plans can be applied: an apply may use values that are not real");
  if (!env.allow_fallback_apply && f.allow_fallback_apply)
    out.push("fallback secrets can be applied: an apply may use a value that is not the real secret");
  return out;
}

export function EnvSettings({
  env,
  canWrite,
  isAdmin,
  onRenamed,
  onDeleted,
}: {
  env: Environment;
  canWrite: boolean;
  isAdmin: boolean;
  onRenamed: (name: string) => void;
  onDeleted: () => void;
}) {
  const qc = useQueryClient();
  const catalog = useQuery({ queryKey: ["tiers"], queryFn: tiers.list });
  const [form, setForm] = useState<Form>({
    name: env.name,
    tier: env.tier,
    branch: env.branch,
    protected: env.protected,
    autodeploy: env.autodeploy,
    require_second_pair_of_eyes: env.require_second_pair_of_eyes,
    managed_state: env.managed_state,
    allow_mock_apply: env.allow_mock_apply,
    allow_fallback_apply: env.allow_fallback_apply,
    drift_check_enabled: env.drift_check_enabled,
    backend_config_file: env.backend_config_file ?? "",
    backend_config: toStrings(env.backend_config),
    labels: toStrings(env.labels),
  });
  const [confirming, setConfirming] = useState(false);
  const set = (patch: Partial<Form>) => setForm((f) => ({ ...f, ...patch }));
  const fourEyesTier = !!catalog.data?.find((t) => t.name === form.tier)?.requires_four_eyes;
  const weak = weakenings(env, form, fourEyesTier);

  const save = useMutation({
    mutationFn: () =>
      environments.update(env.id, {
        ...form,
        backend_config_file: form.backend_config_file || null,
        backend_config: Object.keys(form.backend_config).length ? form.backend_config : null,
      }),
    onSuccess: (updated) => {
      qc.invalidateQueries({ queryKey: ["environments", env.stack_id] });
      qc.invalidateQueries({ queryKey: ["environment", env.id] });
      if (updated.name !== env.name) onRenamed(updated.name);
    },
  });

  const remove = () =>
    environments.remove(env.id).then(() => {
      qc.invalidateQueries({ queryKey: ["environments", env.stack_id] });
      onDeleted();
    });

  const disabled = !canWrite;
  return (
    <div className="flex max-w-[720px] flex-col gap-6">
      <form
        className="flex flex-col gap-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (weak.length) setConfirming(true);
          else save.mutate();
        }}
      >
        <fieldset disabled={disabled} className="flex flex-wrap items-end gap-3">
          <Field label="Name">
            <Input value={form.name ?? ""} onChange={(e) => set({ name: e.target.value })} required />
          </Field>
          <Field label="Tier">
            <NativeSelect value={form.tier ?? ""} onChange={(e) => set({ tier: e.target.value })}>
              {(catalog.data ?? []).map((t) => (
                <option key={t.name} value={t.name}>
                  {t.name}
                  {t.requires_four_eyes ? " (four-eyes)" : ""}
                </option>
              ))}
            </NativeSelect>
          </Field>
          <Field label="Branch">
            <Input value={form.branch ?? ""} onChange={(e) => set({ branch: e.target.value })} required />
          </Field>
        </fieldset>

        <fieldset disabled={disabled} className="flex flex-col gap-2">
          <legend className="eyebrow mb-1 text-muted">Protections</legend>
          <Checkbox
            checked={!!form.protected}
            onChange={(v) => set({ protected: v })}
            label="protected — confirmation always required, four-eyes"
          />
          <Checkbox
            checked={!!form.require_second_pair_of_eyes}
            onChange={(v) => set({ require_second_pair_of_eyes: v })}
            label="second pair of eyes — the triggerer cannot confirm"
          />
          <Checkbox
            checked={!!form.autodeploy}
            onChange={(v) => set({ autodeploy: v })}
            label="autodeploy — apply a clean plan without confirmation"
          />
          <Checkbox
            checked={!!form.allow_mock_apply}
            onChange={(v) => set({ allow_mock_apply: v })}
            label="allow applying mocked plans"
          />
          <Checkbox
            checked={!!form.allow_fallback_apply}
            onChange={(v) => set({ allow_fallback_apply: v })}
            label="allow applying with fallback secrets"
          />
          <Checkbox
            checked={!!form.drift_check_enabled}
            onChange={(v) => set({ drift_check_enabled: v })}
            label="drift detection"
          />
          {fourEyesTier && (
            <p className="text-[12px] text-muted">Tier {form.tier} requires four-eyes on its own.</p>
          )}
        </fieldset>

        <fieldset disabled={disabled} className="flex flex-col gap-3">
          <legend className="eyebrow mb-1 text-muted">State</legend>
          <Checkbox
            checked={!!form.managed_state}
            onChange={(v) => set({ managed_state: v })}
            label="managed state (Stackd's HTTP backend, SPECS §11)"
          />
          {!form.managed_state && (
            <>
              <Field
                label="Backend config file"
                help="Repository-relative, passed as -backend-config at init."
              >
                <Input
                  value={form.backend_config_file ?? ""}
                  placeholder="prod.config"
                  onChange={(e) => set({ backend_config_file: e.target.value })}
                />
              </Field>
              <Field label="Backend config values" help="Each passed as -backend-config=key=value.">
                <LabelsEditor
                  value={form.backend_config}
                  onChange={(backend_config) => set({ backend_config })}
                  keyPlaceholder="bucket / key / region…"
                />
              </Field>
            </>
          )}
        </fieldset>

        <fieldset disabled={disabled}>
          <Field label="Labels" help="Matched by variable-set selectors (SPECS §3.4).">
            <LabelsEditor value={form.labels} onChange={(labels) => set({ labels })} />
          </Field>
        </fieldset>

        {save.error && (
          <Banner tone="failed" role="alert" title="Settings not saved">
            {save.error instanceof ApiError ? (save.error.detail ?? save.error.title) : String(save.error)}
          </Banner>
        )}
        {save.isSuccess && !save.isPending && (
          <p role="status" className="text-[12px] text-muted">
            Saved.
          </p>
        )}
        {canWrite && (
          <div>
            <Button type="submit" variant="primary" loading={save.isPending}>
              Save settings
            </Button>
          </div>
        )}
      </form>

      <TypeNameDialog
        trigger={null}
        open={confirming}
        onOpenChange={setConfirming}
        title={`Weaken the safety of ${env.name}?`}
        consequence={
          <ul className="list-disc pl-5">
            {weak.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        }
        name={env.name}
        confirmLabel="Save these settings"
        variant="decision"
        onConfirm={() => save.mutateAsync()}
      />

      {isAdmin && (
        <section
          aria-labelledby="danger-title"
          className="flex flex-col gap-2 rounded-base border border-st-failed p-3"
        >
          <h2 id="danger-title" className="text-section font-semibold">
            Danger zone
          </h2>
          <p className="text-[12px] text-muted">
            Deleting the environment removes its runs and its state history. This cannot be undone.
          </p>
          <div>
            <TypeNameDialog
              trigger={<Button variant="danger">Delete environment {env.name}</Button>}
              title={`Delete ${env.name}`}
              consequence="Removes this environment with all its runs and state history."
              name={env.name}
              confirmLabel={`Delete ${env.name}`}
              onConfirm={remove}
            />
          </div>
        </section>
      )}
    </div>
  );
}
