import { useState, type ReactNode } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { observability, stacks, tiers } from "@/api/resources";
import type { TierDef } from "@/api/types";
import { useKeyboardSettings } from "@/app/keyboard";
import { useTheme, type Theme } from "@/app/theme";
import { useIsAdmin } from "@/auth/session";
import { Button } from "@/ui/Button";
import { Badge, Banner, Checkbox, Field, Input, Skeleton } from "@/ui/primitives";
import { TypeNameDialog } from "@/ui/TypeNameDialog";
import { SpaceMembers, Users } from "./access";

// Settings (DESIGN §5.7): personal preferences for everyone (kept in memory, no browser storage),
// then — for admins — the tier catalogue and users. Weakening a tier's safety or deleting it asks
// for the typed name; user edits are staged and saved with a summary.

function Section({
  id,
  title,
  help,
  children,
}: {
  id: string;
  title: string;
  help?: string;
  children: ReactNode;
}) {
  return (
    <section aria-labelledby={id} className="flex flex-col gap-2">
      <h2 id={id} className="text-section font-semibold">
        {title}
      </h2>
      {help && <p className="max-w-[720px] text-[12px] text-muted">{help}</p>}
      {children}
    </section>
  );
}

function Preferences() {
  const { theme, overridden, setOverride } = useTheme();
  const keys = useKeyboardSettings();
  const choice: Theme | "system" = overridden ? theme : "system";
  return (
    <Section
      id="prefs"
      title="Preferences"
      help="For this tab only: Stackd keeps no preference in your browser, so a reload returns to the defaults."
    >
      <div className="flex max-w-[560px] flex-col gap-3">
        <fieldset className="flex flex-col gap-1">
          <legend className="eyebrow mb-1 text-muted">Theme</legend>
          <div className="flex flex-wrap gap-4">
            {(["system", "dark", "light"] as const).map((t) => (
              <label key={t} className="flex items-center gap-2 text-ui">
                <input
                  type="radio"
                  name="theme"
                  className="size-4 accent-ring"
                  checked={choice === t}
                  onChange={() => setOverride(t === "system" ? null : t)}
                />
                {t === "system" ? "Follow the system" : t === "dark" ? "Dark" : "Light"}
              </label>
            ))}
          </div>
        </fieldset>
        <label className="flex items-center gap-2 text-ui">
          <Checkbox checked={keys.enabled} onChange={(e) => keys.setEnabled(e.target.checked)} />
          <span>
            Single-key shortcuts (<span className="font-data">c</span>, <span className="font-data">g o</span>
            , <span className="font-data">?</span>…) — <span className="font-data">⌘K</span> always works
          </span>
        </label>
      </div>
    </Section>
  );
}

function TierRow({
  tier,
  usedBy,
  onMove,
}: {
  tier: TierDef;
  /** Environments classified in this tier, null while loading. */
  usedBy: number | null;
  onMove: { up?: () => void; down?: () => void };
}) {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: ["tiers"] });
  const require = useMutation({
    mutationFn: () => tiers.update(tier.id, { requires_four_eyes: true }),
    onSuccess: invalidate,
  });
  return (
    <li className="flex flex-wrap items-center gap-3 rounded-base border border-line bg-surface px-3 py-2">
      <span className="min-w-[96px] font-data font-semibold">{tier.name}</span>
      {tier.requires_four_eyes ? (
        <Badge glyph="◉◉">four-eyes</Badge>
      ) : (
        <span className="font-data text-[12px] text-muted">single confirmer</span>
      )}
      {usedBy !== null && (
        <span className="font-data text-[12px] text-muted">
          · {usedBy} environment{usedBy === 1 ? "" : "s"}
        </span>
      )}
      <span className="ml-auto flex flex-wrap gap-2">
        <Button
          variant="ghost"
          size="sm"
          disabled={!onMove.up}
          onClick={onMove.up}
          aria-label={`Move ${tier.name} up`}
          title="Move up (display order only)"
        >
          ↑
        </Button>
        <Button
          variant="ghost"
          size="sm"
          disabled={!onMove.down}
          onClick={onMove.down}
          aria-label={`Move ${tier.name} down`}
          title="Move down (display order only)"
        >
          ↓
        </Button>
        {tier.requires_four_eyes ? (
          <TypeNameDialog
            trigger={
              <Button variant="ghost" size="sm">
                Turn off four-eyes…
              </Button>
            }
            title={`Turn off four-eyes on ${tier.name}?`}
            consequence={`Members who trigger a ${tier.name} run will be able to confirm it themselves. Environments marked protected keep their own four-eyes rule.`}
            name={tier.name}
            confirmLabel="Turn off four-eyes"
            variant="decision"
            onConfirm={() => tiers.update(tier.id, { requires_four_eyes: false }).then(invalidate)}
          />
        ) : (
          <Button variant="ghost" size="sm" loading={require.isPending} onClick={() => require.mutate()}>
            Require four-eyes
          </Button>
        )}
        <TypeNameDialog
          trigger={
            <Button variant="ghost" size="sm">
              Delete…
            </Button>
          }
          title={`Delete the ${tier.name} tier`}
          consequence={
            usedBy
              ? `${usedBy} environment${usedBy === 1 ? " is" : "s are"} in this tier: the API refuses until they move to another tier.`
              : "Users who hold this tier lose it."
          }
          name={tier.name}
          confirmLabel={`Delete ${tier.name}`}
          onConfirm={() => tiers.remove(tier.id).then(invalidate)}
        />
      </span>
      {require.isError && (
        <div className="basis-full">
          <Banner tone="failed" role="alert" title="The API refused">
            {(require.error as Error).message}
          </Banner>
        </div>
      )}
    </li>
  );
}

