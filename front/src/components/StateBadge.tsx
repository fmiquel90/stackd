import { STATE_META, STATE_VAR, type RunState } from "./state";

interface StateBadgeProps {
  state: RunState;
  /** Overlay the MOCKED modifier (SPECS §9.3 / DESIGN §3.2). */
  mocked?: boolean;
  /** Overlay the FALLBACK modifier when a secret resolved via fallback (SPECS §15.5). */
  fallback?: boolean;
}

function Modifier({ text, color }: { text: string; color: string }) {
  return (
    <span
      className="font-data rounded-badge px-1.5 py-0.5 text-[12px] uppercase tracking-wide"
      style={{ color, border: `1px solid ${color}` }}
    >
      {text}
    </span>
  );
}

/** Status pill: semantic color + label + icon. Color never stands alone (DESIGN §7). */
export function StateBadge({ state, mocked, fallback }: StateBadgeProps) {
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
      {mocked && <Modifier text="Mocked" color="var(--color-mock)" />}
      {fallback && <Modifier text="Fallback" color="var(--color-state-unconfirmed)" />}
    </span>
  );
}
