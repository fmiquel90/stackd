import { AlertTriangle, HelpCircle } from "lucide-react";

type DriftStatus = "unknown" | "in_sync" | "drifted" | "error";

// Drift is shown only when it needs attention: an in_sync env carries no chip (no noise). Colour
// never stands alone — always an icon + label (DESIGN §7).
const META: Record<"drifted" | "error", { label: string; color: string; title: string }> = {
  drifted: {
    label: "Drift",
    color: "var(--color-state-unconfirmed)",
    title: "Real infrastructure diverged from the last applied state",
  },
  error: {
    label: "Drift check failed",
    color: "var(--color-state-failed)",
    title: "The last drift check could not complete",
  },
};

export function DriftChip({ status }: { status: DriftStatus }) {
  if (status === "in_sync" || status === "unknown") return null;
  const meta = META[status];
  const Icon = status === "error" ? HelpCircle : AlertTriangle;
  return (
    <span
      className="font-data inline-flex items-center gap-1.5 rounded-badge px-1.5 py-0.5 text-[12px] uppercase tracking-wide"
      style={{ color: meta.color, border: `1px solid ${meta.color}`, backgroundColor: "transparent" }}
      title={meta.title}
    >
      <Icon size={12} strokeWidth={1.5} aria-hidden />
      {meta.label}
    </span>
  );
}
