import { STATE_META, STATE_VAR, type RunState } from "./state";

interface StateBadgeProps {
  state: RunState;
  /** Overlay the MOCKED modifier (SPECS §9.3 / DESIGN §3.2). */
  mocked?: boolean;
}

/** Status pill: semantic color + label + icon. Color never stands alone (DESIGN §7). */
export function StateBadge({ state, mocked }: StateBadgeProps) {
  const meta = STATE_META[state];
  const Icon = meta.icon;
  const color = STATE_VAR[meta.color];
  return (
    <span className="inline-flex items-center gap-2">
      <span
        className="font-data inline-flex items-center gap-1.5 rounded-badge px-1.5 py-0.5 text-[12px] uppercase tracking-wide"
        style={{ color, border: `1px solid ${color}`, backgroundColor: "transparent" }}
      >
        <Icon size={12} strokeWidth={1.5} aria-hidden />
        {meta.label}
      </span>
      {mocked && (
        <span
          className="font-data rounded-badge px-1.5 py-0.5 text-[12px] uppercase tracking-wide"
          style={{ color: "var(--color-mock)", border: "1px solid var(--color-mock)" }}
        >
          Mocked
        </span>
      )}
    </span>
  );
}
