import { Crosshair, RefreshCcw } from "lucide-react";
import type { PlanOptions } from "@/api/types";
import { Badge, Field } from "@/ui/primitives";

// Targeted / replace plans (SPECS §3.5): resource addresses passed as `-target=` / `-replace=` to the
// plan only. Such a run never auto-confirms, and a targeted apply is partial (it clears no drift and
// is no promotion source) — the copy below says so where the addresses are entered.

/** One address per line: trimmed, blank lines dropped, duplicates removed (first kept). */
export function parseAddresses(text: string): string[] {
  return [
    ...new Set(
      text
        .split("\n")
        .map((l) => l.trim())
        .filter(Boolean),
    ),
  ];
}

/** The plan options for an entry, or null when both lists are empty (a full plan). */
export function toPlanOptions(targets: string, replace: string): PlanOptions | null {
  const t = parseAddresses(targets);
  const r = parseAddresses(replace);
  if (!t.length && !r.length) return null;
  return { ...(t.length ? { targets: t } : {}), ...(r.length ? { replace: r } : {}) };
}

const TEXTAREA =
  "min-h-16 w-full rounded-base border border-control bg-base px-2 py-1.5 font-data text-[12px] text-primary placeholder:text-muted";

/** "Advanced options" disclosure of the trigger-run UI. `allowReplace=false` for a destroy run. */
export function PlanOptionsFields({
  targets,
  replace,
  onTargets,
  onReplace,
  allowReplace = true,
  defaultOpen = false,
}: {
  targets: string;
  replace: string;
  onTargets: (v: string) => void;
  onReplace: (v: string) => void;
  allowReplace?: boolean;
  defaultOpen?: boolean;
}) {
  return (
    <details className="rounded-base border border-line px-3 py-2" open={defaultOpen || undefined}>
      <summary className="min-h-6 cursor-pointer py-1 eyebrow text-muted">Advanced options</summary>
      <div className="mt-2 flex flex-col gap-3">
        <Field
          label="Target addresses (-target)"
          help="One resource address per line. A targeted plan changes only these (and what they depend on); its apply is partial: it does not clear drift and is not a promotion source."
        >
          <textarea
            value={targets}
            rows={3}
            spellCheck={false}
            placeholder={'aws_s3_bucket.logs\nmodule.net["eu"].aws_subnet.a[0]'}
            onChange={(e) => onTargets(e.target.value)}
            className={TEXTAREA}
          />
        </Field>
        {allowReplace && (
          <Field label="Replace addresses (-replace)" help="One address per line: destroyed and re-created.">
            <textarea
              value={replace}
              rows={2}
              spellCheck={false}
              placeholder="aws_instance.web"
              onChange={(e) => onReplace(e.target.value)}
              className={TEXTAREA}
            />
          </Field>
        )}
        <p className="text-[12px] text-muted">
          A run with options always stops for a human confirmation, even on an autodeploy environment.
        </p>
      </div>
    </details>
  );
}

/** "Targeted" / "Replace" chips of a run, with the addresses (label + icon, never colour alone). */
export function PlanOptionsChips({ options }: { options: PlanOptions | null | undefined }) {
  if (!options) return null;
  const groups = [
    { key: "targets", label: "Targeted", icon: <Crosshair size={12} />, addrs: options.targets ?? [] },
    { key: "replace", label: "Replace", icon: <RefreshCcw size={12} />, addrs: options.replace ?? [] },
  ].filter((g) => g.addrs.length > 0);
  if (!groups.length) return null;
  return (
    <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
      {groups.map((g) => (
        <span key={g.key} className="flex flex-wrap items-center gap-1" aria-label={`${g.label} plan`}>
          <Badge tone="decision" glyph={g.icon} title={`-${g.key === "targets" ? "target" : "replace"}=…`}>
            {g.label}
          </Badge>
          {g.addrs.map((a) => (
            <span key={a} className="font-data text-[12px] text-primary">
              {a}
            </span>
          ))}
        </span>
      ))}
    </span>
  );
}
