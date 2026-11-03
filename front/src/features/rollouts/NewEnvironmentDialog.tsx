import { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Copy } from "lucide-react";
import { ApiError } from "@/api/client";
import { environmentClones, type CloneRequest, type CloneResult } from "@/api/resources";
import type { TierDef } from "@/api/types";
import type { EnvEntry } from "@/features/data/spaceEnvs";
import { Button } from "@/ui/Button";
import { Dialog, DialogContent, DialogTrigger } from "@/ui/overlays";
import { Banner, Checkbox, Field, Input, NativeSelect } from "@/ui/primitives";

// "New environment from…" (DESIGN §5.11, SPECS §21.2): declare one environment across the space's
// stacks, modelled on a reference environment. Form → preview (a dry run of the real request) →
// create → what is left to fill.

export function parseLabels(text: string): Record<string, string> | undefined {
  const pairs = text
    .split(",")
    .map((p) => p.trim())
    .filter(Boolean)
    .map((p) => p.split("=").map((x) => x.trim()));
  if (!pairs.length) return undefined;
  return Object.fromEntries(pairs.filter((p) => p.length === 2 && p[0]));
}

function errorText(e: unknown): string {
  return e instanceof ApiError ? (e.detail ?? e.title) : String(e);
}

export function NewEnvironmentDialog({
  spaceId,
  spaceName,
  spaceBase,
  entries,
  tiers,
  isAdmin,
  onRollOut,
}: {
  spaceId: string;
  spaceName: string;
  /** `/s/<space>`: base of the env links. */
  spaceBase: string;
  entries: EnvEntry[];
  tiers: TierDef[];
  isAdmin: boolean;
  /** Offered once created (lot 3): start a rollout of the new environment. */
  onRollOut?: (envName: string) => void;
}) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const names = useMemo(() => [...new Set(entries.map((e) => e.env.name))].sort(), [entries]);
  const [reference, setReference] = useState("");
  const [name, setName] = useState("");
  const [tier, setTier] = useState("");
  const [branch, setBranch] = useState("");
  const [labels, setLabels] = useState("");
  const [copyCloud, setCopyCloud] = useState(false);
  const [preview, setPreview] = useState<CloneResult | null>(null);
  const [done, setDone] = useState<CloneResult | null>(null);

  const ref = reference || names[0] || "";
  const withRef = entries.filter((e) => e.env.name === ref);
  const without = [...new Set(entries.map((e) => e.stack.name))].filter(
    (s) => !withRef.some((e) => e.stack.name === s),
  );
  const envName = (id: string) => {
    const e = entries.find((x) => x.env.id === id);
    if (e) return `${e.stack.name}/${e.env.name}`;
    const c = (preview ?? done)?.environments.find((x) => x.id === id);
    return c ? `${c.stack_name}/${c.name}` : id;
  };

  const request = (dry: boolean): CloneRequest => ({
    reference_name: ref,
    name,
    overrides: {
      ...(tier ? { tier } : {}),
      ...(branch ? { branch } : {}),
      ...(parseLabels(labels) ? { labels: parseLabels(labels) } : {}),
    },
    copy_cloud_integration: copyCloud,
    dry_run: dry,
  });
  const previewM = useMutation({
    mutationFn: () => environmentClones.create(spaceId, request(true)),
    onSuccess: setPreview,
  });
  const createM = useMutation({
    mutationFn: () => environmentClones.create(spaceId, request(false)),
    onSuccess: (r) => {
      setDone(r);
      qc.invalidateQueries({ queryKey: ["environments"] });
      qc.invalidateQueries({ queryKey: ["graph"] });
    },
  });

  const reset = (v: boolean) => {
    setOpen(v);
    if (!v) {
      setPreview(null);
      setDone(null);
      previewM.reset();
      createM.reset();
      setName("");
      setTier("");
      setBranch("");
      setLabels("");
      setCopyCloud(false);
    }
  };

  const shown = done ?? preview;
  return (
    <Dialog open={open} onOpenChange={reset}>
      <DialogTrigger asChild>
        <Button icon={<Copy size={14} aria-hidden />} disabled={names.length === 0}>
          New environment from…
        </Button>
      </DialogTrigger>
      <DialogContent
        title={done ? "Environment created" : preview ? "2 · Review" : "1 · New environment"}
        description={`Declared once in every stack of ${spaceName} that has the reference environment.`}
      >
        {!shown ? (
          <form
            className="flex flex-col gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              previewM.mutate();
            }}
          >
            <Field label="Model it on" help="Each stack's environment of that name is the model.">
              <NativeSelect value={ref} onChange={(e) => setReference(e.target.value)}>
                {names.map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </NativeSelect>
            </Field>
            <p className="text-[12px] text-muted">
              {withRef.length} stack{withRef.length === 1 ? "" : "s"}:{" "}
              <span className="font-data">{withRef.map((e) => e.stack.name).join(", ")}</span>
              {without.length > 0 && (
                <>
                  {" "}
                  · not in <span className="font-data">{without.join(", ")}</span> (no{" "}
                  <span className="font-data">{ref}</span> there)
                </>
              )}
            </p>
            <Field label="New environment name">
              <Input
                className="font-data"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="prod-euw3"
                required
                autoFocus
              />
            </Field>
            <div className="flex flex-wrap gap-3">
              <Field label="Tier" help="Empty: the model's.">
                <NativeSelect value={tier} onChange={(e) => setTier(e.target.value)}>
                  <option value="">same as the model</option>
                  {tiers.map((t) => (
                    <option key={t.id} value={t.name}>
                      {t.name}
                    </option>
                  ))}
                </NativeSelect>
              </Field>
              <Field label="Branch" help="Empty: the model's.">
                <Input
                  className="font-data w-36"
                  value={branch}
                  onChange={(e) => setBranch(e.target.value)}
                />
              </Field>
            </div>
            <Field
              label="Labels"
              help="key=value, comma-separated; merged onto the model's. A variable set selecting them attaches by itself (e.g. region=eu-west-3). They also route runs: only workers carrying every label take them."
            >
              <Input
                className="font-data"
                value={labels}
                onChange={(e) => setLabels(e.target.value)}
                placeholder="region=eu-west-3"
              />
            </Field>
            {isAdmin && (
              <label className="flex items-center gap-2 text-ui">
                <Checkbox checked={copyCloud} onChange={(e) => setCopyCloud(e.target.checked)} />
                Copy the cloud integrations (the same IAM roles)
              </label>
            )}
            {previewM.error && (
              <Banner tone="failed" role="alert" title="Nothing can be created yet">
                {errorText(previewM.error)}
              </Banner>
            )}
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={() => reset(false)}>
                Cancel
              </Button>
              <Button type="submit" variant="primary" loading={previewM.isPending}>
                Review
              </Button>
            </div>
          </form>
        ) : (
          <div className="flex flex-col gap-3 text-ui">
            <section aria-label="Environments">
              <h3 className="mb-1 text-[12px] font-semibold uppercase text-muted">
                {done ? "Created" : "Will create"}
              </h3>
              <ul className="font-data text-[12px]">
                {shown.environments.map((e) =>
                  done ? (
                    <li key={e.id}>
                      <Link
                        className="link"
                        to={`${spaceBase}/stacks/${encodeURIComponent(e.stack_name)}/envs/${encodeURIComponent(e.name)}`}
                      >
                        {e.stack_name}/{e.name}
                      </Link>{" "}
                      <span className="text-muted">· {e.tier}</span>
                    </li>
                  ) : (
                    <li key={e.id}>
                      {e.stack_name}/{e.name} <span className="text-muted">· {e.tier}</span>
                    </li>
                  ),
                )}
              </ul>
              <p className="mt-1 text-[12px] text-muted">
                With the model&apos;s settings, platform hooks, variable-set attachments, notification targets
                and non-secret variables. Never copied: secret values, mock or fallback apply permissions.
              </p>
            </section>
            {shown.edges.length > 0 && (
              <section aria-label="Dependencies">
                <h3 className="mb-1 text-[12px] font-semibold uppercase text-muted">Dependencies</h3>
                <ul className="font-data text-[12px]">
                  {shown.edges.map((d) => (
                    <li key={`${d.upstream_env_id}-${d.downstream_env_id}`}>
                      {envName(d.upstream_env_id)} → {envName(d.downstream_env_id)}
                      {d.shared_upstream === "true" && <span className="text-muted"> · shared upstream</span>}
                    </li>
                  ))}
                </ul>
              </section>
            )}
            {(shown.missing_secrets.length > 0 || shown.to_configure.length > 0) && (
              <section aria-label="Left to fill in">
                <h3 className="mb-1 text-[12px] font-semibold uppercase text-muted">Left to fill in</h3>
                <ul className="flex flex-col gap-0.5 text-[12px]">
                  {shown.missing_secrets.map((m) => (
                    <li key={`${m.stack}-${m.name}`}>
                      <span aria-hidden>☐ </span>
                      secret <span className="font-data">{m.name}</span> in{" "}
                      {done ? (
                        <Link
                          className="link font-data"
                          to={`${spaceBase}/stacks/${encodeURIComponent(m.stack)}/envs/${encodeURIComponent(m.environment)}?tab=inputs`}
                        >
                          {m.stack}/{m.environment}
                        </Link>
                      ) : (
                        <span className="font-data">
                          {m.stack}/{m.environment}
                        </span>
                      )}
                    </li>
                  ))}
                  {shown.to_configure.map((m) => (
                    <li key={`${m.stack}-${m.item}`}>
                      <span aria-hidden>☐ </span>
                      {m.item} in{" "}
                      <span className="font-data">
                        {m.stack}/{m.environment}
                      </span>
                    </li>
                  ))}
                </ul>
              </section>
            )}
            {createM.error && (
              <Banner tone="failed" role="alert" title="Nothing was created">
                {errorText(createM.error)}
              </Banner>
            )}
            <div className="flex justify-end gap-2">
              {done ? (
                <>
                  <Button variant="ghost" onClick={() => reset(false)}>
                    Close
                  </Button>
                  {onRollOut && (
                    <Button
                      variant="primary"
                      onClick={() => {
                        reset(false);
                        onRollOut(done.environments[0]?.name ?? name);
                      }}
                    >
                      Roll out {done.environments[0]?.name ?? name}…
                    </Button>
                  )}
                </>
              ) : (
                <>
                  <Button variant="ghost" onClick={() => setPreview(null)}>
                    Back
                  </Button>
                  <Button variant="primary" loading={createM.isPending} onClick={() => createM.mutate()}>
                    Create {shown.environments.length} environment{shown.environments.length === 1 ? "" : "s"}
                  </Button>
                </>
              )}
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
