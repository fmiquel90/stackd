import { useEffect, useMemo, useState } from "react";
import { keepPreviousData, useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Copy } from "lucide-react";
import { ApiError } from "@/api/client";
import { environmentClones, stacks, type CloneRequest, type CloneResult } from "@/api/resources";
import type { TierDef } from "@/api/types";
import { Button } from "@/ui/Button";
import { Dialog, DialogContent, DialogTrigger } from "@/ui/overlays";
import { Banner, Checkbox, Field, Input, NativeSelect } from "@/ui/primitives";

// "New environment from…" (DESIGN §5.11, SPECS §21.2): declare one environment across the space's
// stacks, modelled on a reference environment. Form (with a live dry-run hint) → preview (a dry run
// of the real request) → create → what is left to fill.

/** What the dialog needs of an environment of the space (an overview `EnvEntry` fits). */
export interface CloneEntry {
  stack: { id: string; name: string };
  env: { id: string; name: string };
}

export interface ParsedLabels {
  labels: Record<string, string> | undefined;
  /** One message per malformed pair; non-empty blocks the submit. */
  errors: string[];
}

/** `key=value` pairs, comma-separated. The value is everything after the first `=` (it may hold
 * more). Malformed pairs are reported, never dropped. */
export function parseLabels(text: string): ParsedLabels {
  const labels: Record<string, string> = {};
  const errors: string[] = [];
  for (const raw of text.split(",")) {
    const pair = raw.trim();
    if (!pair) continue;
    const at = pair.indexOf("=");
    const key = at < 0 ? "" : pair.slice(0, at).trim();
    const value = at < 0 ? "" : pair.slice(at + 1).trim();
    if (at < 0) errors.push(`"${pair}" is not key=value`);
    else if (!key) errors.push(`"${pair}" has no key`);
    else if (!value) errors.push(`"${pair}" has no value`);
    else if (key in labels) errors.push(`"${key}" is set twice`);
    else labels[key] = value;
  }
  return { labels: Object.keys(labels).length ? labels : undefined, errors };
}

function errorText(e: unknown): string {
  return e instanceof ApiError ? (e.detail ?? e.title) : String(e);
}

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

/** Every environment of the space (stacks → environments), for callers without the overview data. */
export function useCloneEntries(spaceId: string): CloneEntry[] {
  const stackList = useQuery({ queryKey: ["stacks"], queryFn: stacks.list });
  const spaceStacks = (stackList.data ?? []).filter((s) => s.space_id === spaceId);
  const envQueries = useQueries({
    queries: spaceStacks.map((s) => ({
      queryKey: ["environments", s.id],
      queryFn: () => stacks.environments(s.id),
    })),
  });
  return spaceStacks.flatMap((s, i) => (envQueries[i]?.data ?? []).map((env) => ({ stack: s, env })));
}

const OFF_REASON: Record<string, string> = {
  protected: "protected",
  left_to_fill: "secrets or settings left to fill",
};

function where(x: { stack: string; environment: string }) {
  return (
    <span className="font-data">
      {x.stack}/{x.environment}
    </span>
  );
}

/** Label sets, shadowed items and forced-off autodeploy of a (dry-run) result — the live hint under
 * the labels and part of the review. */