function Tiers({ catalog, loading }: { catalog: TierDef[]; loading: boolean }) {
  const qc = useQueryClient();
  const stackList = useQuery({ queryKey: ["stacks"], queryFn: stacks.list });
  const envQueries = useQueries({
    queries: (stackList.data ?? []).map((st) => ({
      queryKey: ["environments", st.id],
      queryFn: () => stacks.environments(st.id),
    })),
  });
  const envsLoaded = !!stackList.data && envQueries.every((q) => q.data);
  const envs = envQueries.flatMap((q) => q.data ?? []);
  const ordered = [...catalog].sort((a, b) => a.position - b.position);
  // Swap display positions with the neighbour; tiers have no rank (CLAUDE §7), only an order on screen.
  const move = useMutation({
    mutationFn: async ([a, b]: [TierDef, TierDef]) => {
      await tiers.update(a.id, { position: b.position });
      await tiers.update(b.id, { position: a.position });
    },
    onSettled: () => qc.invalidateQueries({ queryKey: ["tiers"] }),
  });
  const [name, setName] = useState("");
  const [fourEyes, setFourEyes] = useState(true);
  const create = useMutation({
    mutationFn: () => tiers.create({ name, requires_four_eyes: fourEyes, position: catalog.length }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tiers"] });
      setName("");
    },
  });
  return (
    <Section
      id="tiers"
      title="Tiers"
      help="Tiers classify environments. A user may confirm an apply on a tier only if it is in their allowed tiers — any set, not a ladder. A four-eyes tier needs a second person to confirm a manual run."
    >
      {loading ? (
        <Skeleton className="h-24 w-full max-w-[720px]" />
      ) : (
        <ul className="flex max-w-[720px] flex-col gap-2">
          {ordered.map((t, i) => (
            <TierRow
              key={t.id}
              tier={t}
              usedBy={envsLoaded ? envs.filter((e) => e.tier === t.name).length : null}
              onMove={{
                up: i > 0 && !move.isPending ? () => move.mutate([t, ordered[i - 1]]) : undefined,
                down:
                  i < ordered.length - 1 && !move.isPending
                    ? () => move.mutate([t, ordered[i + 1]])
                    : undefined,
              }}
            />
          ))}
        </ul>
      )}
      <form
        className="flex max-w-[720px] flex-wrap items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <Field label="New tier">
          <Input
            value={name}
            onChange={(e) => setName(e.target.value.trim())}
            placeholder="qa"
            spellCheck={false}
            required
          />
        </Field>
        <label className="flex h-8 items-center gap-2 text-ui">
          <Checkbox checked={fourEyes} onChange={(e) => setFourEyes(e.target.checked)} />
          Require four-eyes
        </label>
        <Button type="submit" disabled={!name} loading={create.isPending}>
          Add tier
        </Button>
      </form>
      {create.isError && (
        <Banner tone="failed" role="alert" title="The API refused">
          {(create.error as Error).message}
        </Banner>
      )}
    </Section>
  );
}

function Deployment() {
  const { data } = useQuery({ queryKey: ["health"], queryFn: observability.health });
  const rows: [string, string | undefined][] = [
    ["Environment", data?.env],
    ["Version", data?.version],
    ["Database", data?.checks.database],
  ];
  return (
    <Section id="deployment" title="Deployment">
      <dl className="grid max-w-[560px] grid-cols-[max-content_1fr] gap-x-6 gap-y-1">
        {rows.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="eyebrow self-center text-muted">{k}</dt>
            <dd className="font-data text-[12px]">{v ?? "—"}</dd>
          </div>
        ))}
      </dl>
    </Section>
  );
}

export function SettingsPage() {
  const isAdmin = useIsAdmin();
  const catalog = useQuery({ queryKey: ["tiers"], queryFn: tiers.list, enabled: isAdmin });
  const tierNames = [...(catalog.data ?? [])].sort((a, b) => a.position - b.position).map((t) => t.name);
  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-title font-semibold">Settings</h1>
      <Preferences />
      {isAdmin ? (
        <>
          <Tiers catalog={catalog.data ?? []} loading={catalog.isLoading} />
          <Users tierNames={tierNames} />
          <SpaceMembers tierNames={tierNames} />
        </>
      ) : (
        <p className="text-muted">Tiers and users are managed by administrators.</p>
      )}
      <Deployment />
    </div>
  );
}
