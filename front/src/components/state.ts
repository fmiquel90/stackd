import {
  Ban,
  Check,
  CirclePause,
  Clock,
  Loader,
  X,
  type LucideIcon,
} from "lucide-react";

// Run state machine (SPECS §4). `confirmed` is transitory — rendered as the start of `applying`.
export type RunState =
  | "queued"
  | "preparing"
  | "planning"
  | "checking"
  | "unconfirmed"
  | "confirmed"
  | "applying"
  | "finished"
  | "failed"
  | "discarded"
  | "canceled";

export type StateColor = "queued" | "running" | "unconfirmed" | "finished" | "failed";

interface StateMeta {
  label: string;
  color: StateColor;
  icon: LucideIcon;
}

// Color carries state only; every entry also has a label + icon (color is never the sole signal, DESIGN §7).
export const STATE_META: Record<RunState, StateMeta> = {
  queued: { label: "Queued", color: "queued", icon: Clock },
  preparing: { label: "Preparing", color: "running", icon: Loader },
  planning: { label: "Planning", color: "running", icon: Loader },
  checking: { label: "Checking", color: "running", icon: Loader },
  unconfirmed: { label: "Unconfirmed", color: "unconfirmed", icon: CirclePause },
  confirmed: { label: "Confirmed", color: "running", icon: Loader },
  applying: { label: "Applying", color: "running", icon: Loader },
  finished: { label: "Finished", color: "finished", icon: Check },
  failed: { label: "Failed", color: "failed", icon: X },
  discarded: { label: "Discarded", color: "queued", icon: Ban },
  canceled: { label: "Canceled", color: "queued", icon: Ban },
};

// CSS custom property carrying the semantic color for a given state color bucket.
export const STATE_VAR: Record<StateColor, string> = {
  queued: "var(--color-state-queued)",
  running: "var(--color-state-running)",
  unconfirmed: "var(--color-state-unconfirmed)",
  finished: "var(--color-state-finished)",
  failed: "var(--color-state-failed)",
};