export function CloneNotes({ result }: { result: CloneResult }) {
  const sets = new Map<string, { isNew: boolean; stacks: string[] }>();
  for (const e of result.environments)
    for (const s of e.label_sets) {
      const cur = sets.get(s.name) ?? { isNew: s.new, stacks: [] };
      cur.isNew ||= s.new;
      cur.stacks.push(e.stack_name);
      sets.set(s.name, cur);
    }
  const off = result.environments.filter((e) => e.autodeploy_off_reason);
  return (
    <div className="flex flex-col gap-1 text-[12px]">
      <p>
        <span className="text-muted">Labels attach: </span>
        {sets.size === 0 ? (
          <span className="text-muted">no variable set.</span>
        ) : (
          [...sets].map(([name, s], i) => (
            <span key={name}>
              {i > 0 && ", "}
              <span className="font-data">{name}</span>
              {s.isNew && <span className="text-muted"> (new: the model does not get it)</span>}
              {s.stacks.length < result.environments.length && (
                <span className="text-muted"> in {s.stacks.join(", ")}</span>
              )}
            </span>
          ))
        )}
      </p>
      {result.not_copied.length > 0 && (
        <div role="note">
          <span className="font-semibold">Not copied, so these sets win: </span>
          <ul className="ml-4 list-disc">
            {result.not_copied.map((n) => (
              <li key={`${n.stack}-${n.item}-${n.name}`}>
                {n.item === "variable" ? "variable" : "attachment of the set"}{" "}
                <span className="font-data">{n.name}</span> in {where(n)} — it would shadow{" "}
                <span className="font-data">{n.keys.join(", ")}</span> of{" "}
                <span className="font-data">{n.label_sets.join(", ")}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {off.length > 0 && (
        <ul className="flex flex-col gap-0.5">
          {off.map((e) => (
            <li key={e.id}>
              Autodeploy off in{" "}
              <span className="font-data">
                {e.stack_name}/{e.name}
              </span>
              : {OFF_REASON[e.autodeploy_off_reason ?? ""] ?? e.autodeploy_off_reason}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function NewEnvironmentDialog({
  spaceId,
  spaceName,
  spaceBase,
  entries,
  tiers,
  isAdmin,
  onRollOut,
  defaultReference,
  open: openProp,
  onOpenChange,
}: {
  spaceId: string;
  spaceName: string;
  /** `/s/<space>`: base of the env links. */
  spaceBase: string;
  entries: CloneEntry[];
  tiers: TierDef[];
  isAdmin: boolean;
  /** Offered once created (lot 3): start a rollout of the new environment. */
  onRollOut?: (envName: string) => void;
  /** Preselected model (the env page's own env). */
  defaultReference?: string;
  /** Controlled (no trigger button) when given — the env page opens it from its "More" menu. */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}) {
  const qc = useQueryClient();
  const [ownOpen, setOwnOpen] = useState(false);
  const controlled = openProp !== undefined;
  const open = controlled ? openProp : ownOpen;
  const names = useMemo(() => [...new Set(entries.map((e) => e.env.name))].sort(), [entries]);
  const [reference, setReference] = useState(defaultReference ?? "");
  const [name, setName] = useState("");
  const [tier, setTier] = useState("");
  const [branch, setBranch] = useState("");
  const [labels, setLabels] = useState("");
  const [copyCloud, setCopyCloud] = useState(false);
  const [excluded, setExcluded] = useState<Set<string>>(new Set());
  const [preview, setPreview] = useState<CloneResult | null>(null);
  const [done, setDone] = useState<CloneResult | null>(null);

  const ref = reference || names[0] || "";
  const withRef = entries
    .filter((e) => e.env.name === ref)
    .sort((a, b) => a.stack.name.localeCompare(b.stack.name));
  const without = [...new Set(entries.map((e) => e.stack.name))]
    .filter((s) => !withRef.some((e) => e.stack.name === s))
    .sort();
  const selected = withRef.filter((e) => !excluded.has(e.stack.id));
  const parsed = parseLabels(labels);
  const envName = (id: string) => {
    const e = entries.find((x) => x.env.id === id);
    if (e) return `${e.stack.name}/${e.env.name}`;
    const c = (preview ?? done)?.environments.find((x) => x.id === id);
    return c ? `${c.stack_name}/${c.name}` : id;
  };

  const body: CloneRequest = {
    reference_name: ref,
    name: name.trim(),
    // Only an explicit subset is sent; the default is every stack with the reference.
    ...(selected.length < withRef.length ? { stack_ids: selected.map((e) => e.stack.id) } : {}),
    overrides: {
      ...(tier ? { tier } : {}),
      ...(branch.trim() ? { branch: branch.trim() } : {}),
      ...(parsed.labels ? { labels: parsed.labels } : {}),
    },
    copy_cloud_integration: copyCloud,
  };
  const ready = !!ref && !!body.name && selected.length > 0 && parsed.errors.length === 0;

  // Live hint (DESIGN §5.11): a debounced dry run of the request as typed.
  const hintBody = useDebounced(JSON.stringify(body), 400);
  const hint = useQuery({
    queryKey: ["clone-preview", spaceId, hintBody],
    queryFn: () =>
      environmentClones.create(spaceId, { ...(JSON.parse(hintBody) as CloneRequest), dry_run: true }),
    enabled: open && ready && !preview && !done,
    retry: false,
    staleTime: 30_000,
    placeholderData: keepPreviousData,
  });

  const previewM = useMutation({
    mutationFn: () => environmentClones.create(spaceId, { ...body, dry_run: true }),
    onSuccess: setPreview,
  });
  const createM = useMutation({
    mutationFn: () => environmentClones.create(spaceId, { ...body, dry_run: false }),
    onSuccess: (r) => {
      setDone(r);
      qc.invalidateQueries({ queryKey: ["environments"] });
      qc.invalidateQueries({ queryKey: ["graph"] });
    },
  });

  const reset = (v: boolean) => {
    if (controlled) onOpenChange?.(v);
    else setOwnOpen(v);
    if (!v) {
      setPreview(null);
      setDone(null);
      previewM.reset();
      createM.reset();
      setReference(defaultReference ?? "");
      setName("");
      setTier("");
      setBranch("");
      setLabels("");
      setCopyCloud(false);
      setExcluded(new Set());
    }
  };

  const shown = done ?? preview;
  return (
    <Dialog open={open} onOpenChange={reset}>
      {!controlled && (
        <DialogTrigger asChild>
          <Button icon={<Copy size={14} aria-hidden />} disabled={names.length === 0}>
            New environment from…
          </Button>
        </DialogTrigger>
      )}
      <DialogContent
        title={done ? "Environment created" : preview ? "2 · Review" : "1 · New environment"}
        description={`Declared once in the chosen stacks of ${spaceName}, modelled on each one's reference environment.`}
      >
        {!shown ? (
          <form
            className="flex flex-col gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              if (ready) previewM.mutate();
            }}
          >
            <Field label="Model it on" help="Each stack's environment of that name is the model.">
              <NativeSelect
                value={ref}
                onChange={(e) => {
                  setReference(e.target.value);
                  setExcluded(new Set());
                }}
              >
                {names.map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </NativeSelect>
            </Field>
            <fieldset className="flex flex-col gap-1">
              <legend className="eyebrow mb-1 text-muted">Stacks</legend>
              {withRef.map((e) => (
                <label key={e.stack.id} className="flex items-center gap-2 text-ui">
                  <Checkbox
                    checked={!excluded.has(e.stack.id)}
                    onChange={(ev) => {
                      const next = new Set(excluded);
                      if (ev.target.checked) next.delete(e.stack.id);
                      else next.add(e.stack.id);
                      setExcluded(next);
                    }}
                  />
                  <span className="font-data">{e.stack.name}</span>
                </label>
              ))}
              {without.map((s) => (
                <label key={s} className="flex items-center gap-2 text-ui text-muted">
                  <Checkbox checked={false} disabled />
                  <span className="font-data">{s}</span>
                  <span>
                    — no <span className="font-data">{ref}</span> environment to model on
                  </span>
                </label>
              ))}
              {selected.length === 0 && withRef.length > 0 && (
                <p role="alert" className="text-[12px]">
                  Choose at least one stack.
                </p>
              )}
            </fieldset>
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
              help="key=value, comma-separated; merged onto the model's. A variable set selecting them attaches by itself (e.g. region=eu-west-3)."
            >
              <Input
                className="font-data"
                value={labels}
                onChange={(e) => setLabels(e.target.value)}
                placeholder="region=eu-west-3"
                aria-invalid={parsed.errors.length > 0}
                aria-describedby={parsed.errors.length > 0 ? "clone-label-errors" : undefined}
              />
            </Field>
            {parsed.errors.length > 0 && (
              <ul id="clone-label-errors" role="alert" className="-mt-2 flex flex-col text-[12px]">
                {parsed.errors.map((m) => (
                  <li key={m}>
                    <span aria-hidden>✕ </span>
                    {m}
                  </li>
                ))}
              </ul>
            )}
            <section
              aria-label="What these settings attach"
              aria-live="polite"
              className="rounded-base border border-line px-3 py-2"
            >
              {!ready ? (
                <p className="text-[12px] text-muted">
                  Name the environment to see which variable sets its labels attach.
                </p>
              ) : hint.error ? (
                <p className="text-[12px]">
                  <span className="text-muted">Preview unavailable: </span>
                  {errorText(hint.error)}
                </p>
              ) : hint.data ? (
                <CloneNotes result={hint.data} />
              ) : (
                <p className="text-[12px] text-muted">Checking…</p>
              )}
            </section>
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
              <Button type="submit" variant="primary" loading={previewM.isPending} disabled={!ready}>
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
                and non-secret variables — except what would shadow a set its labels attach. Never copied:
                secret values, mock or fallback apply permissions.
              </p>
            </section>
            <CloneNotes result={shown} />
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
                        where(m)
                      )}
                      {m.falls_back_to && (
                        <span>
                          {" "}
                          — until then it resolves to the <span className="font-data">
                            {m.falls_back_to}
                          </span>{" "}
                          value
                        </span>
                      )}
                    </li>
                  ))}
                  {shown.to_configure.map((m) => (
                    <li key={`${m.stack}-${m.item}`}>
                      <span aria-hidden>☐ </span>
                      {m.item} in {where(m)}
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
