import { useRef, type KeyboardEvent } from "react";
import { Link } from "react-router-dom";
import { RunStateBadge, ModifierChips } from "@/identity/StateBadge";
import { formatCounts, planCounts } from "@/lib/runOutcome";
import { RelativeTime } from "@/ui/primitives";
import { cn } from "@/ui/cn";
import { GROUP_LABEL, GROUP_ORDER, type AttentionItem } from "./attention";

// The answer to "what is waiting on a decision?" without searching (DESIGN §5.1). A list before the
// matrix in the tab order; j/k move between rows, Enter opens.

export function AttentionZone({
  items,
  spaceName,
  emailOf,
}: {
  items: AttentionItem[];
  spaceName: string;
  emailOf: (id: string | null | undefined) => string | null;
}) {
  const list = useRef<HTMLUListElement>(null);
  const move = (e: KeyboardEvent<HTMLUListElement>) => {
    const links = [...(list.current?.querySelectorAll<HTMLAnchorElement>("a[data-row]") ?? [])];
    const i = links.indexOf(document.activeElement as HTMLAnchorElement);
    if (e.key === "j" || e.key === "ArrowDown") {
      e.preventDefault();
      links[Math.min(links.length - 1, i + 1)]?.focus();
    } else if (e.key === "k" || e.key === "ArrowUp") {
      e.preventDefault();
      links[Math.max(0, i - 1)]?.focus();
    }
  };

  return (
    <section aria-labelledby="attention-title" className="rounded-base border border-line bg-surface">
      <h2 id="attention-title" className="eyebrow flex items-center gap-2 border-b border-line px-3 py-2">
        Attention <span className="font-data text-muted">· {items.length}</span>
      </h2>
      {items.length === 0 ? (
        <p className="px-3 py-3 text-muted">Nothing needs attention in {spaceName}.</p>
      ) : (
        // j/k are list navigation for keyboard users; arrow keys work too (DESIGN §4.3).
        // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
        <ul ref={list} onKeyDown={move} className="divide-y divide-line">
          {GROUP_ORDER.filter((g) => items.some((it) => it.group === g)).map((g) => (
            <li key={g}>
              <div className="eyebrow bg-base/40 px-3 pt-2 pb-1 text-muted">{GROUP_LABEL[g]}</div>
              <ul>
                {items
                  .filter((it) => it.group === g)
                  .map((it) => {
                    const run = it.run;
                    const counts = run ? planCounts(run) : null;
                    const who =
                      run?.triggered_by === "manual" ? emailOf(run.trigger_user_id) : run?.triggered_by;
                    return (
                      <li key={it.entry.env.id}>
                        <Link
                          data-row
                          to={it.href}
                          className={cn(
                            "flex min-h-11 flex-wrap items-center gap-x-3 gap-y-1 border-l-2 px-3 py-1.5 hover:bg-raised",
                            g === "needs_you" ? "border-st-decision bg-tint-decision" : "border-transparent",
                          )}
                        >
                          {run ? (
                            <RunStateBadge
                              run={run}
                              compact
                              decision={
                                run.state === "unconfirmed"
                                  ? g === "needs_you"
                                    ? "needsYou"
                                    : "waiting"
                                  : undefined
                              }
                            />
                          ) : (
                            <ModifierChips modifiers={{ drift: it.entry.summary.modifiers.drift }} />
                          )}
                          <span className="font-data text-[13px]">
                            {it.entry.stack.name} / {it.entry.env.name}
                          </span>
                          {counts && run?.state === "unconfirmed" && (
                            <span className="font-data text-[12px] text-muted">{formatCounts(counts)}</span>
                          )}
                          {run && (
                            <span className="font-data text-[12px] text-muted">
                              {who ? `${who} · ` : ""}
                              <RelativeTime value={run.created_at} />
                            </span>
                          )}
                          <span className="min-w-0 flex-1 truncate text-[12px] text-muted">{it.reason}</span>
                          {it.stalePlan && (
                            <span className="font-data text-[12px] text-muted">↑ stale plan</span>
                          )}
                          {run?.used_mocks && <ModifierChips modifiers={{ mocked: true }} compact />}
                        </Link>
                      </li>
                    );
                  })}
              </ul>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
