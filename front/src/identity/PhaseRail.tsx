import type { Segment, SegmentStatus } from "@/lib/phases";
import { cn } from "@/ui/cn";

// The signature element (DESIGN §2.2): the run state machine made visible. Each status has a glyph,
// a colour and a word — the colour is never alone.

const LOOK: Record<SegmentStatus, { glyph: string; text: string; word: string; mini: string }> = {
  pending: { glyph: "○", text: "text-st-queued", word: "pending", mini: "border border-st-queued" },
  active: { glyph: "◐", text: "text-st-running", word: "in progress", mini: "bg-st-running rail-pulse" },
  waiting: { glyph: "⏸", text: "text-st-decision", word: "waiting on a human", mini: "bg-st-decision" },
  done: { glyph: "●", text: "text-st-finished", word: "done", mini: "bg-st-finished" },
  failed: { glyph: "✕", text: "text-st-failed", word: "failed", mini: "bg-st-failed" },
  not_needed: {
    glyph: "–",
    text: "text-muted",
    word: "not needed",
    mini: "border border-dashed border-control",
  },
  not_run: { glyph: "○", text: "text-muted", word: "not run", mini: "border border-control" },
};

export interface PhaseRailProps {
  segments: Segment[];
  variant?: "vertical" | "horizontal" | "mini";
  /** Vertical/horizontal: clicking a segment opens its logs (DESIGN §2.2). */
  onSelect?: (segment: Segment) => void;
  /** Mini: accessible summary, e.g. "Unconfirmed, needs your decision, plan +2 ~1 −0". */
  label?: string;
  className?: string;
}

export function PhaseRail({ segments, variant = "vertical", onSelect, label, className }: PhaseRailProps) {
  if (variant === "mini") {
    return (
      <span
        role="img"
        aria-label={label ?? segments.map((s) => `${s.label} ${LOOK[s.status].word}`).join(", ")}
        className={cn("inline-flex items-center gap-0.5", className)}
      >
        {segments.map((s) => (
          <span key={s.key} className={cn("h-1 w-2.5 rounded-[1px]", LOOK[s.status].mini)} />
        ))}
        {segments.some((s) => s.status === "failed") && (
          <span aria-hidden className="ml-0.5 font-data text-[11px] leading-none text-st-failed">
            ✕
          </span>
        )}
      </span>
    );
  }

  const horizontal = variant === "horizontal";
  return (
    <ol
      aria-label="Run phases"
      className={cn(horizontal ? "flex items-start gap-1 overflow-x-auto" : "flex flex-col", className)}
    >
      {segments.map((s, i) => {
        const look = LOOK[s.status];
        const last = i === segments.length - 1;
        const content = (
          <>
            <span
              className={cn(
                "flex items-center gap-1.5 text-ui",
                look.text,
                s.status === "not_run" && "line-through",
              )}
            >
              <span aria-hidden className={cn("font-data", s.status === "active" && "rail-pulse")}>
                {look.glyph}
              </span>
              <span
                className={
                  s.status === "pending" || s.status.startsWith("not") ? "text-muted" : "text-primary"
                }
              >
                {s.label}
              </span>
            </span>
            <span className="sr-only">: {look.word}</span>
            {s.meta && <span className="block font-data text-[12px] text-muted">{s.meta}</span>}
          </>
        );
        return (
          <li
            key={s.key}
            className={cn(
              "relative",
              horizontal ? "min-w-[96px] flex-1 border-t-2 pt-1.5" : "flex gap-3 pb-4 last:pb-0",
              horizontal && segmentBorder(s.status),
            )}
          >
            {!horizontal && (
              <span aria-hidden className="relative flex w-3.5 shrink-0 justify-center">
                <span className={cn("mt-1.5 h-2.5 w-2.5 rounded-full", LOOK[s.status].mini)} />
                {!last && <span className="absolute top-5 bottom-[-12px] w-px bg-line" />}
              </span>
            )}
            {onSelect && s.logPhase ? (
              <button
                type="button"
                onClick={() => onSelect(s)}
                className="-mx-1 flex flex-col items-start rounded-base px-1 text-left hover:bg-raised"
                aria-label={`${s.label}: ${look.word}${s.meta ? `, ${s.meta}` : ""} — open logs`}
              >
                {content}
              </button>
            ) : (
              <div className="flex flex-col items-start">{content}</div>
            )}
          </li>
        );
      })}
    </ol>
  );
}

function segmentBorder(status: SegmentStatus): string {
  return {
    pending: "border-st-queued border-dashed",
    active: "border-st-running",
    waiting: "border-st-decision",
    done: "border-st-finished",
    failed: "border-st-failed",
    not_needed: "border-control border-dashed",
    not_run: "border-control",
  }[status];
}
