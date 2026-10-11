import { useMemo, type ReactNode } from "react";
import { ChevronRight } from "lucide-react";
import { groupByAction, parsePlan, type PlanAction, type PlanResource } from "@/lib/planParse";
import type { PlanCounts } from "@/lib/runOutcome";
import { Banner, EmptyState, Input } from "@/ui/primitives";
import { cn } from "@/ui/cn";

// The Plan tab (DESIGN §5.2). Destroys and replaces come first and can never be collapsed, filtered
// out or hidden by search — search only highlights in them. Anything the parser did not recognise is
// shown verbatim above the groups (planParse never drops a line).

const ACTION: Record<PlanAction, { label: string; glyph: string; text: string }> = {
  destroy: { label: "DESTROY", glyph: "−", text: "text-df-destroy" },
  replace: { label: "REPLACE", glyph: "±", text: "text-df-replace" },
  create: { label: "CREATE", glyph: "+", text: "text-df-create" },
  update: { label: "UPDATE", glyph: "~", text: "text-df-update" },
  read: { label: "READ", glyph: "<=", text: "text-df-read" },
  move: { label: "MOVE", glyph: "→", text: "text-df-read" },
  other: { label: "OTHER", glyph: "·", text: "text-df-read" },
};

const ALWAYS_OPEN: PlanAction[] = ["destroy", "replace"];

/** Big mono summary; zeros are neutral so the eye lands on what changes (DESIGN §3.5). */
export function PlanCountsBig({ counts, className }: { counts: PlanCounts; className?: string }) {
  const part = (n: number, glyph: string, cls: string, word: string) => (
    <span className={n ? cls : "text-muted"} aria-label={`${n} to ${word}`}>
      {glyph}
      {n}
    </span>
  );
  return (
    <span className={cn("inline-flex gap-3 font-data text-[20px] leading-7 font-medium", className)}>
      {part(counts.add, "+", "text-df-create", "add")}
      {part(counts.change, "~", "text-df-update", "change")}
      {part(counts.destroy, "−", "text-df-destroy", "destroy")}
    </span>
  );
}

function lineClass(line: string): string {
  const t = line.trimStart();
  if (t.startsWith("-/+") || t.startsWith("+/-")) return "text-df-replace";
  if (t.startsWith("+")) return "text-df-create";
  if (t.startsWith("-")) return "text-df-destroy";
  if (t.startsWith("~")) return "text-df-update";
  if (t.startsWith("#")) return "text-muted";
  return "text-primary";
}

function Highlight({ text, query }: { text: string; query: string }) {
  if (!query) return <>{text}</>;
  const i = text.toLowerCase().indexOf(query.toLowerCase());
  if (i < 0) return <>{text}</>;
  return (
    <>
      {text.slice(0, i)}
      <mark className="rounded-[1px] bg-raised text-primary outline outline-1 outline-ring">
        {text.slice(i, i + query.length)}
      </mark>
      {text.slice(i + query.length)}
    </>
  );
}

function Body({ lines, query }: { lines: string[]; query: string }) {
  return (
    <pre className="overflow-x-auto rounded-base bg-sunken px-3 py-2 font-data text-[12px] leading-[18px]">
      {lines.map((l, i) => (
        <div key={i} className={lineClass(l)}>
          <Highlight text={l || " "} query={query} />
        </div>
      ))}
    </pre>
  );
}

function matches(r: PlanResource, q: string): boolean {
  if (!q) return true;
  const s = q.toLowerCase();
  return r.address.toLowerCase().includes(s) || r.changedAttributes.some((a) => a.toLowerCase().includes(s));
}

