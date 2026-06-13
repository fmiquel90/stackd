import { Check, Loader, Pause, Play, X, type LucideIcon } from "lucide-react";

export type PhaseStatus = "pending" | "active" | "done" | "failed" | "skipped" | "waiting";

export interface Phase {
  key: string;
  label: string;
  status: PhaseStatus;
  /** Mono metadata shown under the label: duration, exit code, check count (DESIGN §2.2). */
  meta?: string;
}

const STATUS: Record<PhaseStatus, { color: string; icon: LucideIcon }> = {
  pending: { color: "var(--color-state-queued)", icon: Play },
  active: { color: "var(--color-state-running)", icon: Loader },
  waiting: { color: "var(--color-state-unconfirmed)", icon: Pause },
  done: { color: "var(--color-state-finished)", icon: Check },
  failed: { color: "var(--color-state-failed)", icon: X },
  skipped: { color: "var(--color-state-queued)", icon: Play },
};

interface PhaseRailProps {
  phases: Phase[];
  variant?: "vertical" | "mini";
  onSelect?: (key: string) => void;
}

/**
 * The signature element (DESIGN §2.2): makes the run state machine visible.
 * `vertical` lives on the run page; `mini` (horizontal) is reused wherever a run is listed.
 */
export function PhaseRail({ phases, variant = "vertical", onSelect }: PhaseRailProps) {
  if (variant === "mini") {
    return (
      <div className="inline-flex items-center gap-1" role="img" aria-label="Run progress">
        {phases.map((p) => (
          <span
            key={p.key}
            title={`${p.label}${p.meta ? ` · ${p.meta}` : ""}`}
            className={p.status === "active" ? "rail-pulse" : undefined}
            style={{
              width: 14,
              height: 4,
              borderRadius: 2,
              backgroundColor: STATUS[p.status].color,
              opacity: p.status === "pending" || p.status === "skipped" ? 0.35 : 1,
            }}
          />
        ))}
      </div>
    );
  }

  return (
    <ol className="flex flex-col gap-0" aria-label="Run phases">
      {phases.map((p, i) => {
        const Icon = STATUS[p.status].icon;
        const color = STATUS[p.status].color;
        const clickable = Boolean(onSelect);
        return (
          <li key={p.key} className="relative flex gap-3 pb-4 last:pb-0">
            {i < phases.length - 1 && (
              <span
                aria-hidden
                className="absolute left-[7px] top-4 bottom-0 w-px"
                style={{ backgroundColor: "var(--color-border)" }}
              />
            )}
            <span
              aria-hidden
              className={p.status === "active" ? "rail-pulse" : undefined}
              style={{
                marginTop: 2,
                width: 14,
                height: 14,
                borderRadius: 7,
                flexShrink: 0,
                backgroundColor: color,
                opacity: p.status === "pending" ? 0.35 : 1,
              }}
            />
            <button
              type="button"
              disabled={!clickable}
              onClick={() => onSelect?.(p.key)}
              className="flex flex-col items-start text-left disabled:cursor-default"
            >
              <span className="flex items-center gap-1.5 text-[13px]" style={{ color }}>
                <Icon size={13} strokeWidth={1.5} aria-hidden />
                {p.label}
              </span>
              {p.meta && (
                <span className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
                  {p.meta}
                </span>
              )}
            </button>
          </li>
        );
      })}
    </ol>
  );
}
