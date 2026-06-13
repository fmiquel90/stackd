import { StateBadge } from "./StateBadge";
import type { RunState } from "./state";

export default { title: "Identity / StateBadge" };

const ALL: RunState[] = [
  "queued",
  "preparing",
  "planning",
  "checking",
  "unconfirmed",
  "confirmed",
  "applying",
  "finished",
  "failed",
  "discarded",
  "canceled",
];

export const AllStates = () => (
  <div style={{ display: "flex", flexDirection: "column", gap: 8, alignItems: "flex-start" }}>
    {ALL.map((s) => (
      <StateBadge key={s} state={s} />
    ))}
  </div>
);

export const Mocked = () => <StateBadge state="unconfirmed" mocked />;