function hint(r: PlanResource): string {
  if (r.forcesReplacement.length) return `forces replacement: ${r.forcesReplacement.join(", ")}`;
  if (r.notes.length) return r.notes[0].replace(/^# \(|\)$/g, "");
  if (r.changedAttributes.length)
    return `${r.changedAttributes.length} attribute${r.changedAttributes.length > 1 ? "s" : ""}`;
  return r.phrase;
}

function ResourceRow({
  r,
  query,
  open,
  action,
}: {
  r: PlanResource;
  query: string;
  open: boolean;
  action?: ReactNode;
}) {
  const a = ACTION[r.action];
  const header = (
    <span className="flex min-w-0 items-baseline gap-2">
      <span aria-hidden className={cn("w-4 shrink-0 font-data", a.text)}>
        {a.glyph}
      </span>
      <span className="min-w-0 font-data break-all">
        <Highlight text={r.address} query={query} />
      </span>
      <span className="truncate text-[12px] text-muted">{hint(r)}</span>
      <span className="sr-only">{a.label.toLowerCase()}</span>
    </span>
  );
  if (open) {
    return (
      <li className="flex flex-col gap-1.5 py-1.5">
        <div className="flex items-start justify-between gap-2">
          {header}
          {action}
        </div>
        {r.notes.length > 0 && (
          <div className="pl-6 font-data text-[12px] text-muted">{r.notes.join(" ")}</div>
        )}
        {r.body.length > 0 && <Body lines={r.body} query={query} />}
      </li>
    );
  }
  // The action sits beside the <summary>, not inside it: a control nested in a disclosure toggle is
  // unreachable as itself for assistive technology (WCAG 4.1.2).
  return (
    <li className="relative py-0.5">
      <details className="group">
        <summary
          className={cn(
            "flex min-h-7 cursor-pointer list-none items-start gap-2 rounded-base px-1 py-1 hover:bg-raised",
            !!action && "pr-10",
          )}
        >
          <span className="flex min-w-0 items-baseline gap-1">
            <ChevronRight
              size={14}
              strokeWidth={1.5}
              aria-hidden
              className="shrink-0 self-center text-muted group-open:rotate-90"
            />
            {header}
          </span>
        </summary>
        <div className="mt-1 flex flex-col gap-1.5 pl-6">
          {r.notes.length > 0 && <div className="font-data text-[12px] text-muted">{r.notes.join(" ")}</div>}
          <Body lines={r.body} query={query} />
        </div>
      </details>
      {action && <span className="absolute top-1 right-1">{action}</span>}
    </li>
  );
}

export interface PlanDiffProps {
  /** Plan text lines (the `plan` log section) — null when the run has none. */
  lines: string[] | null;
  counts: PlanCounts | null;
  query: string;
  onQueryChange: (q: string) => void;
  /** Per-resource action slot (e.g. "comment on this resource"). */
  resourceAction?: (r: PlanResource) => ReactNode;
  /** Shown when there is no plan text (link to the logs). */
  emptyHint?: ReactNode;
}

export function PlanDiff({ lines, counts, query, onQueryChange, resourceAction, emptyHint }: PlanDiffProps) {
  const plan = useMemo(() => (lines && lines.length ? parsePlan(lines) : null), [lines]);
  const groups = useMemo(() => (plan ? groupByAction(plan) : []), [plan]);
  const tagOnly = plan?.resources.filter((r) => r.tagOnly) ?? [];
  const q = query.trim();

  if (!plan) {
    return (
      <div className="flex flex-col gap-3">
        {counts && <PlanCountsBig counts={counts} />}
        <EmptyState title="No plan output for this run">{emptyHint}</EmptyState>
      </div>
    );
  }

  const visible = (action: PlanAction, rs: PlanResource[]) =>
    ALWAYS_OPEN.includes(action) ? rs : rs.filter((r) => matches(r, q));
  const matchCount = plan.resources.filter((r) => matches(r, q)).length;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        {counts ? (
          <PlanCountsBig counts={counts} />
        ) : (
          <span className="font-data text-muted">{plan.summary}</span>
        )}
        <label className="flex items-center gap-2">
          <span className="sr-only">Search the plan</span>
          <Input
            type="search"
            value={query}
            onChange={(e) => onQueryChange(e.target.value)}
            placeholder="Search address or attribute"
            className="w-64"
          />
          {q && (
            <span className="font-data text-[12px] text-muted" aria-live="polite">
              {matchCount} match{matchCount === 1 ? "" : "es"}
            </span>
          )}
        </label>
      </div>

      {plan.unparsed.length > 0 && (
        <Banner tone="info" title="Plan output the parser did not recognise — shown as printed">
          <Body lines={plan.unparsed} query={q} />
        </Banner>
      )}

      {groups.map(({ action, resources }) => {
        const a = ACTION[action];
        const shown = visible(action, resources);
        const open = ALWAYS_OPEN.includes(action);
        return (
          <section key={action} aria-label={`${a.label.toLowerCase()} ${resources.length}`}>
            <h3 className="eyebrow mb-1 flex items-center gap-2 border-b border-line pb-1">
              <span aria-hidden className={cn("font-data text-[13px]", a.text)}>
                {a.glyph}
              </span>
              <span className={a.text}>{a.label}</span>
              <span className="text-muted">{resources.length}</span>
              {open && (
                <span className="ml-auto text-muted normal-case tracking-normal">always expanded</span>
              )}
              {!open && q && shown.length !== resources.length && (
                <span className="ml-auto text-muted normal-case tracking-normal">
                  {shown.length} of {resources.length} match
                </span>
              )}
            </h3>
            <ul>
              {shown.map((r) => (
                <ResourceRow
                  key={`${r.address}:${r.line}`}
                  r={r}
                  query={q}
                  open={open}
                  action={resourceAction?.(r)}
                />
              ))}
            </ul>
          </section>
        );
      })}

      {tagOnly.length > 0 && (
        <details className="rounded-base border border-line px-3 py-2">
          <summary className="min-h-6 cursor-pointer py-1 eyebrow text-muted">
            ~ Tag-only updates · {tagOnly.length} (collapsed, counted)
          </summary>
          <ul className="mt-2">
            {tagOnly
              .filter((r) => matches(r, q))
              .map((r) => (
                <ResourceRow
                  key={`${r.address}:${r.line}`}
                  r={r}
                  query={q}
                  open={false}
                  action={resourceAction?.(r)}
                />
              ))}
          </ul>
        </details>
      )}

      {plan.outputs.length > 0 && (
        <details className="rounded-base border border-line px-3 py-2">
          <summary className="min-h-6 cursor-pointer py-1 eyebrow text-muted">Changes to outputs</summary>
          <div className="mt-2">
            <Body lines={plan.outputs.slice(1)} query={q} />
          </div>
        </details>
      )}

      {plan.summary && <div className="font-data text-[12px] text-muted">{plan.summary}</div>}

      {plan.preamble.some((l) => l.trim()) && (
        <details>
          <summary className="min-h-6 cursor-pointer py-1 text-[12px] text-muted">
            Plan legend (as printed by the tool)
          </summary>
          <Body lines={plan.preamble.filter((l) => l.trim())} query="" />
        </details>
      )}
    </div>
  );
}
