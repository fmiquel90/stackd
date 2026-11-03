import { useMemo, useRef, type KeyboardEvent } from "react";
import { useNavigate } from "react-router-dom";
import type { Stack } from "@/api/types";
import type { EnvEntry } from "@/features/data/spaceEnvs";
import { EnvCell } from "@/identity/EnvCell";
import type { CascadeStep } from "@/lib/cascade";
import { cn } from "@/ui/cn";
import { formatCounts, planCounts } from "@/lib/runOutcome";
import { lookOf } from "@/identity/StateBadge";
import { age } from "@/lib/time";

// The stack × environment matrix (DESIGN §5.1): an ARIA grid with one roving tab stop. h/j/k/l and
// arrows move, Enter opens the env, r opens its run, s toggles the downstream preview. Rows are
// stacks, columns are environment names in tier order; a missing env is "—".

export interface Selection {
  stack: string;
  env: string;
}

export function Matrix({
  stacks,
  columns,
  byKey,
  selected,
  onSelect,
  downstream,
  envHref,
  onToggleCascade,
  fourEyesColumns,
}: {
  stacks: Stack[];
  columns: string[];
  byKey: Map<string, EnvEntry>;
  selected: Selection | null;
  onSelect: (s: Selection) => void;
  downstream: Map<string, CascadeStep>;
  envHref: (stack: string, env: string) => string;
  onToggleCascade: () => void;
  fourEyesColumns: Set<string>;
}) {
  const navigate = useNavigate();
  const grid = useRef<HTMLTableElement>(null);
  const now = useMemo(() => new Date(), []); // ages refresh with the data (each refetch re-renders)

  const pos = selected
    ? { r: stacks.findIndex((s) => s.name === selected.stack), c: columns.indexOf(selected.env) }
    : { r: 0, c: 0 };
  const r0 = Math.max(0, pos.r);
  const c0 = Math.max(0, pos.c);

  const focusCell = (r: number, c: number) => {
    const rr = Math.min(stacks.length - 1, Math.max(0, r));
    const cc = Math.min(columns.length - 1, Math.max(0, c));
    onSelect({ stack: stacks[rr].name, env: columns[cc] });
    requestAnimationFrame(() =>
      grid.current?.querySelector<HTMLElement>(`[data-cell="${rr}:${cc}"]`)?.focus(),
    );
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTableElement>) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const k = e.key;
    const moves: Record<string, [number, number]> = {
      ArrowLeft: [0, -1],
      h: [0, -1],
      ArrowRight: [0, 1],
      l: [0, 1],
      ArrowUp: [-1, 0],
      k: [-1, 0],
      ArrowDown: [1, 0],
      j: [1, 0],
    };
    // Position from the focused cell itself: the URL selection updates asynchronously, and fast key
    // repeats must not read a stale one.
    const cell = (e.target as HTMLElement)
      .closest<HTMLElement>("[data-cell]")
      ?.dataset.cell?.split(":")
      .map(Number);
    const [r, c] = cell && cell.length === 2 ? cell : [r0, c0];
    if (moves[k]) {
      e.preventDefault();
      focusCell(r + moves[k][0], c + moves[k][1]);
      return;
    }
    const entry = byKey.get(`${stacks[r]?.name}/${columns[c]}`);
    if (k === "Enter" && entry) {
      e.preventDefault();
      navigate(envHref(entry.stack.name, entry.env.name));
    } else if (k === "r" && entry?.summary.primary) {
      e.preventDefault();
      navigate(`/runs/${entry.summary.primary.id}`);
    } else if (k === "s") {
      e.preventDefault();
      onToggleCascade();
    } else if (k === "Home") {
      e.preventDefault();
      focusCell(r, 0);
    } else if (k === "End") {
      e.preventDefault();
      focusCell(r, columns.length - 1);
    }
  };

  return (
    <div className="max-w-full overflow-x-auto rounded-base border border-line bg-surface">
      <table
        ref={grid}
        role="grid"
        aria-label="Stacks by environment"
        aria-rowcount={stacks.length + 1}
        onKeyDown={onKeyDown}
        className="w-full border-collapse text-left"
      >
        <thead>
          <tr role="row">
            <th
              role="columnheader"
              scope="col"
              className="eyebrow sticky left-0 z-10 border-b border-line bg-surface px-3 py-2 font-medium text-muted"
            >
              Stack
            </th>
            {columns.map((c) => (
              <th
                key={c}
                role="columnheader"
                scope="col"
                className="eyebrow border-b border-line px-3 py-2 font-medium text-muted"
              >
                <span className="text-primary normal-case">{c}</span>
                {fourEyesColumns.has(c) && <span className="ml-2 normal-case">four-eyes</span>}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {stacks.map((s, r) => (
            <tr key={s.id} role="row" className="border-b border-line last:border-b-0">
              <th
                role="rowheader"
                scope="row"
                className="sticky left-0 z-10 bg-surface px-3 py-1 text-left font-data text-[13px] font-normal"
              >
                {s.name}
              </th>
              {columns.map((c, ci) => {
                const entry = byKey.get(`${s.name}/${c}`);
                const isSel = r === r0 && ci === c0 && !!selected;
                const step = entry ? downstream.get(entry.env.id) : undefined;
                return (
                  <td
                    key={c}
                    role="gridcell"
                    data-cell={`${r}:${ci}`}
                    tabIndex={r === r0 && ci === c0 ? 0 : -1}
                    aria-selected={isSel}
                    aria-label={entry ? cellLabel(entry) : `${s.name} has no ${c} environment`}
                    onFocus={() => (isSel ? undefined : onSelect({ stack: s.name, env: c }))}
                    onClick={() => onSelect({ stack: s.name, env: c })}
                    onDoubleClick={() => entry && navigate(envHref(s.name, c))}
                    // The global :focus-visible ring, drawn inside the cell so the scroll container does not clip it.
                    className="cursor-pointer px-1 py-1 align-top focus-visible:-outline-offset-2"
                  >
                    {entry ? (
                      <EnvCell
                        summary={entry.summary}
                        needsYou={!!entry.rights?.needsYou}
                        selected={isSel}
                        downstream={step?.likelihood}
                        now={now}
                      />
                    ) : (
                      <span
                        className={cn(
                          "block px-2 py-1.5 font-data text-[12px] text-muted",
                          isSel && "border-l-2 border-ring",
                        )}
                      >
                        —
                      </span>
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** What a screen reader hears on a cell: env, state in words, plan, age, modifiers. */
export function cellLabel(entry: EnvEntry): string {
  const { summary, stack, env } = entry;
  const run = summary.primary;
  const parts = [`${stack.name} / ${env.name}`];
  if (!run) parts.push("never run");
  else {
    parts.push(lookOf(run.state, summary.outcome).label.toLowerCase());
    if (run.state === "unconfirmed")
      parts.push(
        entry.rights?.blockedForAll.length
          ? "apply blocked"
          : entry.rights?.needsYou
            ? "needs your decision"
            : "waiting on someone",
      );
    const c = planCounts(run);
    if (c && run.state === "unconfirmed") parts.push(`plan ${formatCounts(c)}`);
    parts.push(`${age(run.finished_at ?? run.created_at)} ago`);
  }
  const m = summary.modifiers;
  if (m.mocked) parts.push("mocked");
  if (m.fallback) parts.push("fallback secret");
  if (m.stale) parts.push("branch ahead");
  if (m.drift) parts.push(m.drift === "error" ? "drift check failed" : "drifted");
  if (m.locked) parts.push("locked");
  if (m.pr) parts.push("pull-request plan active");
  return parts.join(", ");
}
